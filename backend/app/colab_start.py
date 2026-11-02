"""Explicit CLI entry point; uses the same allocation guards as the API."""
import argparse
import json
from starlette.requests import Request
from app.colab import ConnectRequest, connect_colab


def start_t4(project_id):
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 0)})
    return connect_colab(ConnectRequest(project_id=project_id), request)


def main():
    parser = argparse.ArgumentParser(description="Permintaan T4 eksplisit untuk proyek yang dipilih")
    parser.add_argument("--project-id", required=True, type=int)
    args = parser.parse_args()
    try:
        print(json.dumps(start_t4(args.project_id), ensure_ascii=False))
    except Exception:
        parser.exit(1, "Permintaan runtime belum terkonfirmasi. Periksa otorisasi dan status proyek sebelum mencoba lagi.\n")


if __name__ == "__main__":
    main()
