#!/usr/bin/env python3
"""Read-only verification of an intl managed account on its original egress."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wb_accounts import WEB_USER_AGENT, http_json
from wb_bridge import guarded_proxy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uid', required=True)
    parser.add_argument('--expected-ref', required=True)
    parser.add_argument('--data-dir', default=os.environ.get('WB_DATA_DIR', '/var/lib/workbuddy-hub'))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', args.uid):
        parser.error('invalid uid')
    if not re.fullmatch(r'account-hub:[1-9][0-9]*', args.expected_ref):
        parser.error('invalid expected ref')
    report = {'uid': args.uid, 'accountRef': args.expected_ref, 'checkedAt': int(time.time()),
              'readOnly': True, 'credentialOwner': 'workbuddy-hub', 'checks': []}
    try:
        # Do not create another AccountPool, refresh tokens, or save credentials.
        account = json.loads((Path(args.data_dir) / 'accounts' / (args.uid + '.json')).read_text())
        if (account.get('uid') != args.uid or account.get('bridgeRef') != args.expected_ref
                or account.get('realm') != 'intl' or not account.get('accessToken')):
            raise ValueError('account identity mismatch')
        route = account['bridgeRoute']
        proxy = guarded_proxy(route)
        report.update({'realm': 'intl', 'egressKey': route['key'], 'routeSource': 'persisted_bridge_route',
                       'leaseCreatedAt': route['binding']['leaseCreatedAt'],
                       'ip': route['binding']['ip'], 'platform': route['binding']['platform']})
        headers = {'Authorization': 'Bearer ' + account['accessToken'], 'Accept': 'application/json',
                   'x-client-platform': 'web', 'Origin': 'https://www.workbuddy.ai',
                   'Referer': 'https://www.workbuddy.ai/login/started', 'User-Agent': WEB_USER_AGENT}
        for path in ('/console/accounts', '/v2/plugin/accounts'):
            data = http_json('https://www.workbuddy.ai' + path, headers=headers,
                             timeout=20, retries=1, proxy=proxy)
            if isinstance(data, dict) and data.get('code', 0) not in (0, '0'):
                raise ValueError('profile response rejected')
            envelope = data.get('data', data) if isinstance(data, dict) else data
            rows = envelope.get('accounts', []) if isinstance(envelope, dict) else envelope
            matched = [row for row in rows if isinstance(row, dict) and row.get('uid') == args.uid] if isinstance(rows, list) else []
            report['checks'].append({'path': path, 'match': len(matched) == 1,
                                     'areaInfoComplete': matched[0].get('areaInfoComplete') if len(matched) == 1 else None})
        report['ok'] = all(row['match'] and row['areaInfoComplete'] is True for row in report['checks'])
    except Exception as error:
        # Exception messages can include the authenticated proxy URL.
        report.update({'ok': False, 'errorKind': type(error).__name__})
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
