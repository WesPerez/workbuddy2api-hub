"""Account-scoped Resin leases for gateway traffic, separate from browser login.

Only the credential owner uses this integration. A fresh strict guard fences
every request; a CONNECT rejection may be retried once before any origin request.
Other transport failures affect future leases, never replay a request here.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import wb_bridge


class RouteUnavailable(RuntimeError):
    pass


class ManagedProxy(str):
    def __new__(cls, route, uid):
        value = super().__new__(cls, wb_bridge.guarded_proxy(route, allow_ipv6=True))
        value.route = copy.deepcopy(route)
        value.uid = uid
        return value


class ManagedResponse:
    """Observe transport completion without consuming or replaying a stream."""
    def __init__(self, response, proxy, chat=False):
        self.response, self.proxy, self.chat = response, proxy, chat
        self.bytes_seen = 0
        self.finished = False

    def __getattr__(self, name):
        return getattr(self.response, name)

    def _read(self, method, size):
        try:
            chunk = getattr(self.response, method)(size)
        except Exception as error:
            if not self.finished:
                self.finished = True
                note_failure(self.proxy, error)
            raise
        self.bytes_seen += len(chunk or b"")
        complete = size != 0 and (not chunk or (method == "read" and (size is None or size < 0)))
        if complete and not self.finished:
            self.finished = True
            if self.chat and not self.bytes_seen:
                note_failure(self.proxy, reason="empty_stream")
            else:
                note_success(self.proxy)
        return chunk

    def read(self, size=-1):
        return self._read("read", size)

    def readline(self, size=-1):
        return self._read("readline", size)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        # Client cancellation/unread bodies are not evidence of proxy failure.
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RouteUnavailable("Resin control endpoint redirected")


_lock = threading.RLock()
_states = {}
_account_locks = {}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
TARGET = "www.workbuddy.ai:443"
CACHE_SECONDS = 20
BACKOFF_SECONDS = 20
FAILURE_THRESHOLD = 2


def enabled(account):
    return bool(account.bridge_ref and account.realm == "intl"
                and os.environ.get("WB_RESIN_PLATFORM"))


def identity(account):
    return "wb-" + hashlib.sha256((account.bridge_ref + ":" + account.uid).encode()).hexdigest()[:24]


def source_route(account):
    wb_bridge.validate_route(account.bridge_route)
    platform = os.environ.get("WB_RESIN_PLATFORM", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", platform):
        raise RouteUnavailable("WorkBuddy Resin platform is invalid")
    key = identity(account)
    source = account.bridge_route["proxy"]
    return {"key": key, "proxy": {"server": source["server"],
            "username": platform + "." + key, "password": source["password"]},
            "binding": {"platform": platform}}


def account_lock(uid):
    with _lock:
        return _account_locks.setdefault(uid, threading.RLock())


def _control(route, action, body):
    path = "/proxy-api/v1/%s/leases/%s/actions/%s" % (
        route["binding"]["platform"], route["key"], action)
    req = urllib.request.Request(route["proxy"]["server"].rstrip("/") + path,
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + route["proxy"]["password"],
                 "Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=8) as response:
            value = json.loads(response.read(65536))
    except Exception:
        raise RouteUnavailable("Resin lease service unavailable; account request stopped") from None
    if not isinstance(value, dict) or value.get("recovery_version") != 1:
        raise RouteUnavailable("Resin lease recovery capability unavailable")
    return value


def _binding(route, result):
    lease = result.get("lease") or {}
    if lease.get("account") != route["key"]:
        raise RouteUnavailable("Resin returned a different account lease")
    binding = {"platform": route["binding"]["platform"],
        "platformId": lease.get("platform_id"), "nodeHash": lease.get("node_hash"),
        "ip": lease.get("egress_ip"), "leaseCreatedAt": lease.get("created_at_ns"),
        "leaseGuardVersion": lease.get("lease_guard_version"), "expiry": lease.get("expiry")}
    candidate = {**route, "binding": binding}
    try:
        wb_bridge.validate_route(candidate, allow_ipv6=True)
        if not isinstance(binding["leaseCreatedAt"], str):
            raise ValueError("lease generation must be a string")
    except (ValueError, TypeError):
        raise RouteUnavailable("Resin returned an invalid strict lease") from None
    return binding


def _publish(uid, state):
    # Receipts contain no proxy credential, access token or request body.
    with _lock:
        previous = _states.get(uid, {})
        _states[uid] = state
        changed = any(previous.get(k) != state.get(k) for k in
                      ("key", "binding", "status", "retryAt", "recoveries"))
        if not changed:
            return
        root = wb_bridge.data_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "resin-leases.json"
        temp = path.with_suffix(".tmp")
        payload = {key: {k: v for k, v in row.items() if k != "failures"}
                   for key, row in _states.items()}
        try:
            with temp.open("w", encoding="utf-8") as stream:
                os.chmod(temp, 0o600)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except OSError:
            # An unrecorded route must not become usable to callers.
            _states[uid] = {**previous, "checkedAt": 0, "status": "receipt_unavailable",
                            "retryAt": time.time() + BACKOFF_SECONDS}
            raise RouteUnavailable("Resin lease receipt could not be saved") from None


def _acquire(uid, route, force=False):
    with account_lock(uid):
        now = time.time()
        state = _states.get(uid, {})
        same = state.get("key") == route["key"] and (state.get("binding") or {}).get("platform") == route["binding"]["platform"]
        if same and not force and state.get("retryAt", 0) > now:
            raise RouteUnavailable("Resin route is cooling; account request stopped")
        if same and not force and state.get("status") == "available" and now - state.get("checkedAt", 0) < CACHE_SECONDS:
            return ManagedProxy({**route, "binding": state["binding"]}, uid)
        try:
            result = _control(route, "acquire", {"target_host": TARGET})
            binding = _binding(route, result) if result.get("lease") else state.get("binding", route["binding"])
            status = result.get("status")
            if status not in {"available", "no_alternative", "recovery_limited", "stale_lease"}:
                raise RouteUnavailable("Resin returned an unknown recovery status")
            if status == "available" and not result.get("lease"):
                raise RouteUnavailable("Resin returned no lease")
        except RouteUnavailable:
            _publish(uid, {**state, "key": route["key"], "binding": state.get("binding", route["binding"]),
                "status": "control_unavailable", "checkedAt": 0, "retryAt": now + BACKOFF_SECONDS})
            raise
        changed = state.get("binding", {}).get("leaseCreatedAt") != binding.get("leaseCreatedAt")
        _publish(uid, {**state, "key": route["key"], "binding": binding, "status": status,
            "checkedAt": now, "retryAt": 0 if status == "available" else now + BACKOFF_SECONDS,
            "failures": 0 if changed else state.get("failures", 0),
            "recoveries": state.get("recoveries", 0) + int(bool(state.get("binding", {}).get("leaseCreatedAt")) and changed)})
        if status != "available":
            raise RouteUnavailable("Resin %s; account request stopped" % status)
        return ManagedProxy({**route, "binding": binding}, uid)


def proxy_for(account):
    return _acquire(account.uid, source_route(account))


def guard_rejected(error):
    # Only CONNECT 409 proves the origin has not received the request. An
    # upstream HTTP 409 is a business response and must never rotate/replay it.
    return (isinstance(error, urllib.error.URLError)
            and isinstance(error.reason, OSError)
            and str(error.reason).startswith("Tunnel connection failed: 409"))


def transport_failure(error):
    if isinstance(error, urllib.error.HTTPError):
        return False
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, ssl.SSLCertVerificationError):
        return False
    return isinstance(cause, (OSError, ConnectionError, TimeoutError))


def reacquire(proxy):
    return _acquire(proxy.uid, proxy.route, force=True)


def note_failure(proxy, error=None, reason="transport_error"):
    if not isinstance(proxy, ManagedProxy) or (error is not None and not transport_failure(error)):
        return
    with account_lock(proxy.uid):
        state = _states.get(proxy.uid, {})
        binding = proxy.route["binding"]
        if state.get("binding", {}).get("leaseCreatedAt") != binding["leaseCreatedAt"]:
            return  # A delayed failure must not penalize the replacement.
        count = state.get("failures", 0) + 1
        _states[proxy.uid] = {**state, "failures": count, "checkedAt": 0}
        if count < FAILURE_THRESHOLD or state.get("retryAt", 0) > time.time():
            return
        now = time.time()
        try:
            result = _control(proxy.route, "report-failure", {"target_host": TARGET,
                "expected_node_hash": binding["nodeHash"],
                "expected_created_at_ns": binding["leaseCreatedAt"], "reason": reason})
            updated = _binding(proxy.route, result) if result.get("lease") else binding
            status = result.get("status", "control_unavailable")
        except RouteUnavailable:
            updated, status = binding, "control_unavailable"
        changed = updated["leaseCreatedAt"] != binding["leaseCreatedAt"]
        _publish(proxy.uid, {**state, "binding": updated, "status": status, "checkedAt": 0,
            "failures": 0 if changed else count,
            "retryAt": 0 if status in {"rotated", "stale_lease"} else now + BACKOFF_SECONDS,
            "recoveries": state.get("recoveries", 0) + int(changed)})


def note_success(proxy):
    if isinstance(proxy, ManagedProxy):
        with account_lock(proxy.uid):
            state = _states.get(proxy.uid, {})
            if state.get("binding", {}).get("leaseCreatedAt") == proxy.route["binding"]["leaseCreatedAt"]:
                _states[proxy.uid] = {**state, "failures": 0}


def public_route(account):
    if not enabled(account):
        route = account.bridge_route or {}
        binding = route.get("binding") or {}
        return {"policy": "protected_login_route", "key": route.get("key"),
                **{k: binding.get(k) for k in ("platform", "ip", "nodeHash", "leaseCreatedAt")}}
    with _lock:
        state = copy.deepcopy(_states.get(account.uid, {}))
    return {"policy": "account_lease_auto_recovery", "key": identity(account),
            "platform": os.environ.get("WB_RESIN_PLATFORM"),
            **{k: v for k, v in state.get("binding", {}).items() if k != "platformId"},
            **{k: state.get(k) for k in ("status", "checkedAt", "retryAt", "recoveries")}}
