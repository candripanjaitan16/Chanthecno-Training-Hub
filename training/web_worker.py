import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from .common import now, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", required=True)
    args = parser.parse_args()

    job = Path(args.job_dir).resolve()
    config = json.loads(
        (job / "request.json").read_text(encoding="utf-8")
    )

    project = Path("/content/cth/projects") / str(config["project_id"])

    if job.parent != project / "jobs":
        raise ValueError("Lokasi pekerjaan tidak valid.")

    status = job / "job.json"
    stop = job / "stop.request"
    child = None

    def update(phase, message, **extra):
        write_json(
            status,
            {
                "status": phase,
                "message": message,
                "updated_at": now(),
                "job_id": job.name,
                "project_id": config["project_id"],
                **extra,
            },
        )

    def cancelled():
        if stop.exists():
            update(
                "stopped",
                "Dihentikan sebelum optimizer berjalan; "
                "belum ada checkpoint baru.",
            )
            return True
        return False

    with (project / ".cth-web.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            update("failed", "Masih ada worker lain pada proyek ini.")
            return

        finished = threading.Event()

        def heartbeat():
            while not finished.is_set():
                write_json(job / "heartbeat.json", {"job_id": job.name, "updated_at": now()})
                finished.wait(10)

        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()

        try:
            update(
                "preparing",
                "Memeriksa T4 dan menyiapkan dependensi di Colab…",
            )

            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )

            devices = result.stdout.strip().splitlines()

            if not devices or any("T4" not in name for name in devices):
                raise RuntimeError(
                    "GPU bukan T4. Training dibatalkan tanpa fallback CPU."
                )

            if cancelled():
                return

            environment = Path("/tmp/cth-training-env") / job.name

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--without-pip",
                    "--system-site-packages",
                    str(environment),
                ],
                check=True,
            )

            python = environment / "bin/python"

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "--python",
                    str(environment),
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    str(job / "code/requirements-training.txt"),
                ],
                check=True,
                timeout=1200,
            )

            if cancelled():
                return

            env = os.environ.copy()
            env.update(
                PYTHONUNBUFFERED="1",
                HF_HUB_DISABLE_TELEMETRY="1",
                WANDB_DISABLED="true",
                CTH_STOP_FILE=str(stop),
                CTH_JOB_FILE=str(status),
            )

            manifest_file = job / "dataset-manifest.json"
            write_json(manifest_file, config["dataset_manifest"])
            command = [
                str(python),
                "-u",
                "-m",
                "training.train",
                "--project-dir",
                str(project),
                "--data-dir",
                str(job / "data"),
                "--model-name",
                config["model_name"],
                "--epochs",
                str(config["epochs"]),
                "--dataset-manifest",
                str(manifest_file),
                "--max-length",
                "512",
                "--save-steps",
                "25",
            ]

            update(
                "preparing",
                "Memuat Qwen 0.5B dan memvalidasi dataset…",
            )

            child = subprocess.Popen(
                command,
                cwd=job / "code",
                env=env,
            )

            return_code = child.wait()
            current = json.loads(status.read_text(encoding="utf-8"))
            final_status = current.get("status")

            if return_code != 0 and final_status not in {"failed", "stopped"}:
                update(
                    "failed",
                    "Worker training gagal. "
                    "Log tersimpan di folder pekerjaan Colab.",
                )
            elif final_status not in {"completed", "stopped", "failed"}:
                update(
                    "failed",
                    "Worker berakhir tanpa status akhir; "
                    "jangan anggap model selesai.",
                )

        except Exception as error:
            print(
                f"Worker error: {type(error).__name__}: {error}",
                flush=True,
            )

            message = (
                str(error)
                if isinstance(error, RuntimeError)
                else "Persiapan atau training gagal. "
                     "Periksa log pekerjaan; GPU tidak dihentikan oleh worker."
            )

            update("failed", message)

        finally:
            finished.set()
            pulse.join(timeout=2)
            if child is not None and child.poll() is None:
                stop.touch()


if __name__ == "__main__":
    main()
