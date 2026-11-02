"""Explicit GPU check using the shared Colab/Jupyter connection lifecycle."""
import json
from app.colab import RUNTIME_NAME, T4_SPEC
from app.colab_bridge import RuntimeBridge
from app.model_testing import inference_lock


GPU_TEST_CODE = """
import json
import subprocess

try:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )

    devices = []

    for line in result.stdout.strip().splitlines():
        name, memory = line.rsplit(",", 1)

        devices.append({
            "name": name.strip(),
            "memory_mib": int(memory.strip()),
        })

    print("CTH_CONTROL:" + json.dumps({
        "ok": bool(devices),
        "devices": devices,
    }))

except Exception as error:
    print("CTH_CONTROL:" + json.dumps({
        "ok": False,
        "error_type": type(error).__name__,
    }))
"""



def check_gpu():
    with inference_lock():
        bridge = RuntimeBridge(RUNTIME_NAME, T4_SPEC)
        try:
            bridge.require_idle_other_kernels()
            result = bridge.execute(GPU_TEST_CODE, timeout=60)
            devices = result.get("devices", [])
            if not devices or any("T4" not in item.get("name", "").upper() for item in devices):
                raise ValueError("GPU belum terverifikasi sebagai T4.")
            return {"status": "success", "gpu_verified": True, "devices": devices}
        finally:
            bridge.close()


if __name__ == "__main__":
    try:
        print(json.dumps(check_gpu(), ensure_ascii=False))
    except Exception:
        raise SystemExit("Pemeriksaan T4 belum berhasil. Periksa koneksi dan status pekerjaan; tidak ada runtime baru yang diminta.")
