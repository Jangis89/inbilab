# 인비랩 렌더 서버 — Node 22 + ffmpeg
FROM node:22-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-nanum \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY . .

ENV NODE_ENV=production
CMD ["npm", "start"]
