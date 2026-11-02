"""Offline source-index rebuild and guarded replacement; subprocesses are mocked."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import test_app
from app import model_restore
from training.source_answer import answer_from_sources


class RestoreIndexTests(unittest.TestCase):
    def test_rebuild_filters_chat_and_preserves_original_source(self):
        with tempfile.TemporaryDirectory(prefix='cth-index-') as temporary:
            root = Path(temporary)
            run = root/'run'
            run.mkdir()
            meta = {'file_id': 'fixture-file', 'name': 'fakta.txt', 'size': 100, 'sha256': 'a'*64}
            def download(drive, project, file_id, category, target, size, digest):
                target.write_text('USER: Siapa pembuat ChanThecno?\nASSISTANT: Tokoh Palsu.\nFakta: ChanThecno dikembangkan oleh Candri Panjaitan.\n')
            with patch.object(model_restore, 'download_drive', side_effect=download):
                model_restore.rebuild_index({'project_id': 1}, {'source_backups': [meta]}, root, run, object())
            self.assertEqual(json.loads((run/'source-index-version.json').read_text())['version'], 2)
            result = answer_from_sources(run/'dataset.sqlite', 'Siapa pencipta ChanThecno?')
            self.assertEqual(result['answer'], 'ChanThecno dikembangkan oleh Candri Panjaitan.')
            self.assertEqual(result['sources'], ['fakta.txt (Drive: fixture-file)'])

    def test_remote_restore_replaces_only_index_after_matching_existing_model(self):
        with tempfile.TemporaryDirectory(prefix='cth-restore-control-') as temporary:
            root = Path(temporary)
            run_id = '20261101T120000Z-1234abcd'
            run = root/'content/cth/projects/1/training_results'/run_id
            (run/'adapter').mkdir(parents=True)
            files = {'run.json': b'{"settings":{}}', 'adapter/adapter_model.safetensors': b'unchanged model weights',
                     'dataset.sqlite': b'new index fixture', 'source-index-version.json': b'{"version":2}'}
            for name, value in files.items():
                (run/name).write_bytes(b'legacy index' if name == 'dataset.sqlite' else value)
            (run/'source-index-version.json').unlink()
            archive = root/'transfer.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for name, value in files.items():
                    item = tarfile.TarInfo(name)
                    item.size = len(value)
                    tar.addfile(item, io.BytesIO(value))
            config = {'project_id': 1, 'job_id': 'a'*32, 'run_id': run_id, 'upload': str(archive),
                      'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                      'files': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}, 'requirements': ''}
            path_type = type(root)
            def mapped_path(*parts):
                path = path_type(*parts)
                text = str(path)
                if text.startswith('/content/'):
                    return root/text.lstrip('/')
                if text.startswith('/tmp/cth-inference-env'):
                    return root/'inference-env'/path.name
                return path
            output = io.StringIO()
            with patch('pathlib.Path', side_effect=mapped_path), patch('subprocess.run') as processes, redirect_stdout(output):
                exec(compile(model_restore.RESTORE.replace('__CONFIG__', repr(json.dumps(config))), '<mocked-restore-control>', 'exec'), {})
            self.assertEqual((run/'dataset.sqlite').read_bytes(), files['dataset.sqlite'])
            self.assertEqual((run/'adapter/adapter_model.safetensors').read_bytes(), files['adapter/adapter_model.safetensors'])
            self.assertEqual(json.loads((run/'source-index-version.json').read_text()), {'version': 2})
            self.assertEqual(processes.call_count, 3)  # All venv/pip/GPU commands were intercepted.
            self.assertIn('"restored": true', output.getvalue())
