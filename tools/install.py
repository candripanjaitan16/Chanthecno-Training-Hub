#!/usr/bin/env python3
"""Build a tracked Git checkout into an isolated, per-user release."""
import argparse
import ast
import json
import socket
import sqlite3
import re
import signal
import tempfile
import time
import urllib.request
import urllib.error
import fcntl
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from uuid import uuid4

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'backend'))
from app.paths import STATE_HOME, prepare
from app.storage import authentication_name, atomic_json
from app.version import VERSION, stable_version
from app.maintenance import require_inactive


def run(*command, cwd=None, env=None):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def install_home():
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')).expanduser().resolve() / 'chanthecno-hub/program'


def tracked_sources():
    result = subprocess.check_output(['git', 'ls-files', '-z'], cwd=SOURCE)
    names = []
    for raw in result.split(b'\0'):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = PurePosixPath(name)
        if path.parts[0] not in {'backend', 'frontend', 'training', 'tools', 'chanthecno-hub', 'requirements-training.txt', 'release-notes.json'}:
            continue
        if authentication_name(path.name) or path.suffix.lower() in {'.db', '.sqlite'}:
            raise RuntimeError(f'Berkas sensitif terlacak Git: {name}. Instalasi dibatalkan.')
        actual = SOURCE / name
        if actual.is_symlink() or not actual.is_file() or actual.resolve() != actual:
            raise RuntimeError(f'Berkas sumber tidak aman: {name}')
        names.append(name)
    if not names:
        raise RuntimeError('Jalankan installer dari checkout GitHub yang valid.')
    return names


