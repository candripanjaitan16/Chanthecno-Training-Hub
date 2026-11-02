"""Drive protocol and launch gating tests; only in-memory fake Google responses."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import unittest

import httplib2
from googleapiclient.errors import HttpError

import test_app
from app import datasets, drive_storage, training_jobs, dataset_manifest
from app.storage import atomic_json


class Reply:
    def __init__(self, operation):
        self.operation = operation

    def execute(self):
        return self.operation()


class FakeDrive:
    def __init__(self):
        self.metadata = {}
        self.content = {}
        self.creates = 0
        self.generated = 0
        self.fail_upload = False
        self.lose_ack = False
        for project in (1, 2):
            for name, parents in ((f'project-{project}', []), (f'datasets-{project}', [f'project-{project}'])):
                self.metadata[name] = {'id': name, 'name': 'datasets' if parents else name,
                    'parents': parents, 'mimeType': 'application/vnd.google-apps.folder',
                    'capabilities': {'canAddChildren': True}, 'trashed': False}

    def files(self):
        return self

    def get(self, fileId, fields):
        def value():
            if fileId not in self.metadata:
                raise HttpError(httplib2.Response({'status': '404'}), b'not found')
            return dict(self.metadata[fileId])
        return Reply(value)

    def list(self, q, fields, pageSize, pageToken=None):
        import re
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        values = [item for item in self.metadata.values() if parent in item.get('parents', []) and not item.get('trashed')]
        if "name='datasets'" in q:
            values = [item for item in values if item['name'] == 'datasets']
        else:
            sha = re.search(r"key='cth_sha256' and value='([^']+)'", q).group(1)
            project = re.search(r"key='cth_project_id' and value='([^']+)'", q).group(1)
            values = [item for item in values if item.get('appProperties') == {'cth_sha256': sha, 'cth_project_id': project}]
        return Reply(lambda: {'files': [{'id': item['id']} for item in values]})

    def generateIds(self, **kwargs):
        self.generated += 1
        return Reply(lambda: {'ids': [f'file-{self.generated}']})

    def create(self, body, media_body, fields):
        self.creates += 1
        drive = self
        class Upload:
            def next_chunk(self, num_retries):
                if drive.fail_upload:
                    raise TimeoutError('upload unavailable')
                data = media_body.getbytes(0, media_body.size())
                file_id = body['id']
                drive.content[file_id] = data
                drive.metadata[file_id] = {**body, 'size': str(len(data)), 'trashed': False,
                    'md5Checksum': hashlib.md5(data).hexdigest(), 'sha256Checksum': hashlib.sha256(data).hexdigest()}
                if drive.lose_ack:
                    raise TimeoutError('acknowledgment lost')
                return None, {'id': file_id}
        return Upload()

    def get_media(self, fileId):
        return self.content[fileId]


class DriveTests(unittest.TestCase):
    upload = test_app.AppTests.upload
    job = test_app.AppTests.job

    def setUp(self):
        test_app.AppTests.setUp(self)
        from app.database import get_db
        with get_db() as conn:
            conn.executemany('UPDATE projects SET drive_folder_id=? WHERE id=?', [('project-1', 1), ('project-2', 2)])
        self.drive = FakeDrive()
        patch.object(drive_storage, 'storage_connection', return_value=self.drive).start()
        patch.object(dataset_manifest, 'storage_connection', return_value=self.drive).start()

    def source(self, project=1, data=b'Dataset Alpha menyimpan fakta proyek secara persisten.'):
        result = self.upload(project=project, data=data)
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()['dataset']

    def synced(self, project=1):
        source = self.source(project)
        datasets.sync_one(project, source['id'])
        return datasets.get_row(project, source['id'])

    def start(self, ids=None, receipts=None, project=1):
        with patch.object(training_jobs, 'preflight_record', return_value={'verified': True}), patch.object(training_jobs.threading, 'Thread') as thread, patch.object(training_jobs, 'RuntimeBridge') as bridge:
            response = self.client.post('/api/colab/training/start', json={'project_id': project, 'model_name': 'alpha', 'dataset_ids': ids or [], 'source_receipts': receipts or []})
            if thread.called:
                thread.call_args.kwargs['args'][2].close()
            bridge.assert_not_called()
            if response.status_code != 202:
                thread.assert_not_called()
            return response

    def test_upload_sync_verifies_real_server_checksums(self):
        source = self.source()
        states = []
        original = datasets.update
        def record(row, **fields):
            if 'drive_status' in fields:
                states.append(fields['drive_status'])
            return original(row, **fields)
        with patch.object(datasets, 'update', side_effect=record):
            datasets.sync_one(1, source['id'])
        row = datasets.get_row(1, source['id'])
        self.assertEqual(row['drive_status'], 'saved')
        self.assertIn('syncing', states)
        self.assertIn('verifying', states)
        self.assertEqual(states[-1], 'saved')
        manifest = dataset_manifest.verify_selection(1, [row['id']])
        self.assertEqual(manifest['datasets'][0]['file_id'], row['drive_file_id'])
        self.assertEqual(manifest['datasets'][0]['folder_id'], 'datasets-1')

    def test_failed_upload_preserves_local_and_retry_reuses_reserved_id(self):
        source = self.source()
        self.drive.fail_upload = True
        datasets.sync_one(1, source['id'])
        row = datasets.get_row(1, source['id'])
        self.assertEqual(row['drive_status'], 'failed')
        self.assertTrue(datasets.local_path(row).is_file())
        self.assertEqual(self.start([row['id']]).status_code, 409)
        self.drive.fail_upload = False
        datasets.sync_one(1, source['id'], force=True)
        self.assertEqual(datasets.get_row(1, source['id'])['drive_status'], 'saved')
        self.assertEqual(self.drive.generated, 1)
        self.assertEqual(len(self.drive.content), 1)

    def test_ack_loss_retry_never_creates_second_file(self):
        source = self.source()
        self.drive.lose_ack = True
        datasets.sync_one(1, source['id'])
        self.assertEqual(datasets.get_row(1, source['id'])['drive_status'], 'failed')
        self.drive.lose_ack = False
        datasets.sync_one(1, source['id'], force=True)
        self.assertEqual(self.drive.creates, 1)
        self.assertEqual(datasets.get_row(1, source['id'])['drive_status'], 'saved')

    def test_upload_checksum_mismatch_never_marks_saved(self):
        source = self.source()
        actual_get = self.drive.get
        def wrong_checksum(fileId, fields):
            response = actual_get(fileId, fields)
            if fileId.startswith('file-'):
                def corrupt():
                    return {**response.execute(), 'sha256Checksum': '0'*64}
                return Reply(corrupt)
            return response
        with patch.object(self.drive, 'get', side_effect=wrong_checksum):
            datasets.sync_one(1, source['id'])
        row = datasets.get_row(1, source['id'])
        self.assertEqual(row['drive_status'], 'failed')
        self.assertIsNone(row['receipt_id'])
        self.assertTrue(datasets.local_path(row).is_file())

    def test_duplicate_browser_upload_reuses_existing_drive_file(self):
        row = self.synced()
        duplicate = self.source()
        self.assertEqual(duplicate['id'], row['id'])
        datasets.sync_one(1, row['id'])
        self.assertEqual(self.drive.creates, 1)

    def test_remote_dedup_survives_missing_local_upload_journal(self):
        row = self.synced()
        from app.paths import WORKSPACE
        for path in (WORKSPACE / 'drive-uploads').glob('*.json'):
            path.unlink()
        datasets.sync_one(1, row['id'], force=True)
        self.assertEqual(self.drive.creates, 1)

    def test_backend_rejects_each_unverified_state(self):
        row = self.source()
        for state in ('pending', 'syncing', 'verifying', 'failed'):
            datasets.update(row, drive_status=state)
            self.assertEqual(self.start([row['id']]).status_code, 409)
        self.assertFalse(training_jobs.CURRENT.exists())

    def test_backend_rejects_missing_selection_and_cross_project(self):
        self.assertEqual(self.start().status_code, 400)
        row = self.synced(2)
        self.assertEqual(self.start([row['id']], project=1).status_code, 404)
        self.assertEqual(self.start(receipts=[row['receipt_id']], project=1).status_code, 409)

    def test_backend_rejects_one_pending_among_selected(self):
        first = self.synced()
        other = self.source(data=b'File kedua belum disimpan di Drive.')
        self.assertEqual(self.start([first['id'], other['id']]).status_code, 409)

    def test_local_same_size_mutation_blocks_training(self):
        row = self.synced()
        path = datasets.local_path(row)
        path.write_bytes(b'X' * row['size'])
        self.assertEqual(self.start([row['id']]).status_code, 409)
        self.assertEqual(self.drive.creates, 1)

    def test_remote_checksum_folder_size_id_and_deletion_block_training(self):
        for field, value in (('sha256Checksum', '0'*64), ('md5Checksum', '0'*32), ('parents', ['datasets-2']), ('size', '99999'), ('id', 'other-id'), ('trashed', True)):
            with self.subTest(field=field):
                row = self.synced()
                saved = dict(self.drive.metadata[row['drive_file_id']])
                self.drive.metadata[row['drive_file_id']][field] = value
                self.assertEqual(self.start([row['id']]).status_code, 409)
                self.drive.metadata[row['drive_file_id']] = saved
                datasets.update(row, drive_status='saved')

    def test_app_property_hash_alone_never_verifies_bytes(self):
        row = self.synced()
        remote = self.drive.metadata[row['drive_file_id']]
        remote.pop('sha256Checksum')
        remote.pop('md5Checksum')
        self.assertEqual(self.start([row['id']]).status_code, 409)

    def test_md5_api_fallback_checks_actual_local_content(self):
        row = self.synced()
        self.drive.metadata[row['drive_file_id']].pop('sha256Checksum')
        manifest = dataset_manifest.verify_selection(1, [row['id']])
        self.assertEqual(manifest['datasets'][0]['checksum_method'], 'md5Checksum')

    def test_receipt_not_matching_local_source_is_rejected(self):
        row = self.synced()
        from app.colab_stop import receipt_path
        record = json.loads(receipt_path(row['receipt_id']).read_text())
        record['upload']['sha256'] = '0'*64
        atomic_json(receipt_path(row['receipt_id']), record)
        self.assertEqual(self.start([row['id']]).status_code, 409)

    def test_verified_selection_creates_immutable_manifest_before_thread(self):
        row = self.synced()
        response = self.start([row['id']])
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        manifest = dataset_manifest.load(job)
        self.assertEqual(manifest['datasets'][0]['name'], row['name'])
        self.assertEqual(manifest['datasets'][0]['sha256'], row['sha256'])
        with self.assertRaises(FileExistsError):
            dataset_manifest.freeze(job['job_id'], {'changed': True})
        added = self.source(data=b'Dataset lain tidak mengubah manifest run lama.')
        self.assertNotIn(added['id'], json.dumps(dataset_manifest.load(job)))
        self.assertEqual(dataset_manifest.load(job), manifest)

    def test_changed_dataset_after_acceptance_fails_before_runtime(self):
        row = self.synced()
        response = self.start([row['id']])
        job = response.json()
        datasets.local_path(row).write_bytes(b'X'*row['size'])
        with self.assertRaises(Exception):
            dataset_manifest.revalidate(job)

    def test_prepare_rechecks_local_version_before_opening_runtime(self):
        from contextlib import nullcontext
        from app import colab
        row = self.synced()
        job = self.start([row['id']]).json()
        datasets.local_path(row).write_bytes(b'X'*row['size'])
        body = training_jobs.StartTraining(project_id=1, model_name='alpha', dataset_ids=[row['id']])
        handle = (training_jobs.WORK / 'launch.lock').open('a')
        with patch.object(colab, 'runtime_lock', return_value=nullcontext()), patch.object(colab, 'load_state', return_value={'project_id': 1}), patch.object(training_jobs, 'RuntimeBridge') as runtime:
            training_jobs.prepare_job(job, body, handle)
            runtime.assert_not_called()
        self.assertEqual(training_jobs.load_job()['status'], 'failed')

    def test_transfer_time_source_change_blocks_actual_worker_request(self):
        from contextlib import nullcontext
        from app import colab
        row = self.synced()
        job = self.start([row['id']]).json()
        body = training_jobs.StartTraining(project_id=1, model_name='alpha', dataset_ids=[row['id']])
        bridge = MagicMock()
        bridge.identity = ('runtime.example', '/')
        def transfer(*args):
            datasets.local_path(row).write_bytes(b'X'*row['size'])
            return '/tmp/mock-transfer.zip', '0'*64
        handle = (training_jobs.WORK / 'launch.lock').open('a')
        with patch.object(colab, 'runtime_lock', return_value=nullcontext()), patch.object(colab, 'load_state', return_value={'project_id': 1}), patch.object(training_jobs, 'RuntimeBridge', return_value=bridge), patch.object(training_jobs, 'preflight_record', return_value={'runtime_identity': list(bridge.identity)}), patch.object(training_jobs, 'build_bundle', return_value=dataset_manifest.load(job)['datasets']), patch.object(training_jobs, 'transfer', side_effect=transfer):
            training_jobs.prepare_job(job, body, handle)
        bridge.execute.assert_not_called()
        self.assertEqual(training_jobs.load_job()['status'], 'failed')

    def test_tampered_run_manifest_is_rejected(self):
        row = self.synced()
        job = self.start([row['id']]).json()
        from app.paths import WORKSPACE
        (WORKSPACE / 'training-web/manifests' / (job['job_id']+'.json')).write_text('{}')
        with self.assertRaises(ValueError):
            dataset_manifest.load(job)

    def test_download_checksum_failure_blocks_bundle_without_runtime(self):
        row = self.synced()
        manifest = dataset_manifest.verify_selection(1, [row['id']])
        class Downloader:
            def __init__(self, handle, data, chunksize):
                self.handle = handle
            def next_chunk(self, num_retries):
                self.handle.write(b'X'*row['size'])
                return None, True
        body = training_jobs.StartTraining(project_id=1, model_name='alpha', dataset_ids=[row['id']])
        with patch.object(training_jobs, 'storage_connection', return_value=self.drive), patch.object(training_jobs, 'MediaIoBaseDownload', Downloader), patch.object(training_jobs, 'RuntimeBridge') as runtime:
            with self.assertRaises(Exception):
                training_jobs.build_bundle(body, test_app.ROOT/'bundle.zip', manifest)
            runtime.assert_not_called()

    def test_resume_rejects_changed_drive_manifest_before_model_loading(self):
        import argparse
        import sys
        from training import train
        from training.common import MODEL_ID, MODEL_REVISION, script_hash, write_json
        from training.data import dataset_manifest as local_manifest
        root = test_app.ROOT/'resume-fixture'
        data = root/'data'
        data.mkdir(parents=True, exist_ok=True)
        (data/'0000.txt').write_text('Dataset asli untuk uji resume saja.')
        run_id = '20261101T120000Z-1234abcd'
        run = root/'training_results'/run_id
        run.mkdir(parents=True, exist_ok=True)
        original = {'format': 1, 'project_id': 1, 'datasets': [{'file_id': 'original-file'}]}
        settings = {'model_id': MODEL_ID, 'model_revision': MODEL_REVISION, 'model_name': 'alpha',
                    'epochs': 1.0, 'learning_rate': .0001, 'max_length': 512, 'save_steps': 25,
                    'seed': 42, 'sources': local_manifest(data), 'code_sha256': script_hash(), 'dataset_manifest': original}
        write_json(run/'run.json', {'settings': settings})
        changed = root/'changed-manifest.json'
        write_json(changed, {**original, 'datasets': [{'file_id': 'original-file'}, {'file_id': 'added-file'}]})
        args = argparse.Namespace(project_dir=str(root), data_dir=str(data), model_name='alpha', epochs=1.0,
            learning_rate=.0001, max_length=512, save_steps=25, seed=42, resume_run=run_id, dataset_manifest=str(changed))
        with patch.dict(sys.modules, {name: MagicMock() for name in ('torch', 'transformers', 'peft', 'accelerate')}), patch.object(train, 'require_t4', return_value={'name': 'mock T4'}):
            with self.assertRaisesRegex(ValueError, 'Data, kode, model, atau parameter berubah'):
                train.run(args)

    def test_saved_selection_remains_available_after_restart(self):
        row = self.synced()
        from fastapi.testclient import TestClient
        from app.main import app
        restarted = TestClient(app)
        sources = restarted.get('/api/datasets/projects/1').json()
        self.assertEqual(sources[0]['id'], row['id'])
        self.assertEqual(sources[0]['drive_status'], 'saved')
        self.assertEqual(self.start([row['id']]).status_code, 202)
