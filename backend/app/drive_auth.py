import argparse

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


from app.paths import CONFIG_HOME, prepare

BASE_DIR = CONFIG_HOME

CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
COLAB_TOKEN_FILE = BASE_DIR / "token_colab.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]

COLAB_SCOPES = [
    "https://www.googleapis.com/auth/colaboratory",
]


def save_credentials(credentials, token_file=TOKEN_FILE):
    import json
    from app.storage import atomic_json
    prepare()
    if token_file.is_symlink():
        raise ValueError("Token berupa symlink; penyimpanan dibatalkan.")
    atomic_json(token_file, json.loads(credentials.to_json()))


def connect_drive(authorize=False):
    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_credentials(credentials)

    if not credentials or not credentials.valid:
        if not authorize:
            raise ValueError("Google Drive belum diotorisasi. Jalankan chanthecno-hub connect-drive.")
        prepare()
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            SCOPES,
        )

        credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        if not credentials.has_scopes(SCOPES) or not credentials.refresh_token:
            raise ValueError("Izin Drive atau refresh token belum tersedia; ulangi otorisasi akun sendiri.")
    service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )
    if authorize:
        service.files().list(pageSize=1, fields="files(id)").execute(num_retries=0)
        save_credentials(credentials)
    return service


def check_drive_connection():
    if not TOKEN_FILE.exists():
        return {
            "status": "disconnected",
            "message": "Google Drive belum dihubungkan",
        }

    try:
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            save_credentials(credentials)

        if not credentials.valid:
            return {
                "status": "disconnected",
                "message": "Silakan hubungkan ulang Google Drive",
            }

        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        service.files().list(
            pageSize=1,
            fields="files(id)",
        ).execute(num_retries=0)

        return {
            "status": "connected",
            "message": "Google Drive terhubung",
        }

    except RefreshError:
        return {
            "status": "disconnected",
            "message": "Silakan hubungkan ulang Google Drive",
        }

    except HttpError as error:
        if error.resp.status == 401:
            return {
                "status": "disconnected",
                "message": "Silakan hubungkan ulang Google Drive",
            }

        return {
            "status": "error",
            "message": "Akses Google Drive belum dapat diverifikasi",
        }

    except (TransportError, OSError):
        return {
            "status": "error",
            "message": "Tidak dapat memeriksa koneksi Google Drive",
        }

    except (ValueError, KeyError):
        return {
            "status": "disconnected",
            "message": "Token Google tidak valid. Hubungkan ulang Google Drive",
        }


# Login Colab hanya dijalankan melalui terminal
def authorize_colab():
    prepare()
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            "credentials.json tidak ditemukan di direktori konfigurasi pengguna CTH."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        COLAB_SCOPES,
    )

    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    if not credentials.has_scopes(COLAB_SCOPES):
        raise ValueError("Izin Colab belum diberikan.")

    if not credentials.refresh_token:
        raise ValueError(
            "Refresh token belum diterima. Ulangi otorisasi Colab."
        )

    verify_colab_access(credentials)
    save_credentials(credentials, COLAB_TOKEN_FILE)


def verify_colab_access(credentials):
    with AuthorizedSession(credentials) as session:
        response = session.get(
            "https://colaboratory.googleapis.com/v1beta/runtimes",
            timeout=30,
            allow_redirects=False,
        )
        try:
            if response.status_code != 200:
                raise ValueError("Akses Colab API belum terverifikasi. Periksa izin OAuth dan akses akun; tidak ada GPU yang diminta.")
            if not isinstance(response.json().get("runtimes", []), list):
                raise ValueError("Respons Colab API tidak valid.")
        finally:
            response.close()


# Dipakai backend tanpa membuka login otomatis
def get_colab_credentials():
    if not COLAB_TOKEN_FILE.exists():
        raise ValueError(
            "Colab belum diotorisasi. Jalankan login Colab dari terminal."
        )

    credentials = Credentials.from_authorized_user_file(
        str(COLAB_TOKEN_FILE)
    )

    if not credentials.has_scopes(COLAB_SCOPES):
        raise ValueError(
            "Token belum memiliki izin Colab. Jalankan ulang login Colab."
        )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_credentials(credentials, COLAB_TOKEN_FILE)

    if not credentials.valid:
        raise ValueError(
            "Token Colab tidak valid. Jalankan ulang login Colab."
        )

    return credentials


def main():
    parser = argparse.ArgumentParser(
        description="Otorisasi Google untuk CTH."
    )

    parser.add_argument(
        "--connect-drive", action="store_true",
        help="Otorisasi akun Google Drive pengguna ini.",
    )
    parser.add_argument(
        "--connect-colab",
        action="store_true",
        help="Memberikan izin akun Google untuk Colab API.",
    )

    args = parser.parse_args()

    try:
        if args.connect_colab:
            authorize_colab()
            print("Otorisasi dan akses Colab API terverifikasi. Runtime belum dibuat dan training belum dijalankan.")
        elif args.connect_drive:
            prepare()
            service = connect_drive(authorize=True)
            service.files().list(pageSize=1, fields="files(id)").execute(num_retries=0)
            print("Akses Google Drive terverifikasi untuk pengguna ini.")
        else:
            print(check_drive_connection())
    except Exception:
        parser.exit(1, "Otorisasi belum berhasil. Periksa konfigurasi OAuth, izin akun, dan jaringan; tidak ada runtime yang dibuat.\n")


if __name__ == "__main__":
    main()
