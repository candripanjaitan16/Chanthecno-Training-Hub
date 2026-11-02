"""Installer failure/activation tests using temporary releases and mocked build commands."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import unittest

import test_app
from app import updates
from app.paths import DATA_HOME
from app.storage import atomic_json
from app.version import VERSION

SPEC = importlib.util.spec_from_file_location('cth_install_tests', Path(__file__).resolve().parents[1]/'tools/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerUpdateTests(unittest.TestCase):
    def setUp(self):
        test_app.AppTests.setUp(self)
        import tempfile
        self.temporary = tempfile.TemporaryDirectory(prefix='cth-installer-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.program = self.root/'program'
        self.old = self.program/'releases/old'
        self.old.mkdir(parents=True)
        (self.old/'chanthecno-hub').write_text('print("old version still runs")')
        atomic_json(self.old/'.cth-release.json', {'version': '1.0.0'})
        (self.program/'current').symlink_to(self.old)
        self.binary = self.root/'bin/chanthecno-hub'
        self.binary.parent.mkdir()
        self.binary.symlink_to(self.program/'current/chanthecno-hub')
        self.source = self.root/'source'
        (self.source/'frontend').mkdir(parents=True)
        (self.source/'frontend/package.json').write_text(json.dumps({'version': VERSION}))
        (self.source/'chanthecno-hub').write_text('print("new")')
        atomic_json(self.source/'release-notes.json', {'version': VERSION, 'changes': ['New fixture']})
        patch.object(installer, 'SOURCE', self.source).start()
        patch.object(installer, 'install_home', return_value=self.program).start()
        patch.object(installer, 'tracked_sources', return_value=['frontend/package.json', 'chanthecno-hub', 'release-notes.json']).start()
        patch.object(installer.shutil, 'which', return_value='/usr/bin/fixture').start()
        patch.object(installer.subprocess, 'check_output', return_value='24.19.0\n').start()
        patch.dict('os.environ', {'CTH_BIN_DIR': str(self.binary.parent)}).start()
        self.probe = patch.object(installer, 'smoke_release').start()
        self.run = patch.object(installer, 'run', side_effect=self.fake_build).start()
        self.addCleanup(patch.stopall)
        (DATA_HOME/'model.bin').write_bytes(b'keep user model')

    def fake_build(self, *command, cwd=None, **kwargs):
        if command[:3] == ('npm', 'run', 'build'):
            (cwd/'dist').mkdir()
            (cwd/'node_modules').mkdir()
            (cwd/'dist/index.html').write_text('<div id="root"></div>')

    def install(self):
        with patch.object(sys, 'argv', ['install.py']), redirect_stdout(io.StringIO()):
            installer.main()

    def assert_old_survives(self):
        self.assertEqual((self.program/'current').resolve(), self.old)
        # Use the real subprocess implementation rather than the mocked build wrapper.
        result = subprocess.run([sys.executable, str(self.binary)], capture_output=True, text=True, check=True)
        self.assertIn('old version still runs', result.stdout)
        self.assertEqual((DATA_HOME/'model.bin').read_bytes(), b'keep user model')
        self.assertFalse((updates.DIRECTORY/'seen.json').exists())

    def test_dependency_build_and_probe_failures_keep_old_release_runnable(self):
        for stage in ('dependency', 'build', 'probe'):
            with self.subTest(stage=stage):
                def fail(*command, **kwargs):
                    if stage == 'dependency' or (stage == 'build' and command[:3] == ('npm', 'run', 'build')):
                        raise subprocess.CalledProcessError(1, command)
                    return self.fake_build(*command, **kwargs)
                self.run.side_effect = fail
                self.probe.side_effect = RuntimeError('new startup failed') if stage == 'probe' else None
                with self.assertRaises((subprocess.CalledProcessError, RuntimeError)):
                    self.install()
                self.assert_old_survives()

    def test_successful_activation_is_not_a_seen_update_until_new_version_serves(self):
        self.install()
        current = (self.program/'current').resolve()
        self.assertNotEqual(current, self.old)
        self.assertTrue(self.old.is_dir())
        self.probe.assert_called_once_with(current)
        self.assertFalse((updates.DIRECTORY/'seen.json').exists())
        with patch.object(updates, 'ROOT', current):
            value = self.client.get('/api/app/status').json()
        self.assertEqual(value['notice']['version'], VERSION)
        self.assertEqual((DATA_HOME/'model.bin').read_bytes(), b'keep user model')

    def test_active_uncertain_runtime_restore_and_sync_states_block_install(self):
        for path in (DATA_HOME/'colab_runtime_state.json', DATA_HOME/'workspace/training-web/current.json', DATA_HOME/'workspace/training-web/model-library/restore-1.json'):
            atomic_json(path, {'status': 'uncertain'})
            with self.assertRaises(ValueError):
                self.install()
            path.unlink()
            self.assert_old_survives()
        from app.datasets import db
        with db() as conn:
            conn.execute("INSERT INTO cth_datasets(id,project_id,kind,name,size,sha256,drive_status,created_at) VALUES(?,?,?,?,?,?,?,?)", ('a'*32, 1, 'txt', 'a.txt', 1, 'b'*64, 'syncing', 'fixture'))
        with self.assertRaises(ValueError):
            self.install()
        self.run.assert_not_called()
        self.assert_old_survives()

    def test_uninstall_cannot_strand_active_or_uncertain_work(self):
        atomic_json(DATA_HOME/'workspace/training-web/current.json', {'status': 'uncertain'})
        with patch.object(sys, 'argv', ['install.py', '--uninstall']), redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                installer.main()
        self.assert_old_survives()

    def test_pointer_rolls_back_if_command_installation_fails(self):
        self.binary.unlink()
        target = self.program/'releases/new'
        target.mkdir()
        original = Path.symlink_to
        def fail_binary(path, *args, **kwargs):
            if path == self.binary:
                raise OSError('command cannot be installed')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'symlink_to', fail_binary):
            with self.assertRaises(OSError):
                installer.activate(self.program, target, self.binary)
        self.assertEqual((self.program/'current').resolve(), self.old)
        self.assertTrue(self.old.is_dir())

    def test_legacy_version_is_read_without_importing_old_application(self):
        (self.old/'.cth-release.json').unlink()
        (self.old/'backend/app').mkdir(parents=True)
        (self.old/'backend/app/main.py').write_text('raise RuntimeError("must not execute")\napp = FastAPI(version="1.0.0")\n')
        self.assertEqual(installer.previous_version(self.program), '1.0.0')
