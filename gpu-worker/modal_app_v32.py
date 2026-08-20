# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 V32 경쟁속도 스테이징 — Modal 앱 (운영·V31과 완전 분리)
# scan CPU(가벼움) / segment GPU(마스크+AI+합성 통합) / finish CPU
# 배포: .github/workflows/modal-deploy-v32-staging.yml (workflow_dispatch 전용)
# ============================================================
import os
import time
import modal

APP_NAME = "inbilab-wm-gpu-v32-speed-staging"
HERE = os.path.dirname(os.path.abspath(__file__))

app = modal.App(APP_NAME)

_local_files = [
    ("handler.py", "/app/handler.py"),
    ("handler_v31.py", "/app/handler_v31.py"),
    ("handler_v32.py", "/app/handler_v32.py"),
    ("pipeline_minimax_remover.py", "/app/pipeline_minimax_remover.py"),
    ("restore_rc4.py", "/app/restore_rc4.py"),
    ("transformer_minimax_remover.py", "/app/transformer_minimax_remover.py"),
]

gpu_image = (
    modal.Image.from_registry("pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(os.path.join(HERE, "requirements.txt"))
    .env({
        "MODEL_DIR": "/models",
        "PYTHONUNBUFFERED": "1",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility,video",
        "WM_BACKEND_NAME": "modal-v32",
        "WM_NPROC": "14",
        "WM_SEG_NPROC": "6",
    })
    .run_commands(
        "python -c \"from huggingface_hub import snapshot_download; "
        "snapshot_download('zibojia/minimax-remover', local_dir='/models', "
        "allow_patterns=['vae/*','transformer/*','scheduler/*'])\""
    )
)
for src, dst in _local_files:
    gpu_image = gpu_image.add_local_file(os.path.join(HERE, src), dst)

cpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("numpy==1.26.4", "opencv-python-headless==4.10.0.84", "requests",
                 "boto3==1.34.162")
    .env({"PYTHONUNBUFFERED": "1", "WM_BACKEND_NAME": "modal-v32",
          "WM_NPROC": "14"})
)
for src, dst in _local_files:
    cpu_image = cpu_image.add_local_file(os.path.join(HERE, src), dst)

_secrets = [modal.Secret.from_name("inbilab-supabase")]
# S3 업로드 키는 finish 전용 시크릿으로만 주입 (범위 최소화 — 사장님 보안 원칙)
_secrets_fin = _secrets + [modal.Secret.from_name("v32-staging-s3")]


def _enter(app_dir="/app"):
    import sys
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    os.chdir(app_dir)


@app.function(image=cpu_image, cpu=16.0, memory=32768,
              timeout=900, secrets=_secrets, scaledown_window=120)
def scan_v32_cpu(event: dict) -> dict:
    _enter()
    t0 = time.time()
    from handler_v32 import handler_v32
    out = handler_v32(event)
    if isinstance(out, dict):
        out["__exec_ms"] = int((time.time() - t0) * 1000)
        out["__fn"] = "scan_v32_cpu"
    return out


@app.function(image=gpu_image, gpu="L40S", cpu=8.0, memory=65536,
              timeout=1800, max_containers=32, retries=0, secrets=_secrets,
              scaledown_window=300)
def segment_v32_gpu(event: dict) -> dict:
    _enter()
    t0 = time.time()
    from handler_v32 import handler_v32
    out = handler_v32(event)
    if isinstance(out, dict):
        out["__exec_ms"] = int((time.time() - t0) * 1000)
        out["__fn"] = "segment_v32_gpu"
    return out


@app.function(image=cpu_image, cpu=8.0, memory=16384,
              timeout=900, secrets=_secrets_fin)
def finish_v32_cpu(event: dict) -> dict:
    _enter()
    t0 = time.time()
    from handler_v32 import handler_v32
    out = handler_v32(event)
    if isinstance(out, dict):
        out["__exec_ms"] = int((time.time() - t0) * 1000)
        out["__fn"] = "finish_v32_cpu"
    return out
