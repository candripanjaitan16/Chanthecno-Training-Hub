"""Real GitHub-checkout install/reinstall/start/uninstall smoke; uses disposable XDG paths.

Run explicitly with python3 tests/smoke_install.py. Requires package-registry access.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='cth-install-smoke-') as temporary:
        root = Path(temporary)
        environment = os.environ.copy()
        environment.update(XDG_DATA_HOME=str(root/'share'), XDG_CONFIG_HOME=str(root/'config'), XDG_STATE_HOME=str(root/'state'), CTH_BIN_DIR=str(root/'bin'))
        for key in ('CTH_DATA_DIR','CTH_CONFIG_DIR','CTH_STATE_DIR'):
            environment.pop(key,None)
        log = root/'install.log'
        def install(*args, success=True):
            with log.open('a') as output:
                result=subprocess.run([sys.executable,str(REPO/'tools/install.py'),*args],env=environment,stdout=output,stderr=subprocess.STDOUT)
            if success and result.returncode:
                raise AssertionError(log.read_text()[-6000:])
            if not success:
                assert result.returncode != 0
        install()
        program=root/'share/chanthecno-hub/program'
        command=root/'bin/chanthecno-hub'
        first=(program/'current').resolve()
        assert command.is_symlink()
        assert (first/'frontend/dist/index.html').is_file()
        assert not (first/'frontend/node_modules').exists()
        assert (first/'requirements-training.txt').is_file()
        assert (first/'training/retrieval.py').is_file()
        assert (first/'backend/app/source_analysis.py').is_file()
        assert (first/'backend/app/dataset_manifest.py').is_file()
        installed_version=subprocess.check_output([str(first/'backend/.venv/bin/python'),'-c','import sys,json; print(json.dumps(list(sys.version_info[:3])))'],text=True)
        assert json.loads(installed_version)==list(sys.version_info[:3])
        print('PASS installed backend interpreter matches installer:',sys.version.split()[0],flush=True)
        assert not list(first.rglob('token*.json'))
        data=root/'share/chanthecno-hub/data'
        data.mkdir(parents=True,exist_ok=True)
        (data/'user-data.txt').write_text('preserve')
        config=root/'config/chanthecno-hub'
        (config/'user-settings.txt').write_text('preserve settings')
        unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(180)'])
        launcher=None
        server_log=root/'server.log'
        try:
            with server_log.open('w') as output:
                launcher=subprocess.Popen([str(command),'--no-browser'],env=environment,stdout=output,stderr=subprocess.STDOUT)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline=time.monotonic()+30
            while True:
                if launcher.poll() is not None:
                    raise AssertionError(server_log.read_text())
                try:
                    with opener.open('http://127.0.0.1:8000/api/hello',timeout=1) as response:
                        assert json.load(response)['message']=='ChanThecno Training Hub Run!'
                    break
                except OSError:
                    if time.monotonic()>deadline:
                        raise AssertionError(server_log.read_text())
                    time.sleep(.2)
            with opener.open('http://127.0.0.1:8000/api/projects/') as response:
                assert json.load(response)==[]
            with opener.open('http://127.0.0.1:8000/') as response:
                html=response.read().decode()
                assert '<div id="root">' in html
            asset=re.search(r'src="([^"]+\.js)"',html).group(1)
            with opener.open('http://127.0.0.1:8000'+asset) as response:
                assert response.status==200 and len(response.read())>10000
            install(success=False)
            assert (program/'current').resolve()==first
            launcher.send_signal(signal.SIGTERM)
            assert launcher.wait(timeout=15)==0
            assert unrelated.poll() is None
            print('PASS production launcher, static assets, update lock, SIGTERM, unrelated process retained')
            try:
                opener.open('http://127.0.0.1:8000/api/hello',timeout=1)
            except OSError:
                pass
            else:
                raise AssertionError('Owned backend still running after exit')
        finally:
            if launcher and launcher.poll() is None:
                launcher.terminate()
                launcher.wait(timeout=15)
            unrelated.terminate()
            unrelated.wait(timeout=5)
        install()
        assert (program/'current').resolve()!=first
        assert first.is_dir()
        assert (data/'user-data.txt').read_text()=='preserve'
        assert (config/'user-settings.txt').read_text()=='preserve settings'
        result=subprocess.run([str(command),'uninstall'],env=environment,capture_output=True,text=True)
        assert result.returncode==0, result.stderr
        assert not command.is_symlink()
        assert not (program/'releases').exists()
        assert (data/'user-data.txt').read_text()=='preserve'
        assert (config/'user-settings.txt').read_text()=='preserve settings'
        print('PASS real install, reinstall, preserved data/configuration, uninstall without sudo')


if __name__=='__main__':
    main()
