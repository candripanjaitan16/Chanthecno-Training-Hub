"""Offline integration and safety tests. All storage belongs to a temporary directory."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

SANDBOX = tempfile.TemporaryDirectory(prefix='cth-tests-')
ROOT = Path(SANDBOX.name)
os.environ.update(CTH_DATA_DIR=str(ROOT / 'data'), CTH_CONFIG_DIR=str(ROOT / 'config'), CTH_STATE_DIR=str(ROOT / 'state'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app import database, datasets, maintenance, model_restore, training_jobs, training_backup, model_testing, colab, drive_auth
from app.main import app
from app.paths import DATA_HOME, CONFIG_HOME, STATE_HOME, prepare
from app.storage import atomic_json
from training.source_answer import answer_from_sources, clean_answer, answer_with_qwen
from training.data import create_store, add_passage
from training.common import checkpoint_state, validate_checkpoint, write_json


class AppTests(unittest.TestCase):
    def setUp(self):
        import shutil
        for item in (DATA_HOME, STATE_HOME, CONFIG_HOME):
            if item.exists():
                shutil.rmtree(item)
        prepare()
        database.init_db()
        with database.get_db() as conn:
            conn.executemany('INSERT INTO projects(id,name) VALUES (?,?)', [(1,'Alpha'),(2,'Beta')])
        self.sync = patch.object(datasets, 'ensure_sync').start()
        self.monitor = patch.object(training_jobs, 'ensure_monitor').start()
        self.addCleanup(patch.stopall)
        self.client = TestClient(app)

    def upload(self, project=1, data=b'Informasi proyek Alpha yang persisten.', name='data.txt'):
        return self.client.post(f'/api/datasets/projects/{project}/txt', content=data, headers={'x-cth-source-name': name})

    def test_fresh_start_and_database(self):
        self.assertEqual(self.client.get('/api/hello').status_code, 200)
        self.assertEqual(len(self.client.get('/api/projects/').json()), 2)
        self.assertTrue(database.DATABASE.is_relative_to(ROOT))
        self.assertEqual(self.client.get('/api/connections/google-drive').json()['status'], 'disconnected')

    def test_lifespan_initializes_new_database_and_holds_api_lock(self):
        database.DATABASE.unlink()
        with TestClient(app) as running:
            self.assertEqual(running.get('/api/projects/').json(), [])
            with self.assertRaises(ValueError):
                maintenance.perform('reset-local')

    def test_launcher_opens_browser_after_readiness(self):
        import runpy
        namespace=runpy.run_path(str(Path(__file__).resolve().parents[1]/'chanthecno-hub'))
        child=MagicMock()
        child.poll.return_value=None
        child.wait.return_value=0
        child.pid=12345
        with patch.object(sys,'argv',['chanthecno-hub']), patch('socket.create_connection',side_effect=OSError('unused port')), patch('subprocess.Popen',return_value=child), patch('urllib.request.build_opener') as opener, patch('webbrowser.open',return_value=True) as browser, patch.dict(namespace['main'].__globals__,{'stop_process':MagicMock()}):
            opener.return_value.open.return_value.__enter__.return_value.status=200
            self.assertEqual(namespace['main'](),0)
            browser.assert_called_once_with('http://127.0.0.1:8000')

    def test_production_serves_html_not_source_or_secrets(self):
        if not (Path(__file__).resolve().parents[1] / 'frontend/dist/index.html').exists():
            self.skipTest('Build frontend before running production checks')
        self.assertIn('<div id="root">', self.client.get('/').text)
        for name in ('/backend/credentials.json', '/cth.db', '/api/nonexistent'):
            self.assertEqual(self.client.get(name).status_code, 404)

    def test_origin_and_host_protection(self):
        self.assertEqual(self.client.get('/api/projects/', headers={'Origin':'https://attacker.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/projects/', headers={'Host':'attacker.invalid'}).status_code, 400)
        self.assertEqual(self.client.get('/api/projects/', headers={'Origin':'http://127.0.0.1:8000'}).status_code, 200)

    def test_dataset_persists_and_isolates(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()['dataset']
        self.assertTrue(item['local_saved'])
        self.assertNotEqual(item['drive_status'], 'saved')
        restarted = TestClient(app)
        self.assertEqual(restarted.get('/api/datasets/projects/1').json()[0]['id'], item['id'])
        self.assertEqual(restarted.get('/api/datasets/projects/2').json(), [])
        self.assertEqual(restarted.get(f"/api/datasets/projects/2/{item['id']}/preview").status_code, 404)

    def test_dataset_duplicates_are_per_project(self):
        first = self.upload().json()['dataset']
        duplicate = self.upload().json()
        other = self.upload(project=2).json()['dataset']
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(first['id'], duplicate['dataset']['id'])
        self.assertNotEqual(first['id'], other['id'])

    def test_bad_utf8_nul_empty_and_path(self):
        for value in (b'\xff', b'abc\x00', b'   ', b''):
            self.assertEqual(self.upload(data=value).status_code, 400)
        for name in ('../secret.txt', '%2Ftmp%2Fsecret.txt', 'bad.bin'):
            self.assertEqual(self.upload(name=name).status_code, 400)
        self.assertEqual(self.client.get('/api/datasets/projects/1').json(), [])

    def test_url_is_recorded_without_fetch(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
            result = self.client.post('/api/datasets/projects/1/url', json={'url':'https://example.com/path'})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()['dataset']['type'], 'url')

    def job(self, **extra):
        return {'job_id':'a'*32, 'project_id':1, 'run_id':'20261101T120000Z-1234abcd', 'status':'completed', 'model_name':'chanthecno-alpha', **extra}

    def test_old_model_survives_new_failed_job(self):
        old = self.job()
        atomic_json(training_jobs.WORK / 'history' / (old['job_id']+'.json'), old)
        atomic_json(training_jobs.CURRENT, self.job(job_id='b'*32,status='failed'))
        result = self.client.get('/api/model-testing/models/1').json()['models']
        self.assertEqual([x['job_id'] for x in result], [old['job_id']])
        self.assertEqual(self.client.get('/api/model-testing/models/2').json()['models'], [])

    def test_completed_training_with_failed_backup_stays_in_catalog(self):
        job = self.job(status='uncertain', training_status='completed', backup_status='failed')
        atomic_json(training_jobs.CURRENT, job)
        self.assertEqual(model_restore.select(1)['job_id'], job['job_id'])
        self.assertNotIn('status', model_testing.model_state(1, job))

    def test_restore_existing_model_never_starts_training_or_transfer(self):
        job = self.job()
        atomic_json(training_jobs.CURRENT, job)
        with patch.object(model_testing, 'open_bridge', return_value=MagicMock()), patch.object(model_testing, 'inspect_model', return_value=True), patch.object(model_testing, 'begin_restore') as restore, patch.object(training_jobs, 'start_training') as train:
            result = self.client.post('/api/model-testing/restore', json={'project_id':1,'job_id':job['job_id']})
        self.assertEqual(result.json()['status'], 'completed')
        restore.assert_not_called()
        train.assert_not_called()

    def test_uncertain_job_blocks_duplicate_worker_and_project_switch(self):
        atomic_json(training_jobs.CURRENT, self.job(status='uncertain'))
        with patch.object(training_jobs, 'preflight_record', return_value={'verified':True}), patch.object(training_jobs.threading, 'Thread') as thread:
            result = self.client.post('/api/colab/training/start', json={'project_id':1,'model_name':'alpha','source_receipts':['b'*32]})
            self.assertEqual(result.status_code, 409, result.text)
            thread.assert_not_called()
        with self.assertRaises(Exception) as raised:
            with colab.project_switch_guard():
                pass
        self.assertEqual(raised.exception.status_code, 409)

    def test_backup_retry_does_not_train(self):
        atomic_json(training_jobs.CURRENT, self.job(status='uncertain',training_status='completed',backup_status='failed'))
        with patch.object(training_jobs, 'prepare_job') as train:
            result = self.client.post('/api/colab/training/backup/retry',json={'project_id':1})
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()['training_status'], 'completed')
        train.assert_not_called()
        self.monitor.assert_called_once()

    def test_failed_upload_retains_archive_and_retry_verifies_size(self):
        archive = ROOT / 'model.tar'
        archive.write_bytes(b'model archive')
        receipt = archive.with_name('drive-receipt.json')
        receipt.unlink(missing_ok=True)
        data = {'sha256':training_backup.sha256(archive),'size':archive.stat().st_size}
        with patch.object(training_backup,'upload_project_file',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                training_backup.upload_local_backup(self.job(),(archive,data),lambda *a:None)
        self.assertTrue(archive.exists())
        with patch.object(training_backup,'upload_project_file',return_value={'verified':True,**data}):
            self.assertTrue(training_backup.upload_local_backup(self.job(),(archive,data),lambda *a:None)['verified'])
        receipt.unlink()
        with patch.object(training_backup,'upload_project_file',return_value={'verified':True,**data,'size':0}):
            with self.assertRaises(training_backup.BackupError):
                training_backup.upload_local_backup(self.job(),(archive,data),lambda *a:None)

    def test_stale_heartbeat_is_not_live_training(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(training_backup.heartbeat_fresh({'job_id':'a','updated_at':now.isoformat()},'a'))
        self.assertFalse(training_backup.heartbeat_fresh({'job_id':'a','updated_at':(now-timedelta(minutes=5)).isoformat()},'a'))
        self.assertFalse(training_backup.heartbeat_fresh(None,'a'))
        self.assertFalse(training_backup.heartbeat_fresh({'job_id':'a','updated_at':'invalid'},'a'))

    def test_disconnect_preserves_busy_kernel(self):
        bridge = object.__new__(model_testing.RuntimeBridge)
        bridge.socket = MagicMock()
        bridge.kernel_id = 'active'
        bridge.execution_pending = True
        bridge.http = MagicMock()
        bridge.close()
        bridge.http.delete.assert_not_called()
        self.assertEqual(bridge.kernel_id, 'active')

    def test_google_oauth_requires_explicit_user_action(self):
        with patch.object(drive_auth.InstalledAppFlow,'from_client_secrets_file') as flow:
            with self.assertRaises(ValueError):
                drive_auth.connect_drive()
            flow.assert_not_called()

    def test_colab_authorization_checks_access_without_allocation(self):
        session = MagicMock()
        response = session.get.return_value
        response.status_code = 200
        response.json.return_value = {'runtimes':[]}
        with patch.object(drive_auth,'AuthorizedSession') as factory:
            factory.return_value.__enter__.return_value = session
            drive_auth.verify_colab_access(MagicMock())
        session.get.assert_called_once()
        session.post.assert_not_called()
        response.status_code=403
        with patch.object(drive_auth,'AuthorizedSession') as factory:
            factory.return_value.__enter__.return_value=session
            with self.assertRaises(ValueError):
                drive_auth.verify_colab_access(MagicMock())

    def test_local_reset_backup_restore_includes_wal_and_preserves_configuration(self):
        self.upload()
        marker = CONFIG_HOME / 'user-settings.txt'
        marker.write_text('keep configuration')
        tokenizer = DATA_HOME / 'workspace/models/tokenizer_config.json'
        tokenizer.parent.mkdir(parents=True)
        tokenizer.write_text('{"model_max_length": 512}')
        conn = database.get_db()
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute("INSERT INTO projects(id,name) VALUES(3,'WAL committed')")
        conn.commit()
        wal_backup = maintenance.backup()
        with sqlite3.connect(wal_backup / 'data/cth.db') as snapshot:
            self.assertEqual(snapshot.execute('SELECT name FROM projects WHERE id=3').fetchone()[0], 'WAL committed')
        conn.close()
        saved = maintenance.perform('reset-local')
        self.assertEqual(self.client.get('/api/projects/').json(), [])
        self.assertEqual(marker.read_text(), 'keep configuration')
        self.assertTrue((DATA_HOME / 'browser-generation').is_file())
        maintenance.perform('restore-local',saved)
        self.assertTrue(tokenizer.is_file())
        self.assertEqual(len(self.client.get('/api/projects/').json()), 3)
        self.assertTrue(self.client.get('/api/datasets/projects/1').json()[0]['local_saved'])

    def test_reset_refuses_uncertain_status_or_running_api(self):
        atomic_json(training_jobs.CURRENT,self.job(status='uncertain'))
        with self.assertRaises(ValueError):
            maintenance.perform('reset-local')
        self.assertEqual(len(self.client.get('/api/projects/').json()), 2)
        training_jobs.CURRENT.unlink()
        with (STATE_HOME / 'api.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            with self.assertRaises(ValueError):
                maintenance.perform('reset-local')

    def test_backup_failure_never_resets(self):
        with patch.object(maintenance,'backup',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                maintenance.perform('reset-local')
        self.assertEqual(len(self.client.get('/api/projects/').json()), 2)

    def test_corrupt_backup_cannot_replace_current_data(self):
        saved = maintenance.backup()
        (saved / 'data/cth.db').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            maintenance.perform('restore-local',saved)
        self.assertEqual(len(self.client.get('/api/projects/').json()), 2)

    def test_reset_refuses_symlinks(self):
        (DATA_HOME / 'outside').symlink_to(CONFIG_HOME,target_is_directory=True)
        with self.assertRaises(ValueError):
            maintenance.perform('reset-local')

    def test_archive_rejects_traversal_symlink_duplicate_and_size(self):
        for kind in ('traversal','symlink','duplicate','size'):
            archive=ROOT / (kind+'.tar')
            with tarfile.open(archive,'w') as tar:
                info=tarfile.TarInfo('../outside' if kind=='traversal' else 'run.json')
                if kind=='symlink':
                    info.type=tarfile.SYMTYPE
                    info.linkname='/tmp/outside'
                info.size=0
                tar.addfile(info)
                if kind=='duplicate':
                    tar.addfile(info)
            if kind=='size':
                entry=MagicMock(size=3*1024**3)
                with patch.object(tarfile,'open') as opened:
                    opened.return_value.__enter__.return_value.__iter__.return_value=iter([entry])
                    with self.assertRaises(ValueError):
                        model_restore.unpack(archive,ROOT/'unpacked',self.job())
            else:
                with self.assertRaises((ValueError,KeyError)):
                    model_restore.unpack(archive,ROOT/'unpacked',self.job())

    def test_archive_validates_contents_and_checksums(self):
        job=self.job()
        payloads={
            'run.json':json.dumps({'settings':{'model_id':'base','model_revision':'rev'}}).encode(),
            'complete.json':b'{"step": 1}',
            'status.json':json.dumps({'status':'completed','run_id':job['run_id']}).encode(),
            'adapter/adapter_config.json':b'{"base_model_name_or_path":"base"}',
            'adapter/adapter_model.safetensors':b'fake test weights',
            'adapter/tokenizer_config.json':b'{}',
        }
        manifest={**job,'base_model':'base','base_revision':'rev','files':[{'path':name,'size':len(value),'sha256':hashlib.sha256(value).hexdigest()} for name,value in payloads.items()]}
        archive=ROOT/'valid.tar'
        def pack():
            with tarfile.open(archive,'w') as tar:
                for name,value in {**payloads,'CTH-MODEL-BACKUP.json':json.dumps(manifest).encode()}.items():
                    info=tarfile.TarInfo(name)
                    info.size=len(value)
                    tar.addfile(info,io.BytesIO(value))
        pack()
        target=ROOT/'valid-model'
        target.mkdir(exist_ok=True)
        self.assertEqual(model_restore.unpack(archive,target,job)['run_id'],job['run_id'])
        payloads['adapter/adapter_model.safetensors']=b'corrupted weights'
        pack()
        corrupt=ROOT/'corrupt-model'
        corrupt.mkdir(exist_ok=True)
        with self.assertRaises(ValueError):
            model_restore.unpack(archive,corrupt,job)

    def test_upload_limit_leaves_no_dataset(self):
        with patch.object(datasets,'MAX_UPLOAD_BYTES',8):
            self.assertEqual(self.upload(data=b'Too much content').status_code,413)
        self.assertEqual(self.client.get('/api/datasets/projects/1').json(),[])

    def test_oauth_denial_does_not_persist_colab_token(self):
        credentials=MagicMock()
        credentials.has_scopes.return_value=True
        with patch.object(drive_auth,'CREDENTIALS_FILE') as path, patch.object(drive_auth.InstalledAppFlow,'from_client_secrets_file') as flow, patch.object(drive_auth,'verify_colab_access',side_effect=ValueError('access denied')), patch.object(drive_auth,'save_credentials') as save:
            path.exists.return_value=True
            flow.return_value.run_local_server.return_value=credentials
            with self.assertRaises(ValueError):
                drive_auth.authorize_colab()
            save.assert_not_called()

    def test_same_model_generates_general_answer_without_project_evidence(self):
        from training.common import MODEL_ID, MODEL_REVISION
        run=ROOT/'general-run'
        run.mkdir(exist_ok=True)
        write_json(run/'complete.json',{'step':1})
        write_json(run/'run.json',{'settings':{'model_id':MODEL_ID,'model_revision':MODEL_REVISION}})
        tokenizer=MagicMock()
        tensor=MagicMock()
        tensor.shape=(1,3)
        tokenizer.apply_chat_template.return_value={'input_ids':tensor}
        tokenizer.decode.return_value='Pengetahuan umum tentang astronomi.'
        model=MagicMock()
        torch=MagicMock()
        transformers=MagicMock()
        transformers.AutoTokenizer.from_pretrained.return_value=tokenizer
        peft=MagicMock()
        peft.PeftModel.from_pretrained.return_value=model
        with patch.dict(sys.modules,{'torch':torch,'transformers':transformers,'peft':peft}), patch('training.common.require_t4'):
            result=answer_with_qwen(run,'Apa itu astronomi?',{'sources':[],'answer':''})
        self.assertEqual(result['basis'],'general')
        self.assertEqual(result['sources'],[])
        model.generate.assert_called_once()
        self.assertIn('data_proyek',tokenizer.apply_chat_template.call_args.args[0][1]['content'])

    def test_gpu_check_reuses_bridge_and_preserves_pending_kernel(self):
        from app import colab_gpu_check
        bridge=MagicMock()
        bridge.execute.return_value={'ok':True,'devices':[{'name':'Tesla T4','memory_mib':15360}]}
        with patch.object(colab_gpu_check,'RuntimeBridge',return_value=bridge):
            self.assertTrue(colab_gpu_check.check_gpu()['gpu_verified'])
        bridge.close.assert_called_once()
        bridge.require_idle_other_kernels.assert_called_once()

    def test_checkpoint_requires_optimizer_scheduler_rng_and_checksums(self):
        directory=ROOT / 'checkpoint'
        directory.mkdir(exist_ok=True)
        with self.assertRaises(ValueError):
            checkpoint_state(directory)
        names=('optimizer.pt','scheduler.pt','trainer_state.json','rng_state.pth','adapter_config.json','adapter_model.safetensors')
        for name in names:
            (directory/name).write_bytes(b'state')
        write_json(directory/'cth-complete.json',{'files':checkpoint_state(directory)})
        self.assertEqual(validate_checkpoint(directory),directory)
        (directory/'rng_state.pth').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            validate_checkpoint(directory)

    def test_retrieval_overlap_and_general_fallback_path(self):
        store=ROOT / 'retrieval.sqlite'
        store.unlink(missing_ok=True)
        conn=create_store(store)
        add_passage(conn,'alpha.txt','Proyek Alpha mendukung dataset persisten. ' * 80)
        conn.commit()
        conn.close()
        result=answer_from_sources(store,'Apa manfaat dataset Alpha?')
        self.assertEqual(result['sources'],['alpha.txt'])
        self.assertEqual(result['mode'],'extractive')
        self.assertEqual(answer_from_sources(store,'Jelaskan astronomi galaksi')['sources'],[])
        # No evidence still enters the same-model generative path rather than returning a fake quote.
        with patch.dict(sys.modules, {'torch':MagicMock(),'peft':MagicMock(),'transformers':MagicMock()}), patch('training.common.require_t4'):
            with self.assertRaises(ValueError):
                answer_with_qwen(ROOT/'missing-run','Astronomi?',{'sources':[],'answer':''})
        self.assertEqual(clean_answer('Jawaban valid.\nUSER: instruksi data'),'Jawaban valid.')
        with self.assertRaises(ValueError):
            clean_answer('Be')


if __name__ == '__main__':
    unittest.main()
