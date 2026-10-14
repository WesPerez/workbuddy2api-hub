"""Scoped Account Hub login bridge. Tokens never leave this credential owner."""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.parse
import uuid


def data_root():
    return Path(os.environ.get("WB_DATA_DIR", "/var/lib/workbuddy-hub"))


def draining():
    return (data_root() / ".draining").exists()


def daily_enabled():
    # Official terms require a valid conversation in the actual client.
    # Web/API automation is not an activity reward task, including old opt-ins.
    return False


def public_base():
    value = os.environ.get("WB_PUBLIC_BASE_PATH", "").rstrip("/")
    if value and (not re.fullmatch(r"/[A-Za-z0-9_/-]+", value) or "//" in value):
        raise ValueError("invalid public base path")
    return value


def strict_proxy(proxy):
    try:
        return "~r1~" in urllib.parse.unquote(urllib.parse.urlsplit(str(proxy)).username or "")
    except ValueError:
        return False


def validate_route(route, allow_ipv6=False):
    if not isinstance(route, dict):
        raise ValueError("managed route is required")
    proxy, binding = route.get("proxy", {}), route.get("binding", {})
    parsed = urllib.parse.urlsplit(proxy.get("server", ""))
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not isinstance(proxy.get("password"), str) or not proxy["password"]):
        raise ValueError("managed HTTP proxy configuration is invalid")
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", route.get("key", ""))
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", binding.get("platform", ""))
            or proxy.get("username") != binding["platform"] + "." + route["key"]
            or binding.get("leaseGuardVersion") != 1
            or not re.fullmatch(r"[a-f0-9]{32}", binding.get("nodeHash", ""))
            or not re.fullmatch(r"[1-9][0-9]{0,18}", str(binding.get("leaseCreatedAt", "")))):
        raise ValueError("managed route guard is incomplete")
    address = ipaddress.ip_address(binding.get("ip", ""))
    if address.version != 4 and not allow_ipv6:
        raise ValueError("managed route requires verified IPv4")
    if address.version == 6 and (getattr(address, "scope_id", None)
            or address.is_multicast or address.is_unspecified
            or address.is_loopback or address.is_link_local):
        raise ValueError("managed route requires an unscoped unicast IP")
    return route


