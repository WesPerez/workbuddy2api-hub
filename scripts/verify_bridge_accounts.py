#!/usr/bin/env python3
"""Read-only verification of accounts delegated from Account Hub.

Authorization success alone does not prove the delegated credential works: a
token can be stored while the international realm has not finished region
registration (``areaInfoComplete``) or while it belongs to another identity.
This prints one JSON line per account so the result can be archived as
evidence, and it never writes credentials, usage or profile fields.

Usage:
    python3 scripts/verify_bridge_accounts.py --ref account-hub:11 --ref account-hub:12
    python3 scripts/verify_bridge_accounts.py            # every bridged account
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wb_accounts as accounts  # noqa: E402  (path is set above)

# Only non-identifying, non-personal fields are read back out.
REPORTED_FIELDS = ('uid', 'nickname', 'areaInfoComplete')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default=os.environ.get('WB_DATA_DIR', '/var/lib/workbuddy-hub'))
    parser.add_argument('--ref', action='append', dest='refs',
                        help='bridgeRef to verify (repeatable); default is every bridged account')
    args = parser.parse_args()
    directory = Path(args.data_dir) / 'accounts'
    pool = accounts.AccountPool(str(directory))
    for loader in ('load', 'reload'):
        if hasattr(pool, loader):
            getattr(pool, loader)()
    wanted = set(args.refs or [])
    checked = failed = 0
    for account in pool.accounts:
        ref = getattr(account, 'bridge_ref', '') or ''
        if not ref or (wanted and ref not in wanted):
            continue
        checked += 1
        origin = accounts.web_origin_for(getattr(account, 'realm', ''))
        headers, proxy = account.request_snapshot(header_factory=lambda: {
            'Authorization': 'Bearer ' + str(account.access_token or ''),
            'Accept': 'application/json, text/plain, */*',
            'x-client-platform': 'web',
            'Origin': origin,
            'Referer': origin + '/profile/account-settings',
            'User-Agent': accounts.WEB_USER_AGENT,
        })
        request = accounts.urllib.request.Request(origin + accounts.PROFILE_PATH, method='GET', headers=headers)
        try:
            with accounts.urlopen(request, timeout=15, proxy=proxy) as response:
                payload = json.loads(response.read().decode('utf-8'))
            if isinstance(payload, dict) and 'data' in payload:
                payload = payload.get('data') or {}
            observed = {name: payload.get(name) for name in REPORTED_FIELDS}
            matches = str(observed.get('uid') or '') == str(account.uid or '')
            complete = observed.get('areaInfoComplete') is True
            if not (matches and complete):
                failed += 1
            print(json.dumps({'ref': ref, 'realm': getattr(account, 'realm', ''),
                              'identityMatches': matches, 'areaInfoComplete': complete,
                              **observed}, ensure_ascii=False))
        except Exception as error:  # Network, TLS and provider errors stay generic.
            failed += 1
            print(json.dumps({'ref': ref, 'error': type(error).__name__}, ensure_ascii=False))
    print(json.dumps({'checked': checked, 'failed': failed}))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
