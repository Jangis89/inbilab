# 인비랩 GPU 일꾼 — RunPod serverless (RTX 5090/H100 겸용)
# torch 2.7.1 + CUDA 12.8 (Blackwell sm_120 지원)
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY gpu-worker/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 모델 가중치를 이미지 안에 미리 내려받기 (부팅 시 다운로드 0 → 빠른 시동)
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('zibojia/minimax-remover', local_dir='/models', allow_patterns=['vae/*','transformer/*','scheduler/*'])"

COPY gpu-worker/handler.py gpu-worker/transformer_minimax_remover.py gpu-worker/pipeline_minimax_remover.py /app/

ENV MODEL_DIR=/models
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "handler.py"]
