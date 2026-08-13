# 인비랩 렌더 서버 — Node 22 + ffmpeg + Silero VAD
FROM node:22-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-nanum python3 python3-pip libgomp1 curl \
  && rm -rf /var/lib/apt/lists/*

# Silero VAD (onnxruntime, PyTorch 불필요)
RUN pip3 install --break-system-packages --no-cache-dir onnxruntime

# 유튜브 대본 따기용 yt-dlp (소리 추출 → Soniox 정밀 받아쓰기)
RUN pip3 install --break-system-packages --no-cache-dir yt-dlp

# Silero VAD 모델(약 2MB) 내려받기 — 실패 시 빌드 중단
RUN mkdir -p /app/models \
  && ( curl -fL -o /app/models/silero_vad.onnx https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx \
       || curl -fL -o /app/models/silero_vad.onnx https://cdn.jsdelivr.net/gh/snakers4/silero-vad@master/src/silero_vad/data/silero_vad.onnx ) \
  && test $(stat -c%s /app/models/silero_vad.onnx) -gt 1000000

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY . .

ENV NODE_ENV=production
CMD ["npm", "start"]
