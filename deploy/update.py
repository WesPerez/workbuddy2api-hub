#!/usr/bin/env python3
"""Project-owned deployment of exact, verified GitHub-hosted runtime artifacts."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import ci_evidence as evidence
from health_probe import probe_health

PROJECT = json.loads(Path(__file__).with_name('project.json').read_text())
SHA = re.compile(r'^[a-f0-9]{40}$')


def command(args, **kwargs):
    if os.environ.get('HUB_DEPLOY_LOCK_FD'):
        kwargs.setdefault('pass_fds', (int(os.environ['HUB_DEPLOY_LOCK_FD']),))
    return subprocess.run([str(x) for x in args], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=kwargs.pop('timeout', 40), **kwargs)


class GitHub(evidence.GitHub):
    def file(self, item, destination):
        size = item.get('size', item.get('size_in_bytes'))
        if (not isinstance(size, int) or not 0 < size <= 512 * 1024**2
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', item.get('digest', ''))):
            raise ValueError('Invalid artifact size or checksum')
        path = item['download_path']
        if not re.fullmatch(r'repos/[\w.-]+/[\w.-]+/(?:releases/assets/\d+|actions/artifacts/\d+/zip)', path):
            raise ValueError('Invalid artifact download path')
        args = ['/usr/bin/gh', 'api', '--hostname', 'github.com', path]
        if '/releases/' in path:
            args += ['-H', 'Accept: application/octet-stream']
        with destination.open('xb') as stream:
            process = subprocess.Popen(args, stdout=stream, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 300
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline or destination.stat().st_size > size:
                        raise ValueError('Artifact download exceeded budget')
                    time.sleep(0.25)
                if process.returncode:
                    raise ValueError('Artifact download failed')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        with destination.open('rb') as stream:
            digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        if destination.stat().st_size != size or digest != item['digest']:
            raise ValueError('Artifact integrity mismatch')


def health(url, sha=None, idle=False):
    return probe_health(url, sha, idle)


def health_event(state, result, sha=None):
    record = dict(state=state, **result.safe_fields())
    if sha is not None and SHA.fullmatch(sha):
        record['commit'] = sha
    print(json.dumps(record))


def standby_candidate(sha):
    executable = shutil.which('gci')
    if not executable:
        return None
    try:
        state = json.loads(command([executable, 'status']).stdout)
        for row in state.get('requests', []):
            if row.get('source_repository') == PROJECT['repository'] and row.get('source_sha') == sha:
                return row.get('source_run_id') or ('request-' + row['source_request_id'])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return None


def select(github, ref):
    repository = PROJECT['repository']
    sha = github.api(f'repos/{repository}/git/ref/heads/{PROJECT["branch"]}')['object']['sha']
    if not SHA.fullmatch(sha):
        raise ValueError('Invalid branch SHA')
    if ref is None:
        standby = standby_candidate(sha)
        runs = github.api(f'repos/{repository}/actions/workflows/{PROJECT["workflow"].split("/")[-1]}/runs?branch={PROJECT["branch"]}&per_page=30')['workflow_runs']
        candidates = [row for row in runs if row.get('head_sha') == sha
                      and row.get('event') in ('push', 'workflow_dispatch')]
        ref = standby or (candidates[0]['id'] if candidates else None)
        if ref is None:
            return sha, None
    verified = evidence.verified_run(github, repository, evidence.source_id(ref), PROJECT['workflow'],
                                     'refs/heads/' + PROJECT['branch'], sha, PROJECT['required_jobs'])
    return sha, verified


def backup_data(directory, destination):
    """One consistent recovery group. Never overwrite active application data."""
    directory = Path(directory)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = destination / 'preparing'
    if staged.exists():
        raise ValueError('Previous recovery snapshot needs inspection')
    staged.mkdir(mode=0o700)
    databases = set(directory.glob('*.db'))
    for source in directory.iterdir():
        if source.name == '.draining' or source.name.endswith(('-wal', '-shm')):
            continue
        target = staged / source.name
        if source in databases and source.stat().st_size:
            with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as db:
                with sqlite3.connect(target) as copy:
                    db.backup(copy)
                    if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Recovery database is inconsistent')
        elif source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    latest, retired = destination / 'latest', destination / 'retired'
    if retired.exists():
        raise ValueError('Retired recovery group still exists')
    if latest.exists():
        latest.rename(retired)
    staged.rename(latest)
    if retired.exists():
        shutil.rmtree(retired)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/' + PROJECT['id'] + '/auto-update.json')
    parser.add_argument('--run-id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    root = Path(config['root']).resolve()
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    state = Path('/var/lib/' + PROJECT['id'] + '-auto-update')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / 'poll.lock').open('a') as lock, (root / '.deployment.lock').open('a') as deploy_lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(deploy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.environ['HUB_DEPLOY_LOCK_FD'] = str(deploy_lock.fileno())
        github = GitHub()
        sha, verified = select(github, args.run_id)
        current = root / 'current'
        old = current.resolve() if current.is_symlink() else None
        if old and old.name == sha:
            probe = health(config['health'], sha)
            if not probe:
                health_event('unhealthy_current', probe, sha); return
            data = Path(config['data'])
            data.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker = data / '.draining'
            if marker.exists() and marker.read_text() == 'update:' + sha:
                marker.unlink()
            health_event('current', probe, sha); return
        if verified is None:
            print(json.dumps({'state': 'waiting_for_ci', 'commit': sha})); return
        if not args.apply:
            print(json.dumps({'state': 'verified', 'commit': sha})); return
        if old:
            probe = health(config['health'], old.name, idle=True)
            if not probe:
                health_event('waiting_for_idle', probe); return
        if shutil.disk_usage(root.parent).free < 2 * 1024**3:
            raise ValueError('Insufficient deployment space')
        with tempfile.TemporaryDirectory(prefix='download-', dir=state) as temporary:
            temporary = Path(temporary)
            item = evidence.locate_artifact(verified, PROJECT['artifact'])
            archive_zip = temporary / 'runtime.zip'
            github.file(item, archive_zip)
            with zipfile.ZipFile(archive_zip) as archive:
                members = archive.namelist()
                expected = PROJECT['archive_prefix'] + '-' + sha + '.tar.gz'
                if set(members) != {expected, 'SHA256SUMS'}:
                    raise ValueError('Unexpected runtime artifact members')
                if any(info.file_size > 512 * 1024**2 for info in archive.infolist()):
                    raise ValueError('Runtime artifact expansion exceeds budget')
                archive.extractall(temporary)
            runtime = temporary / expected
            lines = (temporary / 'SHA256SUMS').read_text().strip().splitlines()
            if len(lines) != 1 or lines[0].split()[-1] != expected:
                raise ValueError('Runtime checksum contract differs')
            digest = lines[0].split()[0]
            installer = Path(__file__).resolve().parents[1] / 'scripts/install.py'
            install_args = [sys.executable, '-B', installer, '--archive', runtime, '--sha', sha,
                            '--sha256', digest, '--root', root]
            command(install_args, timeout=300)
            data = Path(config['data'])
            data.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker = data / '.draining'
            if marker.exists() and marker.read_text() != 'update:' + sha:
                raise ValueError('Another operation is draining the service')
            marker.write_text('update:' + sha)
            if old:
                for _ in range(30):
                    probe = health(config['health'], old.name, idle=True)
                    if probe:
                        break
                    health_event('draining_health', probe)
                    time.sleep(1)
                else:
                    marker.unlink()
                    health_event('waiting_for_idle', probe); return
            try:
                if old:
                    command(['systemctl', 'stop', config['unit']], timeout=45)
                    backup_data(config['data'], Path('/var/backups') / PROJECT['id'])
                command(install_args + ['--activate'], timeout=300)
                command(['systemctl', 'start', config['unit']], timeout=45)
                for _ in range(30):
                    probe = health(config['health'], sha)
                    if probe:
                        break
                    health_event('new_service_health', probe, sha)
                    time.sleep(1)
                else:
                    raise ValueError('New service did not pass health verification')
            except BaseException:
                if old:
                    command(['systemctl', 'stop', config['unit']], timeout=45)
                    replacement = root / '.restore-current'
                    replacement.symlink_to(old)
                    os.replace(replacement, current)
                    command(['systemctl', 'start', config['unit']], timeout=45)
                else:
                    command(['systemctl', 'stop', config['unit']], timeout=45)
                    if current.is_symlink() and current.resolve().name == sha:
                        current.unlink()
                raise
            finally:
                if marker.exists() and marker.read_text() == 'update:' + sha:
                    marker.unlink()
            (state / 'last-success.json').write_text(json.dumps({'commit': sha, 'artifact': item['digest']}) + '\n')
            # Data is outside releases. Retain current and one previous runtime.
            protected = {current.resolve(), (root / 'previous').resolve(), (root / 'updater').resolve()}
            for release in (root / 'releases').iterdir():
                if SHA.fullmatch(release.name) and release not in protected and release.is_dir() and not release.is_symlink():
                    shutil.rmtree(release)
            print(json.dumps({'state': 'deployed', 'commit': sha}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, evidence.EvidenceError) as error:
        # Provider responses, environment values and account records stay private.
        print(json.dumps({'state': 'deferred', 'reason': type(error).__name__}))
        raise SystemExit(1)
