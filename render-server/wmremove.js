// ============================================
// 인비랩 자막·워터마크 제거기 — 워커 모듈 v1.0
// 방식: 지울 영역만 잘라(밴드/네모) MiniMax-Remover(Replicate)로 복원 후
//       글자 픽셀에만 다시 합성 → 나머지 화면은 100% 원본 유지
// 모드: auto(자막 자동감지 + 모서리 워터마크) / manual(드래그 네모)
// 등급: fast(50%/4step) · std(75%/6step) · hq(100%/8step)
// ============================================
import { createClient } from "@supabase/supabase-js";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { readFile, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";

const exec = promisify(execFile);
const SB_URL = process.env.SUPABASE_URL;
const SB_KEY = process.env.SUPABASE_SERVICE_ROLE;
const sb = createClient(SB_URL, SB_KEY, { auth: { persistSession: false } });
const REPLICATE_TOKEN = String(process.env.REPLICATE_API_TOKEN || "").trim();
const R_API = "https://api.replicate.com/v1";
const WM_MODEL = "ayushunleashed/minimax-remover";
const TIERS = {
  fast: { scale: 0.5, steps: 4, label: "빠름" },
  std:  { scale: 0.75, steps: 6, label: "표준" },
  hq:   { scale: 1.0, steps: 8, label: "고화질" },
};
const CHUNK_LEN = 401, CHUNK_STEP = 389; // 12프레임 겹침
const MAX_CONC = 6; // Replicate 동시 호출 수

// ---------- 공용 ----------
async function setProj(id, status, detail) {
  await sb.from("sc_projects").update({ status, status_detail: typeof detail === "string" ? detail : JSON.stringify(detail || {}), updated_at: new Date().toISOString() }).eq("id", id);
}
async function setProg(jobId, pct) {
  try { await sb.from("sc_render_jobs").update({ progress: Math.max(0, Math.min(99, Math.round(pct))) }).eq("id", jobId); } catch (e) {}
}
async function dl(url, dest) {
  const res = await fetch(url);
  if (!res.ok || !res.body) throw new Error("다운로드 실패 HTTP " + res.status);
  await pipeline(Readable.fromWeb(res.body), createWriteStream(dest));
}
function ff(args, opt) { return exec("ffmpeg", ["-hide_banner", "-nostats", "-loglevel", "error", ...args], { timeout: 1800000, maxBuffer: 64 * 1024 * 1024, ...(opt || {}) }); }
async function probeInfo(p) {
  const { stdout } = await exec("ffprobe", ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", p], { timeout: 120000, maxBuffer: 16 * 1024 * 1024 });
  const j = JSON.parse(stdout);
  const v = (j.streams || []).find((s) => s.codec_type === "video");
  if (!v) throw new Error("영상 스트림 없음");
  let fps = 30; const pr = String(v.r_frame_rate || "30/1").split("/");
  const f = pr.length === 2 && Number(pr[1]) ? Number(pr[0]) / Number(pr[1]) : Number(pr[0]);
  if (f > 0 && f <= 120) fps = Math.round(f * 1000) / 1000;
  return { W: v.width, H: v.height, fps, dur: parseFloat((j.format || {}).duration || 0), hasAudio: (j.streams || []).some((s) => s.codec_type === "audio") };
}
async function frameCount(p) {
  const { stdout } = await exec("ffprobe", ["-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", p], { timeout: 600000, maxBuffer: 4 * 1024 * 1024 });
  return parseInt(String(stdout).trim(), 10) || 0;
}
const snap16 = (n) => Math.max(16, Math.round(n / 16) * 16);
const floor16 = (n) => Math.max(16, Math.floor(n / 16) * 16);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// ---------- 프레임 스트리밍 (ffmpeg rawvideo <-> Node) ----------
function readFrames(input, vf, w, h, pix, onFrame) {
  // input에서 vf 필터 적용된 rawvideo 프레임을 순서대로 onFrame(buf, idx) 호출
  return new Promise((resolve, reject) => {
    const bytes = pix === "gray" ? w * h : w * h * 3;
    const args = ["-hide_banner", "-loglevel", "error", "-i", input, ...(vf ? ["-vf", vf] : []), "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"];
    const p = spawn("ffmpeg", args);
    let buf = Buffer.alloc(0), idx = 0, err = "", busy = false;
    p.stderr.on("data", (d) => { err += d; });
    const drain = async () => {
      if (busy) return; busy = true;
      while (buf.length >= bytes) {
        const fr = buf.subarray(0, bytes); buf = buf.subarray(bytes);
        const r = onFrame(fr, idx++);
        if (r && typeof r.then === "function") { p.stdout.pause(); await r; p.stdout.resume(); }
      }
      busy = false;
    };
    p.stdout.on("data", (d) => { buf = buf.length ? Buffer.concat([buf, d]) : d; drain().catch(reject); });
    p.on("close", async (c) => { try { await drain(); } catch (e) { return reject(e); } (c === 0 || idx > 0) ? resolve(idx) : reject(new Error("ffmpeg 읽기 실패: " + err.slice(-300))); });
    p.on("error", reject);
  });
}
function maskWriter(out, w, h, fps) {
  const p = spawn("ffmpeg", ["-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", w + "x" + h, "-r", String(fps), "-i", "pipe:0", "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", out, "-y"]);
  let err = ""; p.stderr.on("data", (d) => { err += d; });
  return {
    write: (buf) => new Promise((res) => (p.stdin.write(buf) ? res() : p.stdin.once("drain", res))),
    end: () => new Promise((res, rej) => { p.stdin.end(); p.on("close", (c) => (c === 0 ? res() : rej(new Error("mask 인코딩 실패: " + err.slice(-300))))); }),
  };
}

// ---------- 화소 연산 ----------
function dilate(src, w, h, r, dst) {
  // 분리형 사각 팽창 (이진 0/1 Uint8Array)
  const tmp = dilate._t && dilate._t.length >= w * h ? dilate._t : (dilate._t = new Uint8Array(w * h));
  for (let y = 0; y < h; y++) {
    const row = y * w;
    for (let x = 0; x < w; x++) {
      let v = 0;
      for (let k = Math.max(0, x - r); k <= Math.min(w - 1, x + r); k++) if (src[row + k]) { v = 1; break; }
      tmp[row + x] = v;
    }
  }
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) {
      let v = 0;
      for (let k = Math.max(0, y - r); k <= Math.min(h - 1, y + r); k++) if (tmp[k * w + x]) { v = 1; break; }
      dst[y * w + x] = v;
    }
  }
}
function glyphClusters(rgb, w, h) {
  // 흰 글자(저채도 고명도) + 검은 테두리 조건 → 글자조각 CC → 가로줄 클러스터
  const n = w * h;
  const white = new Uint8Array(n), dark = new Uint8Array(n);
  for (let i = 0, j = 0; i < n; i++, j += 3) {
    const r = rgb[j], g = rgb[j + 1], b = rgb[j + 2];
    const mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    const mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
    const sat = mx ? ((mx - mn) * 255 / mx) | 0 : 0;
    if (mx > 200 && sat < 50) white[i] = 1;
    if (mx < 75) dark[i] = 1;
  }
  const darkD = new Uint8Array(n); dilate(dark, w, h, 4, darkD);
  // CC 라벨링 (흰 픽셀)
  const label = new Int32Array(n).fill(-1);
  const stack = new Int32Array(n);
  const comps = [];
  for (let i = 0; i < n; i++) {
    if (!white[i] || label[i] >= 0) continue;
    const id = comps.length; let top = 0; stack[top++] = i; label[i] = id;
    let minx = w, maxx = 0, miny = h, maxy = 0, area = 0, sig = 0;
    const px = [];
    while (top > 0) {
      const c = stack[--top]; const cy = (c / w) | 0, cx = c % w;
      area++; px.push(c);
      if (white[c] && darkD[c]) sig++;
      if (cx < minx) minx = cx; if (cx > maxx) maxx = cx;
      if (cy < miny) miny = cy; if (cy > maxy) maxy = cy;
      if (cx > 0 && white[c - 1] && label[c - 1] < 0) { label[c - 1] = id; stack[top++] = c - 1; }
      if (cx < w - 1 && white[c + 1] && label[c + 1] < 0) { label[c + 1] = id; stack[top++] = c + 1; }
      if (cy > 0 && white[c - w] && label[c - w] < 0) { label[c - w] = id; stack[top++] = c - w; }
      if (cy < h - 1 && white[c + w] && label[c + w] < 0) { label[c + w] = id; stack[top++] = c + w; }
    }
    comps.push({ minx, maxx, miny, maxy, area, sig, px, w: maxx - minx + 1, h: maxy - miny + 1 });
  }
  // 글자조각 필터
  const good = comps.filter((c) => c.area >= 20 && c.area <= 7000 && c.h >= 8 && c.h <= 85 && c.w <= 240 && c.sig >= Math.max(4, 0.06 * c.area));
  // 가로줄 클러스터 (y중심 ±25)
  const clusters = [];
  for (const c of good.sort((a, b) => (a.miny + a.maxy) - (b.miny + b.maxy))) {
    const cy = (c.miny + c.maxy) / 2;
    let put = null;
    for (const cl of clusters) if (Math.abs(cl.cy - cy) < 25) { put = cl; break; }
    if (put) { put.items.push(c); put.cy = put.items.reduce((s, it) => s + (it.miny + it.maxy) / 2, 0) / put.items.length; }
    else clusters.push({ cy, items: [c] });
  }
  const out = [];
  for (const cl of clusters) {
    const its = cl.items;
    if (its.length < 2) continue;
    const x0 = Math.min(...its.map((i) => i.minx)), x1 = Math.max(...its.map((i) => i.maxx));
    if (x1 - x0 < 90) continue;
    const hs = its.map((i) => i.h).sort((a, b) => a - b); const med = hs[(hs.length / 2) | 0];
    if (med < 16 || med > 80) continue;
    if (x0 < 0.07 * w || x1 > 0.93 * w) continue;
    const y0 = Math.min(...its.map((i) => i.miny)), y1 = Math.max(...its.map((i) => i.maxy));
    out.push({ x0, x1, y0, y1, items: its, dark });
  }
  return out;
}
function iou(a, b) {
  const x1 = Math.max(a.x0, b.x0), y1 = Math.max(a.y0, b.y0), x2 = Math.min(a.x1, b.x1), y2 = Math.min(a.y1, b.y1);
  if (x2 <= x1 || y2 <= y1) return 0;
  const inter = (x2 - x1) * (y2 - y1);
  return inter / ((a.x1 - a.x0) * (a.y1 - a.y0) + (b.x1 - b.x0) * (b.y1 - b.y0) - inter);
}
function rasterize(clusters, w, h) {
  // 확정 클러스터 → 글자픽셀 + (글자 8px 이내의 어두운 테두리) → 4px 팽창 마스크(0/255)
  const m = new Uint8Array(w * h);
  let dark = null;
  for (const cl of clusters) { for (const it of cl.items) for (const p of it.px) m[p] = 1; dark = cl.dark; }
  const near = new Uint8Array(w * h); dilate(m, w, h, 8, near);
  if (dark) for (let i = 0; i < w * h; i++) if (near[i] && dark[i]) m[i] = 1;
  const d = new Uint8Array(w * h); dilate(m, w, h, 4, d);
  const out = Buffer.alloc(w * h);
  for (let i = 0; i < w * h; i++) out[i] = d[i] ? 255 : 0;
  return out;
}