def previous_version(root):
    current = root / 'current'
    if not current.is_symlink():
        return None
    release = current.resolve(strict=True)
    if release.parent != (root / 'releases').resolve():
        raise RuntimeError('Rilis aktif berada di luar direktori installer.')
    metadata = release / '.cth-release.json'
    if metadata.is_file():
        version = json.loads(metadata.read_text())['version']
        stable_version(version)
        return version
    # Legacy installs advertised their version in FastAPI, before version metadata existed.
    tree = ast.parse((release / 'backend/app/main.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'FastAPI':
            for argument in node.keywords:
                if argument.arg == 'version' and isinstance(argument.value, ast.Constant):
                    stable_version(argument.value.value)
                    return argument.value.value
    return None


def smoke_release(release):
    with tempfile.TemporaryDirectory(prefix='cth-release-probe-') as temporary:
        probe = Path(temporary)
        environment = os.environ.copy()
        environment.update(CTH_DATA_DIR=str(probe/'data'), CTH_CONFIG_DIR=str(probe/'config'), CTH_STATE_DIR=str(probe/'state'))
        with socket.socket() as address:
            address.bind(('127.0.0.1', 0))
            port = address.getsockname()[1]
        with (probe/'startup.log').open('w') as log:
            process = subprocess.Popen([str(release/'backend/.venv/bin/python'), '-m', 'uvicorn', 'app.main:app',
                '--host', '127.0.0.1', '--port', str(port)], cwd=release/'backend', env=environment,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError('Uji startup versi baru gagal; rilis aktif dipertahankan.')
                try:
                    with opener.open(f'http://127.0.0.1:{port}/api/hello', timeout=1) as response:
                        if json.load(response).get('version') != VERSION:
                            raise RuntimeError('Versi backend hasil build tidak cocok.')
                    with opener.open(f'http://127.0.0.1:{port}/api/app/status', timeout=1) as response:
                        if json.load(response).get('version') != VERSION:
                            raise RuntimeError('Metadata versi hasil build tidak cocok.')
                    with opener.open(f'http://127.0.0.1:{port}/', timeout=1) as response:
                        response_text = response.read(1024*1024).decode()
                        if '<div id="root">' not in response_text:
                            raise RuntimeError('Frontend produksi tidak valid.')
                    asset = re.search(r'src="(/assets/[^"]+\.js)"', response_text)
                    if not asset:
                        raise RuntimeError('Referensi aset frontend produksi tidak tersedia.')
                    with opener.open(f'http://127.0.0.1:{port}' + asset.group(1), timeout=1) as response:
                        if not response.read(1):
                            raise RuntimeError('Aset frontend produksi kosong.')
                    break
                except (urllib.error.URLError, OSError):
                    if time.monotonic() > deadline:
                        raise RuntimeError('Uji startup versi baru timeout; rilis aktif dipertahankan.') from None
                    time.sleep(.1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def activate(root, release, binary):
    current = root / 'current'
    previous = current.readlink() if current.is_symlink() else None
    temporary = root / ('current-' + uuid4().hex)
    temporary.symlink_to(release, target_is_directory=True)
    changed = False
    try:
        os.replace(temporary, current)
        changed = True
        if not binary.is_symlink():
            binary.symlink_to(root / 'current/chanthecno-hub')
    except BaseException:
        if changed:
            if previous is None:
                current.unlink()
            else:
                rollback = root / ('rollback-' + uuid4().hex)
                rollback.symlink_to(previous, target_is_directory=True)
                os.replace(rollback, current)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Install CTH untuk pengguna saat ini, tanpa sudo')
    parser.add_argument('--version', action='version', version=f'CTH installer {VERSION}')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        raise RuntimeError('Jalankan sebagai pengguna biasa, tanpa sudo.')
    root = install_home()
    prepare()
    bindir = Path(os.environ.get('CTH_BIN_DIR', Path.home() / '.local/bin')).expanduser()
    if not bindir.is_absolute():
        raise RuntimeError('CTH_BIN_DIR harus path absolut.')
    binary = bindir / 'chanthecno-hub'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'install.lock').open('a') as lock, (STATE_HOME / 'application.lock').open('a') as application, (STATE_HOME / 'api.lock').open('a') as api:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(application, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(api, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Tutup CTH dan pemeliharaan lokal sebelum instalasi atau uninstall.') from None
        require_inactive()
        if args.uninstall:
            if binary.is_symlink() and binary.readlink() == root / 'current/chanthecno-hub':
                binary.unlink()
            for item in ('current', 'releases'):
                target = root / item
                if target.is_symlink():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            print('Program dihapus. Data, konfigurasi, token, dan cadangan pengguna dipertahankan.')
            return
        old_version = previous_version(root)
        if sys.version_info < (3, 10):
            raise RuntimeError('Python 3.10+ dengan modul venv diperlukan.')
        for tool in ('git', 'node', 'npm'):
            if not shutil.which(tool):
                raise RuntimeError(f'{tool} belum tersedia. Lihat README untuk prasyarat.')
        node = subprocess.check_output(['node', '-p', 'process.versions.node'], text=True).strip()
        major, minor = map(int, node.split('.')[:2])
        if major < 22 or (major == 22 and minor < 12):
            raise RuntimeError('Gunakan Node.js 22.12+ atau 24 LTS.')
        if binary.exists() or binary.is_symlink():
            if not binary.is_symlink() or binary.readlink() != root / 'current/chanthecno-hub':
                raise RuntimeError(f'{binary} sudah ada dan bukan milik installer ini.')
        notes = json.loads((SOURCE/'release-notes.json').read_text())
        frontend_version = json.loads((SOURCE/'frontend/package.json').read_text())['version']
        if notes.get('version') != VERSION or frontend_version != VERSION:
            raise RuntimeError('Versi aplikasi, catatan rilis, dan frontend tidak selaras.')
        names = tracked_sources()
        releases = root / 'releases'
        releases.mkdir(exist_ok=True)
        release = releases / uuid4().hex
        release.mkdir(mode=0o700)
        switched = False
        try:
            for name in names:
                target = release / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(SOURCE / name, target)
            run(sys.executable, '-m', 'venv', str(release / 'backend/.venv'))
            python = release / 'backend/.venv/bin/python'
            run(str(python), '-m', 'pip', 'install', '-r', str(release / 'backend/requirements.txt'))
            run(str(python), '-m', 'pip', 'check')
            run('npm', 'ci', '--no-audit', '--no-fund', cwd=release / 'frontend')
            environment = os.environ.copy()
            environment['VITE_API_BASE_URL'] = ''
            run('npm', 'run', 'build', cwd=release / 'frontend', env=environment)
            if not (release / 'frontend/dist/index.html').is_file():
                raise RuntimeError('Build frontend tidak menghasilkan index.html.')
            shutil.rmtree(release / 'frontend/node_modules')
            (release / 'chanthecno-hub').chmod(0o755)
            binary.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(release/'.cth-release.json', {'version': VERSION, 'previous_version': old_version})
            smoke_release(release)
            activate(root, release, binary)
            switched = True
        finally:
            if not switched:
                shutil.rmtree(release)
        print(f'CTH {VERSION} dipasang dan lolos uji startup terisolasi. Jalankan: chanthecno-hub')
        print('Pembaruan baru dinyatakan berjalan ketika versi baru melayani aplikasi pengguna.')
        print('Rilis sebelumnya dipertahankan; pembaruan tidak menyentuh data pengguna.')
        if str(binary.parent) not in os.environ.get('PATH', '').split(os.pathsep):
            print('Tambahkan ~/.local/bin ke PATH di ~/.profile, lalu buka terminal baru:')
            print('export PATH="$HOME/.local/bin:$PATH"')
            if bindir != Path.home() / '.local/bin':
                print(f'Direktori bin kustom yang harus masuk PATH: {bindir}')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f'Instalasi gagal: {error}', file=sys.stderr)
        raise SystemExit(1)