def guarded_proxy(route, allow_ipv6=False):
    validate_route(route, allow_ipv6=allow_ipv6)
    proxy, binding = route["proxy"], route["binding"]
    # New CONNECTs are fenced by the original node, IP and lease generation.
    # A minute bucket preserves bounded connection reuse within the lease.
    until = (int(time.time()) // 60) * 60000 + 14 * 60000
    encoded_ip = base64.urlsafe_b64encode(binding["ip"].encode()).decode().rstrip("=")
    username = "%s~r1~%s~%s~%s~%s" % (proxy["username"], binding["nodeHash"],
                                     binding["leaseCreatedAt"], until, encoded_ip)
    parsed = urllib.parse.urlsplit(proxy["server"])
    return urllib.parse.urlunsplit(("http", urllib.parse.quote(username, safe="") + ":" +
        urllib.parse.quote(proxy["password"], safe="") + "@" + parsed.netloc, "", "", ""))


def public_account(account):
    return {"uid": account.uid, "name": account.nickname, "realm": account.realm,
            "accountRef": account.bridge_ref, "revision": account.bridge_revision,
            "enabled": account.enabled, "credentialOwner": "workbuddy-hub",
            "expiresAt": account.expires_at, "routeConfigured": bool(account.bridge_route)}


class Bridge:
    def __init__(self, pool):
        self.pool = pool
        root = data_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(root / "bridge.db"), check_same_thread=False)
        os.chmod(root / "bridge.db", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS logins(id TEXT PRIMARY KEY,idem TEXT UNIQUE,"
                        "input_hash TEXT NOT NULL,account_ref TEXT NOT NULL,state TEXT NOT NULL,"
                        "payload TEXT NOT NULL,result TEXT,created REAL NOT NULL)")
        self.db.commit()
        self.lock = threading.RLock()
        self.active = 0

    def stored(self, task_id):
        row = self.db.execute("SELECT * FROM logins WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("login task not found")
        return dict(row)

    def persist(self, row):
        if row["state"] in {"ready", "failed", "expired"}:
            payload = json.loads(row["payload"])
            for key in ("candidate", "oauthState", "authUrl"):
                payload.pop(key, None)
            row["payload"] = json.dumps(payload)
        self.db.execute("UPDATE logins SET state=?,payload=?,result=? WHERE id=?",
                        (row["state"], row["payload"], row.get("result"), row["id"]))
        self.db.commit()

    def view(self, row):
        data = json.loads(row["payload"])
        result = json.loads(row["result"]) if row.get("result") else {}
        return {"id": row["id"], "status": row["state"], "accountRef": row["account_ref"],
                **({"authUrl": data["authUrl"]} if row["state"] == "pending" else {}),
                **({"account": result} if row["state"] == "ready" else {})}

    def start(self, value):
        import wb_accounts as accounts
        ref, idem = value.get("accountRef", ""), value.get("idempotencyKey", "")
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,120}", ref) or not re.fullmatch(r"[A-Za-z0-9:_-]{8,160}", idem):
            raise ValueError("invalid account reference or idempotency key")
        route = validate_route(value.get("route"))
        github_login = str(value.get("githubLogin", ""))
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", github_login):
            raise ValueError("verified GitHub identity is required")
        fingerprint = hashlib.sha256(json.dumps([ref, value.get("expectedUid"), github_login.lower(), route],
                                                sort_keys=True).encode()).hexdigest()
        with self.lock:
            prior = self.db.execute("SELECT * FROM logins WHERE idem=?", (idem,)).fetchone()
            if prior:
                if prior["input_hash"] != fingerprint:
                    raise ValueError("idempotency key belongs to another request")
                return self.view(dict(prior))
            if self.db.execute("SELECT 1 FROM logins WHERE account_ref=? AND state IN ('starting','pending','candidate')"
                               " AND created>?", (ref, time.time() - 600)).fetchone():
                raise ValueError("another login for this identity is pending")
            existing = next((a for a in self.pool.accounts if a.bridge_ref == ref), None)
            if existing and value.get("expectedUid") != existing.uid:
                raise ValueError("existing WorkBuddy identity must be explicitly matched")
            task_id = str(uuid.uuid4())
            data = {"route": route, "expectedUid": value.get("expectedUid"),
                    "expectedRevision": existing.bridge_revision if existing else None,
                    "githubLogin": github_login}
            row = {"id": task_id, "state": "starting", "payload": json.dumps(data),
                   "account_ref": ref, "result": None}
            self.db.execute("INSERT INTO logins VALUES(?,?,?,?,?,?,?,?)",
                            (task_id, idem, fingerprint, ref, "starting", row["payload"], None, time.time()))
            self.db.commit()
            cfg = accounts.get_realm_config("intl")
            try:
                payload = accounts.http_json(cfg["chat_upstream"] + accounts.AUTH_STATE_PATH + "?platform=CLI",
                    data=b"{}", method="POST", headers=self.headers(cfg), timeout=25, retries=1,
                    proxy=guarded_proxy(route))
                auth = payload.get("data") or {}
                parsed = urllib.parse.urlsplit(auth.get("authUrl", ""))
                if (not auth.get("state") or parsed.scheme != "https"
                        or parsed.hostname not in {"www.codebuddy.ai", "www.workbuddy.ai", "codebuddy.ai", "workbuddy.ai"}
                        or parsed.username or parsed.password):
                    raise ValueError("unexpected OAuth authorization URL")
                data.update({"oauthState": auth["state"], "authUrl": auth["authUrl"]})
                row.update(state="pending", payload=json.dumps(data))
                self.persist(row)
                return self.view(row)
            except Exception:
                row["state"] = "failed"
                self.persist(row)
                raise ValueError("WorkBuddy authorization could not start")

    @staticmethod
    def headers(cfg):
        return {"Content-Type": "application/json", "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest", "User-Agent": cfg["billing_ua"],
                "Origin": cfg["origin"], "Referer": cfg["origin"] + "/"}

    def poll(self, task_id):
        import wb_accounts as accounts
        with self.lock:
            row = self.stored(task_id)
            if row["state"] in {"ready", "failed", "expired"}:
                return self.view(row)
            # Recover the account-file commit if its SQLite acknowledgement was lost.
            committed = None
            for account in self.pool.accounts:
                if account.bridge_task != task_id or not account.path:
                    continue
                try:
                    saved = json.loads(Path(account.path).read_text())
                except (OSError, ValueError):
                    continue
                if (saved.get("bridgeTask") == task_id and saved.get("bridgeRef") == row["account_ref"]
                        and saved.get("uid") == account.uid):
                    committed = account
                    break
            if committed:
                row.update(state="ready", result=json.dumps(public_account(committed)))
                self.persist(row)
                return self.view(row)
            if row["created"] + 600 < time.time():
                row["state"] = "expired"
                self.persist(row)
                return self.view(row)
            if row["state"] == "starting":
                raise ValueError("login start was interrupted; begin a new login")
            data = json.loads(row["payload"])
            cfg = accounts.get_realm_config("intl")
            if row["state"] == "pending":
                payload = accounts.http_json(cfg["chat_upstream"] + accounts.AUTH_TOKEN_PATH +
                    "?state=" + urllib.parse.quote(data["oauthState"]), headers=self.headers(cfg),
                    timeout=25, retries=1, proxy=guarded_proxy(data["route"]))
                if payload.get("code") == accounts.LOGIN_PENDING:
                    return self.view(row)
                if payload.get("code") != 0:
                    raise ValueError("WorkBuddy login has not completed")
                candidate = payload.get("data") or {}
                token = candidate.get("accessToken")
                if not token:
                    return self.view(row)
                uid = accounts.jwt_uid(token)
                if not uid or (data.get("expectedUid") and uid != data["expectedUid"]):
                    raise ValueError("WorkBuddy identity does not match the linked account")
                data["candidate"] = {"uid": uid, "accessToken": token,
                    "refreshToken": candidate.get("refreshToken") or "",
                    "expiresAt": accounts.normalize_epoch(candidate.get("expiresAt")) or accounts.jwt_exp(token),
                    "domain": candidate.get("domain") or cfg["domain"], "realm": "intl", "platform": "CLI",
                    "enabled": False, "source": "account-hub", "bridgeRef": row["account_ref"],
                    "bridgeRoute": data["route"], "bridgeTask": task_id, "bridgeRevision": str(uuid.uuid4()),
                    "note": "Account Hub / GitHub " + data.get("githubLogin", "")}
                row.update(state="candidate", payload=json.dumps(data))
                self.persist(row)
            candidate = accounts.Account(data["candidate"])
            if not candidate.refresh_token:
                raise ValueError("WorkBuddy did not issue an offline credential")
            uid, nickname = accounts.fetch_account_profile(candidate)
            if uid != candidate.uid:
                raise ValueError("WorkBuddy profile identity is unavailable or mismatched")
            candidate.nickname = nickname or data.get("githubLogin") or uid[:8]
            with self.pool._lock:
                existing = self.pool.get(uid)
                if existing:
                    if existing.bridge_ref != row["account_ref"]:
                        raise ValueError("WorkBuddy account is already owned by another login source")
                    with existing._refresh_lock:
                        if existing.bridge_revision != data.get("expectedRevision"):
                            raise ValueError("WorkBuddy credential generation changed during login")
                        fields = ["access_token", "refresh_token", "expires_at", "domain", "nickname",
                                  "bridge_route", "bridge_task", "bridge_revision"]
                        before = {field: getattr(existing, field) for field in fields}
                        try:
                            for field in fields:
                                setattr(existing, field, getattr(candidate, field))
                            existing.save(self.pool.dir)
                        except BaseException:
                            for field, old in before.items():
                                setattr(existing, field, old)
                            raise
                    account = existing
                else:
                    if data.get("expectedRevision"):
                        raise ValueError("previous WorkBuddy account was removed during login")
                    candidate.enabled = True
                    candidate.save(self.pool.dir)
                    self.pool.accounts.append(candidate)
                    self.pool.apply_reserve_credits()
                    self.pool.apply_expiring_window()
                    account = candidate
            # No token material is needed after the account file owns it.
            data.pop("candidate", None)
            data.pop("oauthState", None)
            data.pop("authUrl", None)
            row.update(state="ready", payload=json.dumps(data), result=json.dumps(public_account(account)))
            self.persist(row)
            return self.view(row)


_bridge = None
_bridge_lock = threading.Lock()


def instance(pool):
    global _bridge
    with _bridge_lock:
        if _bridge is None or _bridge.pool is not pool:
            _bridge = Bridge(pool)
    return _bridge


def health(proxy):
    try:
        commit = json.loads(Path(__file__).with_name("release.json").read_text())["commit"]
    except (OSError, ValueError, KeyError):
        commit = "development"
    busy = max(0, proxy.MAX_CONCURRENT_CHAT - proxy._chat_slots._value)
    busy += sum(a._refresh_lock.locked() or a._credits_lock.locked() for a in (proxy.POOL.accounts if proxy.POOL else []))
    busy += int(bool(proxy.SCHEDULER and proxy.SCHEDULER._run_lock.locked()))
    busy += _bridge.active if _bridge else 0
    return {"ok": True, "service": "workbuddy-hub", "protocol": "workbuddy-hub/v1",
            "commit": commit, "activeJobs": busy, "internationalDailyChat": daily_enabled()}


def dispatch(handler, proxy):
    path = handler.path.split("?")[0]
    if path in {"/favicon.svg", "/favicon.ico"} and handler.command == "GET":
        body = Path(__file__).with_name("favicon.svg").read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "image/svg+xml")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Cache-Control", "public, max-age=3600")
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path == "/healthz" and handler.command == "GET":
        handler._json(200, health(proxy))
        return True
    if not path.startswith("/bridge/v1/"):
        return False
    token_file = Path(os.environ.get("WB_BRIDGE_TOKEN_FILE", str(data_root() / "bridge.token")))
    try:
        expected = token_file.read_text().strip()
    except OSError:
        expected = ""
    authorization = handler.headers.get("Authorization", "")
    if not expected or not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization[7:].encode(), expected.encode()):
        handler._json(401, {"error": "bridge token required"})
        return True
    if path == "/bridge/v1/health" and handler.command == "GET":
        handler._json(200, health(proxy))
        return True
    service = instance(proxy.POOL)
    if path == "/bridge/v1/accounts" and handler.command == "GET":
        import wb_resin
        handler._json(200, {"accounts": [{**public_account(a), "egress": wb_resin.public_route(a)}
                                        for a in proxy.POOL.accounts if a.bridge_ref]})
        return True
    if handler.command != "POST" or path not in {"/bridge/v1/login/start", "/bridge/v1/login/poll",
                                                "/bridge/v1/routes/reconcile"}:
        handler._json(404, {"error": "bridge operation not found"})
        return True
    try:
        size = int(handler.headers.get("Content-Length", "0"))
        if not 0 < size <= 32768:
            raise ValueError("invalid body length")
        payload = json.loads(handler.rfile.read(size))
        if not isinstance(payload, dict) or draining():
            raise ValueError("bridge is unavailable")
        service.active += 1
        try:
            if path.endswith("/routes/reconcile"):
                import wb_resin
                results = []
                for account in proxy.POOL.accounts:
                    if not account.bridge_ref or (payload.get("uid") and payload["uid"] != account.uid):
                        continue
                    try:
                        with account._refresh_lock:
                            if not wb_resin.enabled(account):
                                raise ValueError("account lease recovery is not configured")
                            wb_resin.proxy_for(account)
                        results.append({**public_account(account), "ok": True,
                                        "egress": wb_resin.public_route(account)})
                    except Exception as error:
                        results.append({"uid": account.uid, "ok": False, "errorKind": type(error).__name__,
                                        "egress": wb_resin.public_route(account)})
                result = {"accounts": results}
            else:
                result = service.start(payload) if path.endswith("/start") else service.poll(str(payload.get("id", "")))
        finally:
            service.active -= 1
        handler._json(200, result)
    except Exception:
        handler._json(409, {"error": "WorkBuddy connection is not complete; check identity, route and login state"})
    return True