// ---------- Replicate ----------
let wmVersion = null;
async function rFetch(path, opt) {
  const res = await fetch(R_API + path, { ...opt, headers: { Authorization: "Bearer " + REPLICATE_TOKEN, ...((opt || {}).headers || {}) } });
  if (!res.ok) throw new Error("Replicate " + path + " HTTP " + res.status + " " + (await res.text()).slice(0, 200));
  return res.json();
}
async function getVersion() {
  if (wmVersion) return wmVersion;
  const j = await rFetch("/models/" + WM_MODEL);
  wmVersion = j.latest_version && j.latest_version.id;
  if (!wmVersion) throw new Error("모델 버전 조회 실패");
  return wmVersion;
}
async function rUpload(path) {
  const buf = await readFile(path);
  const fd = new FormData();
  fd.append("content", new Blob([buf], { type: "video/mp4" }), "f.mp4");
  const j = await rFetch("/files", { method: "POST", body: fd });
  return j.urls && j.urls.get ? j.urls.get : (() => { throw new Error("파일 업로드 응답 이상"); })();
}
async function rPredict(videoUrl, maskUrl, steps) {
  const version = await getVersion();
  const j = await rFetch("/predictions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version, input: { video: videoUrl, mask: maskUrl, num_inference_steps: steps, mask_dilation_iterations: 6, num_frames: -1, height: -1, width: -1, fps: -1 } }) });
  const id = j.id;
  const t0 = Date.now();
  for (;;) {
    await new Promise((r) => setTimeout(r, 5000));
    const s = await rFetch("/predictions/" + id);
    if (s.status === "succeeded") return { out: Array.isArray(s.output) ? s.output[0] : s.output, sec: (s.metrics && s.metrics.predict_time) || (Date.now() - t0) / 1000 };
    if (s.status === "failed" || s.status === "canceled") throw new Error("AI 복원 실패: " + String(s.error || s.status).slice(0, 200));
    if (Date.now() - t0 > 1200000) throw new Error("AI 복원 시간 초과");
  }
}
async function pool(items, limit, fn) {
  const out = new Array(items.length); let i = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) { const k = i++; if (k >= items.length) return; out[k] = await fn(items[k], k); }
  });
  await Promise.all(workers);
  return out;
}

