#!/usr/bin/env python3
"""Verify, stage and activate a CI artifact; never build on the production host."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import platform
import tarfile
import uuid


def private_open(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
        os.close(fd)
        raise ValueError('unsafe release file')
    return os.fdopen(fd, 'rb')


def sync_dir(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def verify_tree(release, sha, digest):
    if release.is_symlink() or release.resolve() != release.absolute():
        raise ValueError('unsafe release directory')
    with private_open(release / 'archive.sha256') as stream:
        if stream.read().decode().strip() != digest:
            raise ValueError('existing release has another archive digest')
    with private_open(release / 'files.json') as stream:
        expected = json.load(stream)
    actual = {str(p.relative_to(release)) for p in release.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != set(expected) | {'files.json', 'archive.sha256'}:
        raise ValueError('installed file list has changed')
    for name, wanted in expected.items():
        parts = PurePosixPath(name)
        if parts.is_absolute() or '..' in parts.parts:
            raise ValueError('invalid installed manifest path')
        target = release / name
        if target.resolve() != target.absolute():
            raise ValueError('symlink in installed release')
        with private_open(target) as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != wanted:
                raise ValueError('installed source content has changed')
    with private_open(release / 'release.json') as stream:
        manifest = json.load(stream)
    if (manifest.get('commit') != sha or manifest.get('repository') != 'WesPerez/workbuddy2api-hub'
            or manifest.get('protocol') != 'workbuddy-hub/v1' or not manifest.get('version')
            or manifest.get('python') != '3.12' or manifest.get('platform') != 'linux-x86_64'):
        raise ValueError('release provenance mismatch')


def switch_link(root, name, target):
    existing = root / name
    if existing.exists() and not existing.is_symlink():
        raise ValueError('refusing to replace an unmanaged deployment path')
    if existing.is_symlink() and existing.resolve().parent != root / 'releases':
        raise ValueError('deployment link escapes managed releases')
    temporary = root / ('.' + name + '-' + str(uuid.uuid4()))
    try:
        temporary.symlink_to(target)
        os.replace(temporary, existing)
        sync_dir(root)
    finally:
        if temporary.is_symlink(): temporary.unlink()


def main():
    if sys.version_info[:2] != (3, 12) or sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise SystemExit('This release requires Python 3.12 on Linux x86_64.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--root', default='/opt/workbuddy-hub')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[a-f0-9]{40}', args.sha) or not re.fullmatch('[a-f0-9]{64}', args.sha256):
        raise SystemExit('Full Git SHA and SHA256 are required.')
    os.umask(0o022)
    root = Path(args.root).absolute()
    if root.resolve() != root or (root.exists() and root.stat().st_mode & 0o022):
        raise SystemExit('Unsafe installation root.')
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    releases = root / 'releases'
    releases.mkdir(mode=0o755, exist_ok=True)
    if releases.resolve() != releases or releases.stat().st_mode & 0o022:
        raise SystemExit('Unsafe releases directory.')
    inherited = os.environ.get('HUB_DEPLOY_LOCK_FD')
    deployment_path = root / '.deployment.lock'
    if inherited:
        deployment = os.dup(int(inherited))
        actual, expected = os.fstat(deployment), deployment_path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError('Deployment lock belongs to another application')
    else:
        deployment = os.open(deployment_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(deployment, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock = os.open(root / '.install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Report previous interrupted invocations; only their owning task may retire them.
        retained_staging = [str(path) for path in sorted(releases.glob('.staging-*'))]
        with private_open(args.archive) as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != args.sha256: raise ValueError('archive checksum mismatch')
            source.seek(0)
            release = releases / args.sha
            if not release.exists() and not release.is_symlink():
                staged = releases / ('.staging-' + args.sha + '-' + str(uuid.uuid4()))
                staged.mkdir(mode=0o755)
                try:
                    files, total = {}, 0
                    with tarfile.open(fileobj=source, mode='r|gz') as tar:
                        for item in tar:
                            path = PurePosixPath(item.name)
                            total += item.size
                            if (path.is_absolute() or '..' in path.parts or len(path.parts) < 2
                                    or path.parts[0] != 'workbuddy-hub' or not item.isfile()
                                    or item.size > (512*1024**2 if str(path).endswith(("/chrome", "/node")) else 64*1024**2) or total > 2*1024**3 or len(files) >= 30000):
                                raise ValueError('archive member exceeds safe extraction contract')
                            relative = str(PurePosixPath(*path.parts[1:]))
                            if relative in files or relative in {'files.json', 'archive.sha256'}:
                                raise ValueError('duplicate/reserved archive member')
                            target = staged / relative
                            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                            with tar.extractfile(item) as src, target.open('xb') as dst:
                                body = src.read(item.size + 1)
                                if len(body) != item.size: raise ValueError('truncated archive member')
                                dst.write(body)
                                dst.flush()
                                os.fsync(dst.fileno())
                            target.chmod(0o755 if item.mode & 0o111 else 0o644)
                            files[relative] = hashlib.sha256(body).hexdigest()
                    (staged / 'files.json').write_text(json.dumps(files, sort_keys=True) + '\n')
                    (staged / 'archive.sha256').write_text(digest + '\n')
                    verify_tree(staged, args.sha, digest)
                    sync_dir(staged)
                    os.replace(staged, release)
                    sync_dir(releases)
                except BaseException:
                    # Only this invocation's unpublished staging directory is removed.
                    if staged.exists(): shutil.rmtree(staged)
                    raise
            verify_tree(release, args.sha, digest)
        if args.activate:
            # The deployment entry survives a failed first application start.
            switch_link(root, 'updater', release)
            current = root / 'current'
            if current.exists() and not current.is_symlink():
                raise ValueError('current is not a managed symlink')
            if current.is_symlink():
                old = current.resolve()
                if old.parent != releases: raise ValueError('old release is outside managed root')
                if old != release: switch_link(root, 'previous', old)
            switch_link(root, 'current', release)
        print(json.dumps({'commit': args.sha, 'sha256': digest, 'release': str(release),
                          'activated': args.activate, 'retained_staging': retained_staging}))
    finally:
        os.close(lock)
        os.close(deployment)


if __name__ == '__main__': main()
