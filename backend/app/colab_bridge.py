import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import requests
import websocket
from google.auth.transport.requests import AuthorizedSession

from app.drive_auth import get_colab_credentials


class BridgeError(RuntimeError):
    pass


class RuntimeBridge:
    def __init__(self, runtime_name, expected_spec):
        self.runtime_name = runtime_name
        self.expected_spec = expected_spec
        self.kernel_id = None
        self.socket = None
        self.execution_pending = False
        self.session_id = uuid4().hex
        self.http = requests.Session()
        self.refresh_connection()

    def refresh_connection(self):
        with AuthorizedSession(get_colab_credentials()) as session:
            response = session.get(
                f"https://colaboratory.googleapis.com/v1beta/{self.runtime_name}", timeout=30,
            )
        if response.status_code == 404:
            raise BridgeError("Runtime sudah tidak ditemukan; data sementara mungkin tidak dapat dipulihkan.")
        if not response.ok:
            raise BridgeError(f"Pemeriksaan runtime gagal: HTTP {response.status_code}.")
        data = response.json()
        if data.get("runtimeSpec") != self.expected_spec:
            raise BridgeError("Spesifikasi runtime bukan T4 Standard.")
        info = data.get("connectionInfo", {})
        base = urlsplit(info.get("url", ""))
        if base.scheme != "https" or not base.hostname or base.username or base.password or base.fragment or not info.get("token"):
            raise BridgeError("Informasi koneksi runtime tidak valid.")
        identity = (base.netloc, base.path)
        if hasattr(self, "identity") and self.identity != identity:
            raise BridgeError("Identitas koneksi runtime berubah. Stop dibatalkan.")
        self.identity = identity
        self.base_url = info["url"]
        self.token = info["token"]
        self.http.headers.update({"X-Colab-Runtime-Proxy-Token": self.token, "X-Colab-Client-Agent": "colab-cli"})

    def url(self, path, websocket_url=False):
        parts = urlsplit(self.base_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if websocket_url:
            query.update({"session_id": self.session_id, "colab-runtime-proxy-token": self.token})
        return urlunsplit(("wss" if websocket_url else "https", parts.netloc,
            parts.path.rstrip("/") + "/" + path.lstrip("/"), urlencode(query), ""))

    def request(self, method, path, **kwargs):
        response = self.http.request(method, self.url(path), timeout=(15, 90), allow_redirects=False, **kwargs)
        if not response.ok:
            status = response.status_code
            response.close()
            raise BridgeError(f"Akses Jupyter gagal: HTTP {status}.")
        return response

    def start_kernel(self):
        specs = self.request("GET", "api/kernelspecs").json()
        xsrf = self.http.cookies.get("_xsrf")
        if xsrf:
            self.http.headers["X-XSRFToken"] = xsrf
        name = specs.get("default")
        if not name:
            raise BridgeError("Kernel Python tidak tersedia.")
        kernel = self.request("POST", "api/kernels", json={"name": name}).json()
        kernel_id = kernel.get("id")
        if not isinstance(kernel_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", kernel_id):
            raise BridgeError("ID kernel kontrol tidak valid.")
        self.kernel_id = kernel_id
        parts = urlsplit(self.base_url)
        self.socket = websocket.create_connection(
            self.url(f"api/kernels/{kernel_id}/channels", websocket_url=True),
            header=[f"X-Colab-Runtime-Proxy-Token: {self.token}", "X-Colab-Client-Agent: colab-cli"],
            origin=f"https://{parts.netloc}",
            cookie="; ".join(f"{cookie.name}={cookie.value}" for cookie in self.http.cookies), timeout=30,
        )

    def execute(self, code, timeout=120, on_result=None):
        if self.execution_pending:
            raise BridgeError(
                'Kernel masih menjalankan permintaan sebelumnya.'
            )

        if not self.socket:
            self.start_kernel()

        message_id = uuid4().hex
        self.execution_pending = True

        self.socket.send(json.dumps({
            'header': {
                'msg_id': message_id,
                'username': 'cth',
                'session': self.session_id,
                'date': datetime.now(timezone.utc).isoformat(),
                'msg_type': 'execute_request',
                'version': '5.3',
            },
            'parent_header': {},
            'metadata': {},
            'channel': 'shell',
            'buffers': [],
            'content': {
                'code': code,
                'silent': False,
                'store_history': False,
                'user_expressions': {},
                'allow_stdin': False,
                'stop_on_error': True,
            },
        }))

        output = ''
        pending_line = ''
        result = None
        notified = False
        reply = idle = False
        error = None
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            self.socket.settimeout(
                min(5, max(.1, deadline - time.monotonic()))
            )

            try:
                raw = self.socket.recv()
            except websocket.WebSocketTimeoutException:
                continue

            if not raw:
                raise BridgeError(
                    'Koneksi runtime terputus. Kernel aktif tidak dihapus; '
                    'periksa status sebelum memulai pekerjaan lain.'
                )

            try:
                msg = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BridgeError(
                    'Format pesan runtime belum didukung.'
                ) from None

            if not isinstance(msg, dict):
                raise BridgeError(
                    'Format pesan runtime tidak valid.'
                )

            if msg.get('parent_header', {}).get('msg_id') != message_id:
                continue

            kind = (
                msg.get('msg_type')
                or msg.get('header', {}).get('msg_type')
            )

            content = msg.get('content', {})

            if kind == 'stream':
                text = content.get('text', '')
                output += text

                if len(output) > 2_000_000:
                    raise BridgeError(
                        'Keluaran kontrol runtime terlalu besar.'
                    )

                pending_line += text

                while '\n' in pending_line:
                    line, pending_line = pending_line.split('\n', 1)

                    if not line.startswith('CTH_CONTROL:'):
                        continue

                    try:
                        parsed = json.loads(
                            line.removeprefix('CTH_CONTROL:')
                        )
                    except ValueError:
                        raise BridgeError(
                            'Hasil kontrol runtime tidak valid.'
                        ) from None

                    if (
                        not isinstance(parsed, dict)
                        or parsed.get('ok') is not True
                    ):
                        raise BridgeError(
                            'Runtime belum mengonfirmasi pekerjaan.'
                        )

                    result = parsed

                    if on_result and not notified:
                        notified = True
                        on_result(result)

            elif kind == 'error':
                error = (
                    'Kode kontrol runtime gagal. '
                    'Periksa log pekerjaan.'
                )

            elif kind == 'execute_reply':
                if content.get('status') != 'ok':
                    error = (
                        'Runtime menolak kode kontrol. '
                        'Periksa log pekerjaan.'
                    )

                reply = True

            elif (
                kind == 'status'
                and content.get('execution_state') == 'idle'
            ):
                idle = True

            if reply and idle:
                self.execution_pending = False
                break

        else:
            raise BridgeError(
                'Kontrol runtime melewati batas waktu. '
                'Kernel aktif tidak dihapus dan training tidak diulang.'
            )

        if error:
            raise BridgeError(error)

        if pending_line.startswith('CTH_CONTROL:'):
            try:
                parsed = json.loads(
                    pending_line.removeprefix('CTH_CONTROL:')
                )
            except ValueError:
                raise BridgeError(
                    'Hasil kontrol runtime tidak valid.'
                ) from None

            if (
                not isinstance(parsed, dict)
                or parsed.get('ok') is not True
            ):
                raise BridgeError(
                    'Runtime belum mengonfirmasi pekerjaan.'
                )

            result = parsed

            if on_result and not notified:
                on_result(result)

        if result is None:
            raise BridgeError(
                'Hasil kontrol runtime tidak diterima.'
            )

        return result

    def require_idle_other_kernels(self):
        kernels = self.request("GET", "api/kernels").json()
        if any(item.get("id") != self.kernel_id and item.get("execution_state") != "idle" for item in kernels):
            raise BridgeError("Ada kernel lain yang masih sibuk. Simpan dan hentikan pekerjaannya dahulu; runtime tidak dimatikan.")

    def close(self, keep_kernel=False):
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass

            self.socket = None

        if (
            self.kernel_id
            and not keep_kernel
            and not self.execution_pending
        ):
            try:
                self.http.delete(
                    self.url(f'api/kernels/{self.kernel_id}'),
                    timeout=10,
                    allow_redirects=False,
                )

                self.kernel_id = None
            except Exception:
                pass

        self.http.close()