// ---------- 자동 감지 ----------
async function detectSubBand(work, W, H) {
  // 샘플 프레임에서 자막줄 y범위 탐색 (아래쪽 45%)
  const y0 = Math.floor(H * 0.5), bh = Math.floor(H * 0.45);
  let hits = 0, minY = bh, maxY = 0;
  await readFrames(work, `select='not(mod(n,12))',crop=${W}:${bh}:0:${y0}`, W, bh, "rgb24", (buf) => {
    const cls = glyphClusters(buf, W, bh);
    if (cls.length) { hits++; for (const c of cls) { if (c.y0 < minY) minY = c.y0; if (c.y1 > maxY) maxY = c.y1; } }
  });
  if (hits < 4) return null;
  let by0 = clamp(y0 + minY - 40, 0, H - 32);
  let bh2 = floor16(clamp((maxY - minY) + 96, 96, Math.floor(H * 0.35)));
  if (by0 + bh2 > H) by0 = H - bh2;
  const bw = floor16(W);
  return { x: Math.floor((W - bw) / 2), y: by0, w: bw, h: bh2, kind: "subtitle" };
}
async function detectCorner(work, W, H, side) {
  // 모서리(좌상/우상) 고정 무늬 감지: 여러 프레임에서 안 변하는 경계픽셀
  const cw = floor16(Math.floor(W * 0.42)), ch = floor16(Math.floor(H * 0.28));
  const cx = side === "tl" ? 0 : W - cw;
  const frames = [];
  await readFrames(work, `select='not(mod(n,50))',crop=${cw}:${ch}:${cx}:0,format=gray`, cw, ch, "gray", (buf) => { if (frames.length < 30) frames.push(Buffer.from(buf)); });
  if (frames.length < 6) return null;
  const n = cw * ch, k = frames.length;
  const mask = new Uint8Array(n);
  const med = new Uint8Array(n);
  const vals = new Uint8Array(k);
  for (let i = 0; i < n; i++) {
    let mn = 255, mx = 0;
    for (let f = 0; f < k; f++) { const v = frames[f][i]; vals[f] = v; if (v < mn) mn = v; if (v > mx) mx = v; }
    vals.sort(); med[i] = vals[k >> 1];
    if (mx - mn <= 20) mask[i] = 1;
  }
  let cnt = 0;
  const wm = new Uint8Array(n);
  for (let y = 1; y < ch - 1; y++) for (let x = 1; x < cw - 1; x++) {
    const i = y * cw + x;
    const g = Math.abs(med[i] - med[i + 1]) + Math.abs(med[i] - med[i + cw]);
    if (mask[i] && g > 14) { wm[i] = 1; cnt++; }
  }
  let stat = 0; for (let i = 0; i < n; i++) if (mask[i]) stat++;
  if (stat / n > 0.55) return null;
  const ratio = cnt / n;
  if (ratio < 0.004 || ratio > 0.08) return null;
  const d = new Uint8Array(n); dilate(wm, cw, ch, 6, d);
  let x0 = cw, x1 = 0, y0 = ch, y1 = 0, tot = 0;
  for (let y = 0; y < ch; y++) for (let x = 0; x < cw; x++) if (d[y * cw + x]) { tot++; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  if (tot < 400 || tot > n * 0.25) return null;
  if ((x1 - x0) > cw * 0.85 || (y1 - y0) > ch * 0.85) return null;
  const px = Buffer.alloc(n); for (let i = 0; i < n; i++) px[i] = d[i] ? 255 : 0;
  return { x: cx, y: 0, w: cw, h: ch, kind: side === "tl" ? "corner-left" : "corner-right", staticMask: px };
}

// ---------- 영역 처리 ----------
async function buildSubtitleMask(work, region, fps, tmp) {
  // pass1: 프레임별 클러스터 bbox 수집 → pass2: 시간 안정성 + ±6 합집합 스트리밍 마스크
  const { w, h, x, y } = region;
  const perFrame = [];
  await readFrames(work, `crop=${w}:${h}:${x}:${y}`, w, h, "rgb24", (buf) => { perFrame.push(glyphClusters(buf, w, h).map((c) => ({ x0: c.x0, x1: c.x1, y0: c.y0, y1: c.y1 }))); });
  const N = perFrame.length;
  const stable = (i, box) => {
    let cnt = 0;
    for (let j = Math.max(0, i - 6); j <= Math.min(N - 1, i + 6); j++) {
      if (j === i) continue;
      if (perFrame[j].some((b) => iou(box, b) > 0.3)) cnt++;
    }
    return cnt >= 5;
  };
  const maskPath = join(tmp, "mask_" + region.kind + ".mp4");
  const mw = maskWriter(maskPath, w, h, fps);
  const ring = []; // {idx, buf}
  let masked = 0;
  await readFrames(work, `crop=${w}:${h}:${x}:${y}`, w, h, "rgb24", async (buf, i) => {
    const cls = glyphClusters(buf, w, h).filter((c) => stable(i, c));
    const m = cls.length ? rasterize(cls, w, h) : Buffer.alloc(w * h);
    if (cls.length) masked++;
    ring.push(m);
    if (ring.length > 13) ring.shift();
    if (i >= 6) {
      // 출력 프레임 i-6 = ring 내 합집합
      const out = Buffer.alloc(w * h);
      for (const r of ring) for (let p = 0; p < out.length; p++) if (r[p]) out[p] = 255;
      await mw.write(out);
    }
  });
  // 꼬리 6프레임
  for (let t = 0; t < 6 && ring.length; t++) {
    const out = Buffer.alloc(region.w * region.h);
    for (const r of ring) for (let p = 0; p < out.length; p++) if (r[p]) out[p] = 255;
    await mw.write(out);
    ring.shift();
  }
  await mw.end();
  return { maskPath, maskedFrames: masked, N };
}
async function buildStaticMask(region, N, fps, tmp) {
  const { w, h } = region;
  const png = join(tmp, "static_" + region.kind + ".png");
  let raw;
  if (region.staticMask) raw = region.staticMask;
  else { raw = Buffer.alloc(w * h, 0); const m = 12, rx0 = Math.max(0, region.rx0 - m), rx1 = Math.min(w - 1, region.rx1 + m), ry0 = Math.max(0, region.ry0 - m), ry1 = Math.min(h - 1, region.ry1 + m); for (let yy = ry0; yy <= ry1; yy++) for (let xx = rx0; xx <= rx1; xx++) raw[yy * w + xx] = 255; }
  const rawp = join(tmp, "static_" + region.kind + ".raw");
  await writeFile(rawp, raw);
  await ff(["-f", "rawvideo", "-pix_fmt", "gray", "-s", w + "x" + h, "-i", rawp, "-frames:v", "1", png, "-y"]);
  const maskPath = join(tmp, "mask_" + region.kind + ".mp4");
  await ff(["-loop", "1", "-i", png, "-t", String(N / fps), "-r", String(fps), "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", maskPath, "-y"]);
  return { maskPath, maskedFrames: N, N };
}
async function processRegion(work, region, maskPath, N, fps, tier, tmp, onStep) {
  const { w, h, x, y } = region;
  const sw = snap16(w * tier.scale), sh = snap16(h * tier.scale);
  const rSrc = join(tmp, "rs_" + region.kind + ".mp4");
  const rMask = join(tmp, "rm_" + region.kind + ".mp4");
  await ff(["-i", work, "-vf", `crop=${w}:${h}:${x}:${y},scale=${sw}:${sh}`, "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", rSrc, "-y"]);
  await ff(["-i", maskPath, "-vf", `scale=${sw}:${sh}:flags=neighbor`, "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", rMask, "-y"]);
  // 조각 계획
  const starts = [];
  if (N <= CHUNK_LEN) starts.push(0);
  else { for (let s = 0; s + CHUNK_LEN <= N; s += CHUNK_STEP) starts.push(s); if (starts[starts.length - 1] + CHUNK_LEN < N) starts.push(N - CHUNK_LEN); }
  const chunks = starts.map((s, i) => ({ i, s, e: Math.min(N - 1, s + CHUNK_LEN - 1) }));
  let doneCnt = 0, gpuSec = 0;
  const outs = await pool(chunks, MAX_CONC, async (c) => {
    const cv = join(tmp, `c_${region.kind}_${c.i}.mp4`), cm = join(tmp, `cm_${region.kind}_${c.i}.mp4`);
    await ff(["-i", rSrc, "-vf", `select='between(n,${c.s},${c.e})',setpts=N/${fps}/TB`, "-vsync", "0", "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", cv, "-y"]);
    await ff(["-i", rMask, "-vf", `select='between(n,${c.s},${c.e})',setpts=N/${fps}/TB`, "-vsync", "0", "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", cm, "-y"]);
    const [vu, mu] = [await rUpload(cv), await rUpload(cm)];
    const r = await rPredict(vu, mu, tier.steps);
    gpuSec += r.sec || 0;
    const op = join(tmp, `o_${region.kind}_${c.i}.mp4`);
    await dl(r.out, op);
    doneCnt++; if (onStep) await onStep(doneCnt, chunks.length);
    return { ...c, op };
  });
  // 겹침 중간에서 자르고 이어붙이기 (+부족분 마지막 프레임 복제)
  const parts = [];
  for (let k = 0; k < outs.length; k++) {
    const c = outs[k];
    const from = k === 0 ? c.s : outs[k - 1].e - 5; // 이전과 6프레임 겹침 지점
    const to = k === outs.length - 1 ? c.e : outs[k + 1].s + 5;
    const rel0 = from - c.s, rel1 = to - c.s;
    const pp = join(tmp, `p_${region.kind}_${k}.mp4`);
    const have = await frameCount(c.op);
    const r1 = Math.min(rel1, have - 1);
    await ff(["-i", c.op, "-vf", `select='between(n,${rel0},${r1})',setpts=N/${fps}/TB` + (r1 < rel1 ? `,tpad=stop_mode=clone:stop=${rel1 - r1}` : ""), "-vsync", "0", "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", pp, "-y"]);
    parts.push(pp);
  }
  const listp = join(tmp, `list_${region.kind}.txt`);
  await writeFile(listp, parts.map((p) => `file '${p}'`).join("\n"));
  const merged = join(tmp, `res_${region.kind}.mp4`);
  await ff(["-f", "concat", "-safe", "0", "-i", listp, "-vf", `scale=${w}:${h}`, "-r", String(fps), "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", merged, "-y"]);
  // 프레임 수 보정
  const mf = await frameCount(merged);
  if (mf < N) await ff(["-i", merged, "-vf", `tpad=stop_mode=clone:stop=${N - mf}`, "-r", String(fps), "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", merged + ".fix.mp4", "-y"]).then(() => exec("mv", [merged + ".fix.mp4", merged]));
  return { merged, gpuSec, chunks: chunks.length };
}

// ---------- 대기열 스캔 (프론트가 wm_queued로 넣으면 작업 예약) ----------
export async function scanWmQueued() {
  if (!REPLICATE_TOKEN) return;
  try {
    const { data: rows } = await sb.from("sc_projects").select("id").eq("objective", "wm_remove").eq("status", "wm_queued").limit(3);
    for (const r of rows || []) {
      const { error } = await sb.from("sc_render_jobs").insert({ project_id: r.id, job_type: "wmremove", idempotency_key: "wmremove-" + r.id });
      if (!error || /duplicate|unique/i.test(error.message)) await setProj(r.id, "wm_waiting", "대기열에 등록됐어요");
    }
  } catch (e) { console.error("[wm] 스캔 실패:", e.message); }
}
export async function cleanupWmExpired() {
  try {
    const cutoff = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
    const { data: rows } = await sb.from("sc_projects").select("id, user_id, source_path").eq("objective", "wm_remove").eq("status", "wm_done").is("cleaned_at", null).lt("updated_at", cutoff).limit(20);
    for (const p of rows || []) {
      try { await sb.storage.from("videos-clips").remove([p.user_id + "/wm_" + p.id + ".mp4"]); } catch (e) {}
      if (p.source_path) { try { await sb.storage.from("videos-source").remove([p.source_path]); } catch (e) {} }
      await sb.from("sc_projects").update({ cleaned_at: new Date().toISOString() }).eq("id", p.id);
    }
  } catch (e) {}
}

// ---------- 본 작업 ----------
export async function runWmRemove(job) {
  if (!REPLICATE_TOKEN) throw new Error("REPLICATE_API_TOKEN이 설정되지 않았어요");
  const { data: proj, error } = await sb.from("sc_projects").select("id, user_id, source_path, wm_mode, wm_tier, wm_rects").eq("id", job.project_id).single();
  if (error || !proj || !proj.source_path) throw new Error("프로젝트/원본 없음");
  const tier = TIERS[proj.wm_tier] || TIERS.std;
  const mode = proj.wm_mode === "manual" ? "manual" : "auto";
  const t0 = Date.now();
  const tmp = await mkdtemp(join(tmpdir(), "ib-wm-"));
  try {
    await setProj(proj.id, "wm_running", "영상을 받아 오는 중…");
    const { data: signed, error: se } = await sb.storage.from("videos-source").createSignedUrl(proj.source_path, 21600);
    if (se) throw new Error("서명 URL 실패: " + se.message);
    const src = join(tmp, "src.mp4");
    await dl(signed.signedUrl, src);
    const info = await probeInfo(src);
    if (info.dur > 900) throw new Error("지금은 15분 이하 영상만 지원해요. 나눠서 올려주세요.");
    await setProg(job.id, 8);
    await setProj(proj.id, "wm_running", "영상을 분석하는 중…");
    const work = join(tmp, "work.mp4");
    await ff(["-i", src, "-vf", `fps=${info.fps}`, "-an", "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", "-pix_fmt", "yuv420p", work, "-y"]);
    const N = await frameCount(work);
    // ---- 영역 결정 ----
    const regions = [];
    if (mode === "manual") {
      const rects = Array.isArray(proj.wm_rects) ? proj.wm_rects : [];
      if (!rects.length) throw new Error("지울 영역이 지정되지 않았어요");
      for (const r of rects.slice(0, 4)) {
        const px = clamp(Math.round(r.x * info.W), 0, info.W - 8), py = clamp(Math.round(r.y * info.H), 0, info.H - 8);
        const pw = clamp(Math.round(r.w * info.W), 8, info.W - px), ph = clamp(Math.round(r.h * info.H), 8, info.H - py);
        let gx = clamp(px - 32, 0, info.W), gy = clamp(py - 32, 0, info.H);
        let gw = floor16(Math.min(info.W - gx, pw + 64)), gh = floor16(Math.min(info.H - gy, ph + 64));
        if (gx + gw > info.W) gx = info.W - gw; if (gy + gh > info.H) gy = info.H - gh;
        regions.push({ x: gx, y: gy, w: gw, h: gh, kind: "manual" + regions.length, rx0: px - gx, rx1: px - gx + pw - 1, ry0: py - gy, ry1: py - gy + ph - 1 });
      }
    } else {
      await setProj(proj.id, "wm_running", "자막·워터마크를 찾는 중…");
      const band = await detectSubBand(work, info.W, info.H);
      if (band) regions.push(band);
      for (const side of ["tl", "tr"]) { const c = await detectCorner(work, info.W, info.H, side); if (c) regions.push(c); }
      if (!regions.length) { await setProj(proj.id, "wm_done", JSON.stringify({ note: "no_target", msg: "지울 자막·워터마크를 찾지 못했어요. [직접 지정] 모드로 영역을 그려주세요." })); return; }
    }
    await setProg(job.id, 18);
    // ---- 마스크 생성 ----
    let totalChunks = 0, doneChunks = 0, gpuSec = 0;
    const prepared = [];
    for (const region of regions) {
      let mk;
      if (region.kind === "subtitle") { await setProj(proj.id, "wm_running", "자막 글자 위치를 정밀하게 잡는 중…"); mk = await buildSubtitleMask(work, region, info.fps, tmp); }
      else if (region.kind.indexOf("manual") === 0) {
        await setProj(proj.id, "wm_running", "지정한 곳의 글자를 정밀하게 찾는 중…");
        const gm = await buildSubtitleMask(work, region, info.fps, tmp);
        mk = gm.maskedFrames >= Math.ceil(N * 0.7) ? gm : await buildStaticMask(region, N, info.fps, tmp);
      }
      else mk = await buildStaticMask(region, N, info.fps, tmp);
      if (region.kind === "subtitle" && mk.maskedFrames === 0) continue;
      prepared.push({ region, mk });
    }
    if (!prepared.length) { await setProj(proj.id, "wm_done", JSON.stringify({ note: "no_target", msg: "지울 자막을 찾지 못했어요. [직접 지정] 모드를 써주세요." })); return; }
    await setProg(job.id, 30);
    // ---- AI 복원 (영역별) ----
    const results = [];
    for (const p of prepared) {
      await setProj(proj.id, "wm_running", `AI가 배경을 복원하는 중… (${TIERS[proj.wm_tier] ? tier.label : "표준"})`);
      const r = await processRegion(work, p.region, p.mk.maskPath, N, info.fps, tier, tmp, async (d, t) => {
        doneChunks++; totalChunks = Math.max(totalChunks, t);
        await setProg(job.id, 30 + Math.round(55 * doneChunks / Math.max(1, prepared.length * t)));
        await setProj(proj.id, "wm_running", `AI가 배경을 복원하는 중… (${doneChunks}/${prepared.length * t} 조각)`);
      });
      gpuSec += r.gpuSec;
      results.push({ region: p.region, maskPath: p.mk.maskPath, merged: r.merged });
    }
    // ---- 합성 ----
    await setProj(proj.id, "wm_running", "복원한 부분을 원본에 합치는 중…");
    let cur = work;
    for (let i = 0; i < results.length; i++) {
      const r = results[i];
      const nxt = join(tmp, "comp_" + i + ".mp4");
      await ff(["-i", cur, "-i", r.merged, "-i", r.maskPath, "-filter_complex", `[2:v]format=gray,gblur=sigma=2[a];[1:v][a]alphamerge[ov];[0:v][ov]overlay=${r.region.x}:${r.region.y}`, "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p", nxt, "-y"]);
      cur = nxt;
    }
    const outp = join(tmp, "out.mp4");
    if (info.hasAudio) await ff(["-i", cur, "-i", src, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", outp, "-y"]);
    else await ff(["-i", cur, "-c:v", "copy", outp, "-y"]);
    await setProg(job.id, 92);
    // ---- 업로드 & 마무리 ----
    const clipPath = proj.user_id + "/wm_" + proj.id + ".mp4";
    const buf = await readFile(outp);
    const { error: ue } = await sb.storage.from("videos-clips").upload(clipPath, buf, { contentType: "video/mp4", upsert: true });
    if (ue) throw new Error("결과 업로드 실패: " + ue.message);
    const { data: rs } = await sb.storage.from("videos-clips").createSignedUrl(clipPath, 86400);
    // 사용량 +1 (월별)
    const ym = new Date().toISOString().slice(0, 7);
    try {
      const { data: u } = await sb.from("wm_usage").select("id, used").eq("user_id", proj.user_id).eq("ym", ym).maybeSingle();
      if (u) await sb.from("wm_usage").update({ used: u.used + 1, updated_at: new Date().toISOString() }).eq("id", u.id);
      else await sb.from("wm_usage").insert({ user_id: proj.user_id, ym, used: 1 });
    } catch (e) { console.error("[wm] 사용량 기록 실패:", e.message); }
    const detail = { url: rs && rs.signedUrl, mode, tier: proj.wm_tier || "std", regions: results.map((r) => r.region.kind), sec: Math.round((Date.now() - t0) / 1000), gpu_sec: Math.round(gpuSec) };
    await setProj(proj.id, "wm_done", JSON.stringify(detail));
    console.log("[wm] 완료 " + proj.id + " " + JSON.stringify({ ...detail, url: "(생략)" }));
  } catch (err) {
    if ((job.attempts || 0) >= 1) await setProj(proj.id, "failed_wm", String(err.message || err).slice(0, 200));
    throw err;
  } finally {
    try { await rm(tmp, { recursive: true, force: true }); } catch (e) {}
  }
}
