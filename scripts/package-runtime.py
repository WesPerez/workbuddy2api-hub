#!/usr/bin/env python3
"""Package the tested runtime on GitHub-hosted CI, with no host installation."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Runtime packaging requires GitHub-hosted Actions')
    root = Path(__file__).resolve().parents[1]
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    epoch = int(subprocess.check_output(['git', 'show', '-s', '--format=%ct', sha], cwd=root, text=True))
    target = Path(os.environ['RUNNER_TEMP']) / 'workbuddy-hub-runtime'
    target.mkdir()
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    for name in filter(None, paths):
        if not (name.startswith('wb_') and name.endswith('.py') or name in {'dashboard.html', 'favicon.svg', 'LICENSE', 'README.md'} or Path(name).parts[0] in {'pricing','deploy','scripts'}):
            continue
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, dest)
    manifest = {'commit': sha, 'repository': 'WesPerez/workbuddy2api-hub', 'version': '1',
                'protocol': 'workbuddy-hub/v1', 'python': '3.12', 'platform': 'linux-x86_64'}
    (target / 'release.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
    output = Path(os.environ['RUNNER_TEMP']) / ('workbuddy-hub-' + sha + '.tar.gz')
    with output.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w', dereference=True) as archive:
            for path in sorted(target.rglob('*')):
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(target):
                    raise SystemExit('Runtime dependency link escapes the package')
                info = archive.gettarinfo(str(path), arcname='workbuddy-hub/' + str(path.relative_to(target)))
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = epoch
                with path.open('rb') as data:
                    archive.addfile(info, data)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    (output.parent / 'SHA256SUMS').write_text(checksum + '  ' + output.name + '\n')
    with tempfile.TemporaryDirectory(prefix='workbuddy-hub-package-check-', dir=os.environ['RUNNER_TEMP']) as check:
        subprocess.run([sys.executable, '-B', 'scripts/install.py', '--archive', str(output), '--sha', sha,
                        '--sha256', checksum, '--root', check], cwd=root, check=True)
    print(output)


if __name__ == '__main__':
    main()
