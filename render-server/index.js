// ============================================
// 인비랩 숏츠 제작기 — 렌더 서버 v0.2 (A-3: 전사 추가)
// 역할: sc_render_jobs 대기열을 감시해 작업을 하나씩 처리한다.
// 지원 작업: probe(파일 검사), transcribe(음성 받아적기)
// 다음 버전에서 추가: analyze(후보 발굴), render(컷·자막)
// 원칙: 원본 무수정 / 멱등성 / 실패 시 1회 자동 재시도 / 전부 기록
// ============================================
import { createClient } from "@supabase/supabase-js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFile, unlink, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import http from "node:http";
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { runWmRemove, scanWmQueued, cleanupWmExpired } from "./wmremove.js";

const exec = promisify(execFile);

async function downloadToFile(fileUrl, dest, label) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 1200000);
  try {
    const res = await fetch(fileUrl, { signal: ctrl.signal });
    if (!res.ok || !res.body) throw new Error("원본 다운로드 실패 HTTP " + res.status);
    const total = Number(res.headers.get("content-length") || 0);
    let got = 0, lastLog = Date.now();
    const src = Readable.fromWeb(res.body);
    src.on("data", (c) => {
      got += c.length;
      const now = Date.now();
      if (now - lastLog > 15000) { lastLog = now; console.log("[다운로드] " + (label || "") + " " + Math.round(got / 1048576) + "MB" + (total ? "/" + Math.round(total / 1048576) + "MB" : "")); }
    });
    await pipeline(src, createWriteStream(dest));
    console.log("[다운로드] 완료 " + (label || "") + " " + Math.round(got / 1048576) + "MB");
  } finally { clearTimeout(timer); }
}

const SUPABASE_URL = process.env.SUPABASE_URL;
const SERVICE_KEY = process.env.SUPABASE_SERVICE_ROLE;
if (!SUPABASE_URL || !SERVICE_KEY) {
  console.error("환경변수(SUPABASE_URL, SUPABASE_SERVICE_ROLE)가 없습니다.");
  process.exit(1);
}
const sb = createClient(SUPABASE_URL, SERVICE_KEY, { auth: { persistSession: false } });

// Gemini 설정 (키가 없으면 전사 작업은 대기열에 남겨두고 검사만 처리)
const GEMINI_KEY = String(process.env.GEMINI_API_KEY || "").trim();
const GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/";
const GEMINI_UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta/files";
const GEMINI_MODELS = ["gemini-flash-latest", "gemini-pro-latest"];

const WORKER_ID = "worker-" + Math.random().toString(36).slice(2, 8);
const POLL_MS = 5000;
const CHUNK_SEC = 600; // 전사용 오디오를 10분 단위로 잘라 처리 (안정성·재시도 용이)
const PROMPT_VER = "a3-ingredients"; // a1=대본만, a2=통짜 영상, a3=재료(장면) 기반
const VIDEO_CHUNK_SEC = 1200;   // 작업용 영상을 20분 단위로 처리
const SHOT_MIN_SEC = 2.5;       // 재료(장면) 최소 길이 — 이보다 짧으면 앞 장면에 합침
const SHOT_MAX_SEC = 12;        // 재료 최대 길이 — 이보다 길면 쪼갬
const SHOT_BATCH = 12;          // AI에게 한 번에 보여줄 재료 개수

// ---------- 상태 확인용 웹 응답 (Railway 헬스체크) ----------
const PORT = process.env.PORT || 8080;
let lastPollAt = null;
let processedCount = 0;
http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({
    ok: true, service: "inbilab-render-server", worker: WORKER_ID,
    lastPollAt, processedCount, ffmpeg: FFMPEG_OK, gemini: GEMINI_KEY ? "설정됨" : "미설정",
  }));
}).listen(PORT, () => console.log(`[서버] 상태 페이지 :${PORT}`));

// ---------- ffmpeg 존재 확인 ----------
let FFMPEG_OK = false;
try {
  const { stdout } = await exec("ffmpeg", ["-version"]);
  FFMPEG_OK = true;
  console.log("[준비] ffmpeg OK:", stdout.split("\n")[0]);
} catch {
  console.error("[준비] ffmpeg를 찾을 수 없음 — Dockerfile 확인 필요");
}
if (!GEMINI_KEY) console.warn("[준비] GEMINI_API_KEY 미설정 — 전사(transcribe) 작업은 키 설정 후 자동 시작됩니다.");

// ---------- 대기열 처리 루프 ----------
async function claimJob() {
  // queued 상태의 가장 오래된 작업 1개를 원자적으로 가져온다 (경쟁 방지: status 조건부 업데이트)
  const allowedTypes = (GEMINI_KEY ? ["probe", "transcribe", "analyze", "render", "desilence"] : ["probe", "render", "desilence"]).concat(process.env.REPLICATE_API_TOKEN || (process.env.RUNPOD_API_KEY && process.env.RUNPOD_ENDPOINT_ID) ? ["wmremove"] : []);
  const { data: jobs, error } = await sb
    .from("sc_render_jobs")
    .select("id, project_id, recipe_id, job_type, attempts")
    .eq("status", "queued")
    .in("job_type", allowedTypes)
    .order("created_at", { ascending: true })
    .limit(1);
  if (error) { console.error("[대기열] 조회 실패:", error.message); return null; }
  if (!jobs || jobs.length === 0) return null;
  const job = jobs[0];
  const { data: updated, error: e2 } = await sb
    .from("sc_render_jobs")
    .update({ status: "running", started_at: new Date().toISOString(), attempts: job.attempts + 1 })
    .eq("id", job.id)
    .eq("status", "queued") // 다른 워커가 먼저 가져갔으면 실패
    .select("id");
  if (e2 || !updated || updated.length === 0) return null;
  return job;
}

async function finishJob(jobId, ok, errMsg = "") {
  await sb.from("sc_render_jobs").update({
    status: ok ? "done" : "failed",
    progress: ok ? 100 : undefined,
    error: errMsg.slice(0, 500),
    finished_at: new Date().toISOString(),
  }).eq("id", jobId);
}

async function setJobProgress(jobId, pct) {
  await sb.from("sc_render_jobs").update({ progress: Math.max(0, Math.min(99, Math.round(pct))) }).eq("id", jobId);
}

async function setProjectStatus(projectId, status, detail = "") {
  await sb.from("sc_projects").update({
    status, status_detail: detail, updated_at: new Date().toISOString(),
  }).eq("id", projectId);
}

async function enqueueJob(projectId, jobType) {
  // 같은 작업이 이미 있으면 만들지 않는다 (멱등성)
  const { error } = await sb.from("sc_render_jobs").insert({
    project_id: projectId, job_type: jobType, idempotency_key: jobType + "-" + projectId,
  });
  if (error && !/duplicate|unique/i.test(error.message)) {
    console.error(`[대기열] ${jobType} 예약 실패:`, error.message);
  }
}

// ---------- 작업: probe (파일 검사) ----------
async function runProbe(job) {
  const { data: proj, error } = await sb.from("sc_projects")
    .select("id, source_path, objective").eq("id", job.project_id).single();
  if (error || !proj?.source_path) throw new Error("프로젝트/원본 경로 없음");
  await setProjectStatus(proj.id, "probing");

  // 원본에 접근할 수 있는 1시간짜리 서명 URL 생성
  const { data: signed, error: se } = await sb.storage
    .from("videos-source").createSignedUrl(proj.source_path, 3600);
  if (se) throw new Error("서명 URL 실패: " + se.message);

  // ffprobe로 메타정보 추출 (다운로드 없이 URL로 직접)
  const { stdout } = await exec("ffprobe", [
    "-v", "error", "-print_format", "json",
    "-show_format", "-show_streams", signed.signedUrl,
  ], { timeout: 120000 });
  const info = JSON.parse(stdout);
  const format = info.format || {};
  const streams = info.streams || [];
  const video = streams.find(s => s.codec_type === "video");
  const audio = streams.find(s => s.codec_type === "audio");
  const durationSec = Number(format.duration || 0);

  const warnings = [];
  if (!audio) warnings.push("소리(오디오)가 없는 영상입니다.");
  if (durationSec < 180) warnings.push("영상이 3분보다 짧습니다.");
  if (durationSec > 7200) warnings.push("영상이 2시간을 넘어 분석 정밀도가 다소 낮아질 수 있습니다.");
  if (video && Number(video.height) > Number(video.width)) warnings.push("세로 영상입니다. 가로 영상 기준으로 만들어졌지만 처리는 가능합니다.");

  const probe = {
    duration_sec: durationSec,
    width: video ? Number(video.width) : null,
    height: video ? Number(video.height) : null,
    fps: video ? video.avg_frame_rate : null,
    video_codec: video ? video.codec_name : null,
    audio_codec: audio ? audio.codec_name : null,
    size_bytes: Number(format.size || 0),
    warnings,
  };
  await sb.from("sc_projects").update({
    probe, source_duration_sec: durationSec, source_bytes: probe.size_bytes,
    status: "uploaded", status_detail: warnings.join(" ") || "검사 통과",
    updated_at: new Date().toISOString(),
  }).eq("id", proj.id);

  await sb.from("sc_usage_log").insert({
    project_id: proj.id, kind: "probe", duration_sec: durationSec, meta: { warnings },
  });
  console.log(`[probe] 완료 p=${proj.id} 길이=${Math.round(durationSec)}초 경고=${warnings.length}`);

  // 소리가 있는 영상이면 다음 단계(전사)를 자동 예약
  if ((proj.objective || "") === "desilence") await enqueueJob(proj.id, "desilence");
  else if ((proj.objective || "") === "wm_remove") await enqueueJob(proj.id, "wmremove");
  else if (audio) await enqueueJob(proj.id, "transcribe");
  else console.log(`[probe] p=${proj.id} 오디오 없음 — 전사 건너뜀`);
}

// ---------- Gemini 도우미 ----------
async function geminiUploadAudio(buf, mime) {
  // 1) 업로드 자리 예약
  const start = await fetch(GEMINI_UPLOAD + "?key=" + GEMINI_KEY, {
    method: "POST",
    headers: {
      "X-Goog-Upload-Protocol": "resumable",
      "X-Goog-Upload-Command": "start",
      "X-Goog-Upload-Header-Content-Length": String(buf.length),
      "X-Goog-Upload-Header-Content-Type": mime,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ file: { display_name: "inbilab-audio-chunk" } }),
    signal: AbortSignal.timeout(60000),
  });
  if (!start.ok) throw new Error("Gemini 업로드 시작 실패: HTTP " + start.status);
  const uploadUrl = start.headers.get("x-goog-upload-url");
  if (!uploadUrl) throw new Error("Gemini 업로드 URL 없음");

  // 2) 실제 바이트 업로드
  const up = await fetch(uploadUrl, {
    method: "POST",
    headers: {
      "X-Goog-Upload-Command": "upload, finalize",
      "X-Goog-Upload-Offset": "0",
      "Content-Length": String(buf.length),
    },
    body: buf,
    signal: AbortSignal.timeout(300000),
  });
  if (!up.ok) throw new Error("Gemini 업로드 실패: HTTP " + up.status);
  const j = await up.json();
  const file = j.file;
  if (!file?.name) throw new Error("Gemini 파일 정보 없음");

  // 3) 처리 완료(ACTIVE) 대기 — 최대 2분 (1.5초 간격으로 빠르게 확인)
  for (let i = 0; i < 80; i++) {
    if (file.state === "ACTIVE") return file;
    await new Promise(r => setTimeout(r, 1500));
    const g = await fetch(GEMINI_BASE + file.name + "?key=" + GEMINI_KEY, { signal: AbortSignal.timeout(30000) });
    const gj = await g.json().catch(() => null);
    if (gj?.state === "ACTIVE") return gj;
    if (gj?.state === "FAILED") throw new Error("Gemini 파일 처리 실패");
    if (gj) Object.assign(file, gj);
  }
  throw new Error("Gemini 파일 처리 대기 시간 초과");
}

async function geminiDeleteFile(name) {
  try { await fetch(GEMINI_BASE + name + "?key=" + GEMINI_KEY, { method: "DELETE", signal: AbortSignal.timeout(30000) }); } catch {}
}

function geminiText(gj) {
  try { return gj.candidates[0].content.parts.map(p => p.text || "").join(""); } catch { return ""; }
}

function parseJsonLoose(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch {}
  const m = text.match(/\{[\s\S]*\}/) || text.match(/\[[\s\S]*\]/);
  if (m) { try { return JSON.parse(m[0]); } catch {} }
  return null;
}

// 한도(429)면 다음 모델로 자동 전환
async function callGemini(requestBody) {
  let last = null;
  for (const m of GEMINI_MODELS) {
    let g, gj;
    try {
      g = await fetch(GEMINI_BASE + "models/" + m + ":generateContent?key=" + GEMINI_KEY, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
        signal: AbortSignal.timeout(600000),
      });
      gj = await g.json().catch(() => null);
    } catch (e) {
      last = new Error("Gemini 연결 실패: " + String(e?.message || e));
      continue;
    }
    if (g.ok) return { gj, model: m };
    const msg = gj?.error?.message || ("HTTP " + g.status);
    last = new Error("Gemini 호출 실패(" + m + "): " + msg);
    // 한도(429)나 일시 혼잡(503/overloaded)이면 다음 모델로 넘어가고, 다른 에러면 중단
    if (!(g.status === 429 || g.status === 503 || /quota|RESOURCE_EXHAUSTED|overloaded|unavailable/i.test(msg))) break;
  }
  throw last || new Error("Gemini 호출 실패");
}

const TRANSCRIBE_PROMPT = `당신은 정밀 전사(받아적기) 전문가입니다. 첨부된 오디오를 처음부터 끝까지 듣고, 들리는 말을 그대로 받아적으세요.

규칙:
- 요약·의역·창작 절대 금지. 실제로 들리는 문장만 기록.
- 한국어 발화는 한국어 맞춤법에 맞게. 다른 언어는 그 언어 그대로.
- 음악만 나오거나 말이 없는 구간은 건너뛰세요.
- s, e는 이 오디오 파일 기준 그 문장의 시작·끝 시각(초 단위, 소수 1자리).
- 문장은 자연스러운 문장 단위로 나누세요.

JSON만 출력하세요:
{"language":"한국어","sentences":[{"s":0.0,"e":4.2,"text":"들리는 그대로의 문장"}],"note":"특이사항 한 줄 (없으면 빈 문자열)"}`;

// ---------- 작업: transcribe (음성 받아적기) ----------
async function runTranscribe(job) {
  const { data: proj, error } = await sb.from("sc_projects")
    .select("id, source_path, probe, source_duration_sec").eq("id", job.project_id).single();
  if (error || !proj?.source_path) throw new Error("프로젝트/원본 경로 없음");
  if (proj.probe && !proj.probe.audio_codec) throw new Error("소리가 없는 영상이라 받아적을 수 없습니다");

  await setProjectStatus(proj.id, "transcribing", "음성을 글로 옮기는 중…");

  const durationSec = Number(proj.source_duration_sec || proj.probe?.duration_sec || 0);
  const chunkCount = Math.max(1, Math.ceil(durationSec / CHUNK_SEC));
  const tmpDir = await mkdtemp(join(tmpdir(), "ib-tr-"));
  const allSentences = [];
  const notes = [];
  let usedModel = "";

  try {
    for (let ci = 0; ci < chunkCount; ci++) {
      const offset = ci * CHUNK_SEC;
      // 매 조각마다 새 서명 URL (만료 걱정 없음)
      const { data: signed, error: se } = await sb.storage
        .from("videos-source").createSignedUrl(proj.source_path, 3600);
      if (se) throw new Error("서명 URL 실패: " + se.message);

      // 1) 소리만 추출 (16kHz 모노 opus — 작고 전사에 충분)
      const audioPath = join(tmpDir, "chunk-" + ci + ".ogg");
      const args = [];
      if (offset > 0) args.push("-ss", String(offset));
      args.push("-t", String(CHUNK_SEC), "-i", signed.signedUrl,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
        "-f", "ogg", audioPath, "-y");
      await exec("ffmpeg", args, { timeout: 900000 });

      // 2) Gemini에 업로드 → 3) 받아적기 요청
      const buf = await readFile(audioPath);
      if (buf.length < 200) { notes.push((ci + 1) + "번째 구간: 소리 없음"); await unlink(audioPath).catch(() => {}); continue; }
      const gFile = await geminiUploadAudio(buf, "audio/ogg");
      let out;
      try {
        const r = await callGemini({
          contents: [{ parts: [
            { file_data: { file_uri: gFile.uri, mime_type: "audio/ogg" } },
            { text: TRANSCRIBE_PROMPT },
          ] }],
          generationConfig: { responseMimeType: "application/json", temperature: 0, maxOutputTokens: 65536 },
        });
        usedModel = r.model;
        out = parseJsonLoose(geminiText(r.gj));
      } finally {
        await geminiDeleteFile(gFile.name);
        await unlink(audioPath).catch(() => {});
      }
      if (!out || !Array.isArray(out.sentences)) throw new Error((ci + 1) + "번째 구간 전사 결과 해석 실패");

      // 이 조각의 실제 길이 (마지막 조각은 10분보다 짧음) — AI가 과장한 시각을 여기에 맞춰 눌러준다
      const chunkDur = Math.min(CHUNK_SEC, Math.max(1, durationSec - offset));
      for (const s of out.sentences) {
        let s0 = Number(s.s), e0 = Number(s.e);
        const text = String(s.text || "").trim();
        if (!text || !isFinite(s0)) continue;
        s0 = Math.max(0, Math.min(s0, chunkDur));
        e0 = Math.max(s0, Math.min(isFinite(e0) ? e0 : s0 + 3, chunkDur));
        allSentences.push({
          s: Math.round((s0 + offset) * 1000),
          e: Math.round((e0 + offset) * 1000),
          text,
        });
      }
      if (out.note) notes.push(String(out.note));
      await setJobProgress(job.id, ((ci + 1) / chunkCount) * 100);
      console.log(`[transcribe] p=${proj.id} 구간 ${ci + 1}/${chunkCount} 완료 (누적 ${allSentences.length}문장)`);
    }
  } finally {
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }

  allSentences.sort((a, b) => a.s - b.s);
  const fullText = allSentences.map(x => x.text).join(" ");

  // 다시 실행해도 겹치지 않게: 이전 전사록 삭제 후 저장 (멱등성)
  await sb.from("sc_transcripts").delete().eq("project_id", proj.id);
  const { error: ie } = await sb.from("sc_transcripts").insert({
    project_id: proj.id,
    engine: "gemini:" + (usedModel || "unknown"),
    sentences: allSentences,
    full_text: fullText,
    quality_note: notes.join(" / ").slice(0, 500),
  });
  if (ie) throw new Error("전사록 저장 실패: " + ie.message);

  await setProjectStatus(proj.id, "uploaded",
    "전사 완료 — " + allSentences.length + "문장" + (allSentences.length === 0 ? " (말소리가 감지되지 않았습니다)" : ""));
  await sb.from("sc_usage_log").insert({
    project_id: proj.id, kind: "transcribe", duration_sec: durationSec,
    meta: { sentences: allSentences.length, chunks: chunkCount, model: usedModel },
  });
  console.log(`[transcribe] 완료 p=${proj.id} 문장=${allSentences.length} 모델=${usedModel}`);

  // 말이 충분히 있으면 다음 단계(후보 발굴)를 자동 예약
  if (allSentences.length >= 10) await enqueueJob(proj.id, "analyze");
  else console.log(`[transcribe] p=${proj.id} 문장이 너무 적어 후보 발굴 건너뜀`);
}

// ---------- 작업: analyze (숏츠 후보 발굴) ----------
// 목적별 점수 가중치 (합계 1.0) — context_dep(맥락 의존)은 낮을수록 좋아서 뒤집어 계산
const OBJECTIVE_WEIGHTS = {
  views: { hook: 0.30, novelty: 0.20, emotion: 0.15, density: 0.10, standalone: 0.10, proof: 0.05, duration_fit: 0.05, context_indep: 0.05 },
  sales: { hook: 0.15, proof: 0.25, standalone: 0.15, density: 0.15, emotion: 0.10, novelty: 0.05, duration_fit: 0.05, context_indep: 0.10 },
  edu:   { hook: 0.15, density: 0.25, proof: 0.20, standalone: 0.15, novelty: 0.10, duration_fit: 0.05, context_indep: 0.10 },
};

function fmtTime(ms) {
  const t = Math.round(ms / 1000);
  return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0");
}

function buildAnalyzePrompt(objective, chunkStartMin, chunkEndMin, lines) {
  const objText = {
    views: "조회수(널리 퍼지는 것)가 목표입니다. 시선을 강탈하는 화면과 강한 후킹을 최우선으로 평가하세요.",
    sales: "판매·상담 문의로 이어지는 것이 목표입니다. 신뢰(근거·증거)와 문제 해결 약속이 뚜렷하면서도 시선을 붙잡는 구간을 최우선으로 평가하세요.",
    edu: "핵심이 잘 전달되는 교육용이 목표입니다. 정보 밀도가 높으면서도 지루하지 않은(화면·전개에 힘이 있는) 구간을 최우선으로 평가하세요.",
  }[objective] || "조회수가 목표입니다.";

  return `당신은 조회수에 목숨 건 유튜브 숏츠 편집장입니다. 첨부된 영상(원본의 ${chunkStartMin}분~${chunkEndMin}분 구간)을 화면과 소리 모두 직접 보고 들으면서, "숏츠로 잘라내면 터질 구간"을 찾으세요.

[목표] ${objText}

[후킹 유형표 — 후보는 반드시 이 중 하나 이상에 해당해야 합니다]
1 호기심 갭: 답을 숨겨서 궁금하게 만듦
2 결과 먼저: 가장 놀라운 결과·장면을 첫 컷에
3 충격 비주얼: 이상하거나 놀라운 화면으로 시선 강탈
4 질문 던지기: 시청자에게 직접 질문
5 공감 저격: "이런 적 있으시죠?" 내 얘기처럼
6 손실 회피: "모르면 손해" 경고형
7 숫자·랭킹: 순위나 구체적 숫자
8 반전 예고: 뒤에 반전이 있음을 예고
9 권위·증거: 전문가·실험·데이터 먼저
10 패턴 파괴: 예상 밖의 화면·소리·연출

[탈락 기준 — 가장 중요. 어기면 실패입니다]
- 그냥 잔잔한 설명, 평범한 풍경, 밋밋한 대화 구간은 내용이 좋아도 절대 뽑지 마세요.
- 숏츠의 첫 3초에 보여줄 "시선을 붙잡는 실제 화면"이 그 구간 안에 없으면 탈락입니다.
- first_scene에는 그 화면에 실제로 보이는 것을 구체적으로 쓰세요 (창작 금지). 화면과 시각이 안 맞으면 실패입니다.
- 이 클립에 그런 구간이 없으면 후보 0개로 답하세요. 억지로 채우는 것이 가장 나쁜 답입니다.

[규칙]
1. 후보 0~4개. 각 20~75초. start_s / end_s는 "이 클립 기준" 초 단위입니다.
2. start_s는 가장 강한 화면·발화가 나오는 순간 직전(0~1초 전)으로 잡으세요. 숏츠 첫 3초가 훅이 되게.
3. 후보 구간만 따로 봐도 이해돼야 합니다 (완결성).
4. title은 그 숏츠의 추천 제목 — 후킹 유형에 맞는 어그로형, 30자 이내. 단, 그 구간에 실제로 나오는 내용만 약속하세요 (구간에 없는 이유·설명을 제목으로 약속하면 실패).
5. 점수는 0~10 정수. hook 7점 미만이면 후보에서 빼세요.
6. scores 항목: hook(첫 3초 훅 강도), standalone(완결성), density(정보 밀도), emotion(감정 반응), novelty(새로움·의외성), proof(근거·증거), duration_fit(길이 적합), context_dep(맥락 의존 — 낮을수록 좋음), safety(안전성).
7. risk_flags: 주의할 점 배열 (예: "동물 부상 장면", "밀렵 언급"). 없으면 빈 배열.

[참고용 대본 — 시각은 이 클립 기준 초]
${lines}

JSON만 출력하세요:
{"candidates":[{"start_s":123.4,"end_s":168.0,"hook_type":3,"hook_type_name":"충격 비주얼","first_scene":"첫 3초에 실제로 보이는 화면 묘사","title":"...","hook_reason":"왜 터질 수 있는지 한 줄","summary":"구간 내용 요약 한 줄","scores":{"hook":8,"standalone":7,"density":6,"emotion":7,"novelty":9,"proof":5,"duration_fit":8,"context_dep":2,"safety":9},"risk_flags":[]}]}`;
}

// 재료 기록 지시문: 클립 여러 개를 보고 "실제로 보이는 것만" 기록 (창작 금지)
function buildIngredientPrompt(count) {
  return `당신은 영상 재료 기록원입니다. 첨부된 짧은 클립 ${count}개를 순서대로 하나씩 보고 들으며, 각 클립에 "실제로 보이고 들리는 것"만 기록하세요.

[규칙 — 어기면 실패]
- 추측·창작·해석 금지. 화면에 보이는 사실만. (예: "다리를 다친 것 같다" 금지 → 다리가 실제로 잘려 있을 때만 기록)
- desc: 이 클립에 보이는 것 한 문장 (주어+행동 중심, 한국어)
- action: 핵심 움직임 한 단어~짧은 구 (예: "나무 오르기", "걷기", "정지 화면")
- subject_pos: 주인공(가장 눈에 띄는 대상)이 화면의 어디에 있는지 — left | center | right
- event: 사건성 점수 0~10. "이 조각에서 뭔가 일어나는가?"만 평가.
  10 = 결정적 순간(사냥, 점프, 충돌, 출산, 골처럼 확실한 사건이 벌어짐)
  7~9 = 눈에 띄는 행동·움직임·반전이 있음 / 함성·웃음·비명 등 소리가 터짐
  4~6 = 무언가 하고는 있지만 평범한 움직임
  0~3 = 풍경, 정지, 인터뷰, 잔잔한 장면
- tags: 검색용 단어 2~4개 (예: ["사향노루","야간","숲"])
- usable: 까맣거나 흐릿하거나 자막판 같은 쓸모없는 클립이면 false
- i는 클립 순서(1부터). 첨부된 순서 그대로.

JSON만 출력하세요:
{"clips":[{"i":1,"desc":"...","action":"...","subject_pos":"center","event":5,"tags":["..."],"usable":true}]}`;
}

// 하이라이트 대본 지시문: 이미 뽑힌 장면들(시간순)에 제목·자막·나레이션만 입힘 (장면 변경 금지)
function buildHighlightPrompt(sceneList) {
  return `당신은 스포츠 뉴스 하이라이트 편집자입니다. 아래는 긴 영상에서 "사건이 일어난 순간"만 뽑아 시간 순서대로 나열한 하이라이트 장면들입니다. 장면 구성은 이미 확정됐습니다 — 바꾸지 말고, 제목과 장면별 자막·나레이션만 만드세요.

[확정된 하이라이트 장면 — 시간순]
${sceneList}

[규칙]
- 각 장면의 desc에 실제로 있는 내용만 쓰세요. 창작·과장으로 없는 사실을 만들지 마세요 (하이라이트는 다큐처럼 정직하게, 대신 문장은 박진감 있게).
- caption: 장면 위에 얹을 자막 한 줄 (띄어쓰기 포함 22자 이내, 강한 구어체)
- narration: 수강생이 AI 목소리로 읽을 한 문장 (스포츠 캐스터처럼 생동감 있게)
- title: 이 하이라이트 전체의 제목 (30자 이내)
- summary: 전체 요약 1~2문장

JSON만 출력하세요:
{"title":"...","summary":"...","scenes":[{"i":1,"caption":"...","narration":"..."}]}`;
}

// 시나리오 지시문: 재료 창고 목록만 보고 이야기를 창작 (재료에 없는 장면 사용 금지)
function buildStoryPrompt(objective, matList) {
  return `당신은 유튜브 숏츠 스토리텔링 작가입니다. 아래는 한 긴 영상을 잘게 잘라 만든 "재료 창고"(장면 사전)입니다. 원본은 잔잔한 다큐일 수 있지만, 시청자는 아무 생각 없이 쉬려고 숏츠를 봅니다. 재료들을 골라 순서를 바꿔 이어붙여, 원본에 없던 "새 이야기"를 창작하세요. 오락용 각색·과장·의인화·스토리 지어내기 전부 허용입니다 (단, 실존 인물 비방·의학/투자 허위정보는 금지).

예시: "엉덩이 비비는 장면" + "짝짓기 장면" + "새끼 장면"을 이어붙여 → "발정난 사슴이 눈이 맞아 1년 뒤 가족을 이뤘다"는 이야기로 재구성.

[재료 창고 — 각 재료는 #번호로 부릅니다]
${matList}

[규칙 — 어기면 실패]
1. 반드시 재료 창고에 있는 #번호만 사용하세요. 재료 설명(desc)에 없는 내용을 이야기 근거로 쓰지 마세요.
2. 스토리 2~3개. 각 스토리는 재료 2~6개를 이어붙임 (합계 20~90초).
3. 재료 순서는 재미가 우선 — 원본 시간 순서를 무시해도 됩니다.
4. 첫 재료 = 훅. 가장 강한 화면으로 시작하세요.
5. caption은 그 장면 위에 수강생이 얹을 자막 문구 — 짧고 강한 구어체(띄어쓰기 포함 22자 이내), 앞뒤가 이야기로 이어지게.
6. narration은 그 장면에서 수강생이 AI 목소리로 읽을 나레이션 한 문장 (자막보다 조금 길어도 됨).
7. title은 어그로형 제목 30자 이내. 창작 허용.
8. storyline은 줄거리 1~2문장. 점수는 0~10 정수.

JSON만 출력하세요:
{"stories":[{"title":"...","storyline":"...","hook_reason":"왜 터질 수 있는지 한 줄","scenes":[{"shot":12,"caption":"장면 위 자막","narration":"AI 목소리로 읽을 문장"}],"scores":{"hook":9,"standalone":8,"density":6,"emotion":8,"novelty":9,"proof":4,"duration_fit":8,"context_dep":1,"safety":9},"risk_flags":[]}]}`;
}

async function runAnalyze(job) {
  const { data: proj, error } = await sb.from("sc_projects")
    .select("id, source_path, objective, source_duration_sec").eq("id", job.project_id).single();
  if (error || !proj?.source_path) throw new Error("프로젝트/원본 경로 없음");
  const { data: tr } = await sb.from("sc_transcripts")
    .select("sentences").eq("project_id", proj.id).order("created_at", { ascending: false }).limit(1).maybeSingle();
  const sentences = (tr && Array.isArray(tr.sentences)) ? tr.sentences : [];

  const durationSec = Number(proj.source_duration_sec || 0);
  if (!durationSec) throw new Error("영상 길이 정보 없음 (검사부터 다시 필요)");
  const objective = proj.objective || "views";
  const chunkCount = Math.max(1, Math.ceil(durationSec / VIDEO_CHUNK_SEC));
  await setProjectStatus(proj.id, "analyzing", "재료 손질 준비 중…");

  const tmpDir = await mkdtemp(join(tmpdir(), "ib-an-"));
  let usedModel = "";
  const clamp10 = v => Math.max(0, Math.min(10, Math.round(Number(v) || 0)));
  const W = OBJECTIVE_WEIGHTS[objective] || OBJECTIVE_WEIGHTS.views;
  const weighted = scores => {
    const parts = {
      hook: scores.hook, novelty: scores.novelty, emotion: scores.emotion, density: scores.density,
      standalone: scores.standalone, proof: scores.proof, duration_fit: scores.duration_fit,
      context_indep: 10 - scores.context_dep,
    };
    let t = 0;
    for (const k of Object.keys(W)) t += W[k] * (parts[k] ?? 0);
    return Math.round(t * 100) / 100;
  };
  const clampSc = sc => ({
    hook: clamp10(sc.hook), standalone: clamp10(sc.standalone), density: clamp10(sc.density),
    emotion: clamp10(sc.emotion), novelty: clamp10(sc.novelty), proof: clamp10(sc.proof),
    duration_fit: clamp10(sc.duration_fit), context_dep: clamp10(sc.context_dep), safety: clamp10(sc.safety),
  });

  const shots = []; // 재료: {idx, chunk, ls, le(조각 내 시각), gs, ge(원본 시각), desc, pos, action, tags, usable}
  let stories = [];
  try {
    // ===== 1단계: 재료 손질 — 장면 전환을 감지해 잘게 자르기 =====
    const proxyPaths = [];
    let shotIdx = 0;
    for (let ci = 0; ci < chunkCount; ci++) {
      const offset = ci * VIDEO_CHUNK_SEC;
      const chunkDur = Math.min(VIDEO_CHUNK_SEC, Math.max(1, durationSec - offset));
      const { data: signed, error: se } = await sb.storage
        .from("videos-source").createSignedUrl(proj.source_path, 3600);
      if (se) throw new Error("서명 URL 실패: " + se.message);

      const proxyPath = join(tmpDir, "proxy-" + ci + ".mp4");
      const args = [];
      if (offset > 0) args.push("-ss", String(offset));
      args.push("-t", String(VIDEO_CHUNK_SEC), "-i", signed.signedUrl,
        "-vf", "scale=-2:360", "-r", "6",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "48k", "-ac", "1",
        proxyPath, "-y");
      await exec("ffmpeg", args, { timeout: 1800000 });
      proxyPaths.push(proxyPath);

      // 장면 전환 감지
      const metaPath = join(tmpDir, "scdet-" + ci + ".txt");
      await exec("ffmpeg", ["-i", proxyPath,
        "-vf", "select='gt(scene,0.30)',metadata=print:file=" + metaPath,
        "-an", "-f", "null", "-"], { timeout: 900000 });
      let cuts = [];
      try {
        const metaTxt = await readFile(metaPath, "utf8");
        cuts = [...metaTxt.matchAll(/pts_time:([0-9.]+)/g)].map(m => Number(m[1]))
          .filter(v => isFinite(v) && v > 0 && v < chunkDur).sort((a, b) => a - b);
      } catch {}

      // 경계 → 재료 목록 (너무 짧으면 앞에 합치고, 너무 길면 쪼갬)
      const bounds = [0, ...cuts, chunkDur];
      for (let bi = 0; bi < bounds.length - 1; bi++) {
        let s0 = bounds[bi];
        const e0 = bounds[bi + 1];
        if (e0 - s0 < SHOT_MIN_SEC) {
          const prev = shots[shots.length - 1];
          if (prev && prev.chunk === ci) { prev.le = e0; prev.ge = e0 + offset; }
          continue;
        }
        while (e0 - s0 > SHOT_MAX_SEC + 1) {
          shots.push({ idx: shotIdx++, chunk: ci, ls: s0, le: s0 + SHOT_MAX_SEC, gs: s0 + offset, ge: s0 + SHOT_MAX_SEC + offset });
          s0 += SHOT_MAX_SEC;
        }
        shots.push({ idx: shotIdx++, chunk: ci, ls: s0, le: e0, gs: s0 + offset, ge: e0 + offset });
      }
      await setProjectStatus(proj.id, "analyzing", `1/3 재료 손질 중… (${ci + 1}/${chunkCount} 구간, 재료 ${shots.length}개)`);
      console.log(`[analyze] p=${proj.id} 손질 ${ci + 1}/${chunkCount} — 누적 재료 ${shots.length}`);
    }
    if (shots.length < 5) throw new Error("장면이 너무 적어 재료를 만들 수 없습니다");
    if (shots.length > 600) shots.length = 600; // 폭주 방지

    // ===== 2단계: 재료 분석 — 조각을 묶어 AI에게 보여주고 "보이는 것만" 기록 =====
    let doneCnt = 0;
    for (let bi = 0; bi < shots.length; bi += SHOT_BATCH) {
      const batch = shots.slice(bi, bi + SHOT_BATCH);
      const files = [];
      for (const sh of batch) {
        const clipPath = join(tmpDir, `shot-${sh.idx}.mp4`);
        await exec("ffmpeg", ["-ss", String(sh.ls), "-t", String(Math.max(1, sh.le - sh.ls)),
          "-i", proxyPaths[sh.chunk],
          "-vf", "scale=-2:240", "-r", "4",
          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34",
          "-c:a", "aac", "-b:a", "32k", clipPath, "-y"], { timeout: 120000 });
        files.push(clipPath);

        // 소리 크기 측정 (함성·웃음이 터지는 순간은 소리가 큼)
        try {
          const { stderr } = await exec("ffmpeg", ["-ss", String(sh.ls), "-t", String(Math.max(1, sh.le - sh.ls)),
            "-i", proxyPaths[sh.chunk], "-vn", "-af", "volumedetect", "-f", "null", "-"], { timeout: 60000 });
          const mm = String(stderr).match(/max_volume:\s*(-?[0-9.]+)\s*dB/);
          sh.audio_db = mm ? Number(mm[1]) : -91;
        } catch { sh.audio_db = -91; }

        // 화면 움직임 측정 (프레임 간 변화량 평균)
        try {
          const mfile = join(tmpDir, `mot-${sh.idx}.txt`);
          await exec("ffmpeg", ["-i", clipPath, "-vf", "select='gte(scene,0)',metadata=print:file=" + mfile,
            "-an", "-f", "null", "-"], { timeout: 60000 });
          const mtxt = await readFile(mfile, "utf8");
          const vals = [...mtxt.matchAll(/scene_score=([0-9.]+)/g)].map(m => Number(m[1]));
          sh.motion = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
          await unlink(mfile).catch(() => {});
        } catch { sh.motion = 0; }
      }
      let gFiles = [];
      try {
        // 12개 조각을 동시에 업로드 (속도 개선)
        gFiles = await Promise.all(files.map(f => readFile(f).then(b => geminiUploadAudio(b, "video/mp4"))));
        const r = await callGemini({
          contents: [{ parts: [
            ...gFiles.map(g => ({ file_data: { file_uri: g.uri, mime_type: "video/mp4" } })),
            { text: buildIngredientPrompt(batch.length) },
          ] }],
          generationConfig: {
            responseMimeType: "application/json", temperature: 0.1,
            mediaResolution: "MEDIA_RESOLUTION_LOW", maxOutputTokens: 16384,
          },
        });
        usedModel = r.model;
        let out = parseJsonLoose(geminiText(r.gj));
        if (Array.isArray(out)) out = { clips: out };
        const clips = (out && Array.isArray(out.clips)) ? out.clips : [];
        batch.forEach((sh, k) => {
          const c = clips.find(x => Number(x.i) === k + 1) || clips[k] || {};
          sh.desc = String(c.desc || "").slice(0, 200);
          sh.pos = ["left", "center", "right"].includes(c.subject_pos) ? c.subject_pos : "center";
          sh.action = String(c.action || "").slice(0, 60);
          sh.event = Math.max(0, Math.min(10, Math.round(Number(c.event) || 0)));
          sh.tags = Array.isArray(c.tags) ? c.tags.slice(0, 4).map(t => String(t).slice(0, 20)) : [];
          sh.usable = c.usable !== false && !!sh.desc;
        });
      } catch (e) {
        // 한 묶음 실패해도 전체는 계속 (실패 재료는 사용 안 함)
        console.error(`[analyze] 재료 묶음 분석 실패 (${bi}~): ${e.message}`);
        batch.forEach(sh => { sh.desc = ""; sh.usable = false; });
      } finally {
        for (const g of gFiles) await geminiDeleteFile(g.name);
        for (const f of files) await unlink(f).catch(() => {});
      }
      doneCnt += batch.length;
      await setJobProgress(job.id, (doneCnt / shots.length) * 80);
      await setProjectStatus(proj.id, "analyzing", `2/3 재료 분석 중… (${doneCnt}/${shots.length})`);
      console.log(`[analyze] p=${proj.id} 재료 분석 ${doneCnt}/${shots.length}`);
    }

    // 재료 창고 저장 (다시 실행해도 겹치지 않게 이전 것 삭제)
    await sb.from("sc_shots").delete().eq("project_id", proj.id);
    const shotRows = shots.map(sh => ({
      project_id: proj.id, idx: sh.idx,
      start_ms: Math.round(sh.gs * 1000), end_ms: Math.round(sh.ge * 1000),
      description: sh.desc || "", subject_pos: sh.pos || "center",
      action: sh.action || "", tags: sh.tags || [], usable: !!sh.usable,
      event: sh.event || 0, sig: { audio_db: sh.audio_db ?? -91, motion: Math.round((sh.motion || 0) * 1000) / 1000 },
    }));
    for (let i = 0; i < shotRows.length; i += 200) {
      const { error: se2 } = await sb.from("sc_shots").insert(shotRows.slice(i, i + 200));
      if (se2) throw new Error("재료 저장 실패: " + se2.message);
    }

    // ===== 하이라이트 조립 — 스포츠 뉴스 방식: 사건 순간만 골라 시간순으로 =====
    // 점수 = AI 사건성 45% + 소리 크기 20% + 화면 움직임 20% + 대사 흥분 단어 15%
    var highlight = null;
    try {
      const HOT_WORDS = ["최초", "충격", "드디어", "놀라", "기적", "포착", "성공", "위기", "탄생", "발견", "결정적", "믿을 수 없", "!"];
      const okShots = shots.filter(s => s.usable && s.desc);
      const audioVals = okShots.map(s => s.audio_db ?? -91).sort((a, b) => a - b);
      const motionVals = okShots.map(s => s.motion || 0).sort((a, b) => a - b);
      const pct = (arr, v) => arr.length ? arr.filter(x => x <= v).length / arr.length : 0.5;
      const scored = okShots.map(s => {
        const talk = sentences.filter(x => x.s / 1000 < s.ge && x.e / 1000 > s.gs).map(x => x.text).join(" ");
        const speech = HOT_WORDS.some(w => talk.includes(w)) ? 1 : 0;
        const score = 0.45 * (s.event || 0) / 10 + 0.2 * pct(audioVals, s.audio_db ?? -91)
          + 0.2 * pct(motionVals, s.motion || 0) + 0.15 * speech;
        return { s, score };
      }).sort((a, b) => b.score - a.score);
      const picked = [];
      let totalSecH = 0;
      for (const { s } of scored) {
        const d = s.ge - s.gs;
        if (totalSecH + d > 65) continue;
        picked.push(s); totalSecH += d;
        if (totalSecH >= 50 || picked.length >= 8) break;
      }
      if (picked.length >= 3 && totalSecH >= 25) {
        picked.sort((a, b) => a.gs - b.gs); // 시간 순서 유지 (하이라이트의 핵심)
        const merged = [];
        for (const s of picked) {
          const last = merged[merged.length - 1];
          if (last && Math.abs(last.ge - s.gs) < 0.2) { last.ge = s.ge; last.descs.push(s.desc); }
          else merged.push({ gs: s.gs, ge: s.ge, pos: s.pos || "center", descs: [s.desc] });
        }
        const sceneList = merged.map((m, i) =>
          `${i + 1}. [${fmtTime(m.gs * 1000)}~${fmtTime(m.ge * 1000)}] ${m.descs.join(" / ")}`).join("\n");
        let hl = null;
        try {
          const r3 = await callGemini({
            contents: [{ parts: [{ text: buildHighlightPrompt(sceneList) }] }],
            generationConfig: { responseMimeType: "application/json", temperature: 0.5, maxOutputTokens: 8192 },
          });
          hl = parseJsonLoose(geminiText(r3.gj));
        } catch (e) { console.error("[analyze] 하이라이트 대본 실패(자막 없이 진행):", e.message); }
        const hscenes = (hl && Array.isArray(hl.scenes)) ? hl.scenes : [];
        const r1x = v => Math.round(v * 10) / 10;
        const segs = merged.map((m, i) => {
          const sc0 = hscenes.find(x => Number(x.i) === i + 1) || hscenes[i] || {};
          return {
            in_s: r1x(m.gs), out_s: r1x(m.ge), pos: m.pos,
            caption: String(sc0.caption || "").slice(0, 60),
            narration: String(sc0.narration || "").slice(0, 200),
          };
        });
        const hScores = clampSc({ hook: 7, standalone: 8, density: 8, emotion: 7, novelty: 7, proof: 8, duration_fit: 8, context_dep: 2, safety: 9 });
        highlight = {
          segments: segs,
          start_ms: Math.round(segs[0].in_s * 1000),
          end_ms: Math.round(segs[0].in_s * 1000 + totalSecH * 1000),
          title: String((hl && hl.title) || "하이라이트 모음").slice(0, 80),
          hook_reason: "[하이라이트] 사건 점수 + 소리 + 움직임 상위 장면을 시간 순서 그대로 모음 (창작 없음)",
          summary: String((hl && hl.summary) || "").slice(0, 300),
          scores: hScores, total_score: weighted(hScores),
          risk_flags: [],
        };
        console.log(`[analyze] p=${proj.id} 하이라이트 조립: 장면 ${segs.length}개, 총 ${Math.round(totalSecH)}초`);
      } else {
        console.log(`[analyze] p=${proj.id} 하이라이트 재료 부족 (${picked.length}개/${Math.round(totalSecH)}초)`);
      }
    } catch (e) {
      console.error("[analyze] 하이라이트 조립 실패:", e.message);
    }

    // ===== 3단계: 시나리오 작성 — 창고에 있는 재료만 사용 =====
    const usable = shots.filter(s => s.usable && s.desc);
    if (usable.length < 5) throw new Error("쓸만한 재료가 너무 적습니다 (" + usable.length + "개)");
    const matList = usable.map(s =>
      `#${s.idx} [${fmtTime(s.gs * 1000)}~${fmtTime(s.ge * 1000)}, ${Math.round(s.ge - s.gs)}초] ${s.desc}` +
      (s.action ? ` | 행동: ${s.action}` : "")).join("\n");
    await setProjectStatus(proj.id, "analyzing", "3/3 AI가 재료로 이야기를 짜는 중…");
    const r2 = await callGemini({
      contents: [{ parts: [{ text: buildStoryPrompt(objective, matList) }] }],
      generationConfig: { responseMimeType: "application/json", temperature: 0.8, maxOutputTokens: 32768 },
    });
    usedModel = r2.model;
    let so = parseJsonLoose(geminiText(r2.gj));
    if (Array.isArray(so)) so = { stories: so };
    const byIdx = new Map(shots.map(s => [s.idx, s]));
    const r1 = v => Math.round(v * 10) / 10;
    for (const s of (so && Array.isArray(so.stories) ? so.stories : [])) {
      const scenes = Array.isArray(s.scenes) ? s.scenes : [];
      const segs = [];
      let valid = true;
      for (const sc0 of scenes) {
        const sh = byIdx.get(Number(sc0.shot));
        if (!sh || !sh.usable) { valid = false; break; } // 창고에 없는 재료를 쓰면 그 스토리 폐기
        segs.push({
          in_s: r1(sh.gs), out_s: r1(sh.ge), shot: sh.idx, pos: sh.pos || "center",
          caption: String(sc0.caption || "").slice(0, 60),
          narration: String(sc0.narration || "").slice(0, 200),
        });
      }
      if (!valid || segs.length < 2 || segs.length > 6) continue;
      const totalSec = segs.reduce((a, g) => a + (g.out_s - g.in_s), 0);
      if (totalSec < 12 || totalSec > 95) continue;
      const scores = clampSc(s.scores || {});
      stories.push({
        segments: segs,
        start_ms: Math.round(segs[0].in_s * 1000),
        end_ms: Math.round(segs[0].in_s * 1000 + totalSec * 1000),
        title: String(s.title || "").slice(0, 80),
        hook_reason: "[스토리 짜집기] " + String(s.hook_reason || "").slice(0, 240),
        summary: String(s.storyline || s.summary || "").slice(0, 300),
        scores, total_score: weighted(scores),
        risk_flags: Array.isArray(s.risk_flags) ? s.risk_flags.slice(0, 5).map(x => String(x).slice(0, 100)) : [],
      });
    }
    stories.sort((a, b) => b.total_score - a.total_score);
    stories = stories.slice(0, 3);
    if (!stories.length && !highlight) throw new Error("하이라이트·이야기 구성에 모두 실패했습니다 (다시 시도해 주세요)");
  } finally {
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }

  // 후보 저장 (이전 후보 삭제 후) — 하이라이트가 맨 앞
  await sb.from("sc_candidates").delete().eq("project_id", proj.id);
  const rows = [
    ...(highlight ? [{
      project_id: proj.id, kind: "highlight", segments: highlight.segments,
      start_ms: highlight.start_ms, end_ms: highlight.end_ms, title: highlight.title,
      hook_reason: highlight.hook_reason, summary: highlight.summary, scores: highlight.scores,
      total_score: highlight.total_score, risk_flags: highlight.risk_flags,
      model_info: { model: usedModel, prompt_ver: PROMPT_VER, objective },
    }] : []),
    ...stories.map(c => ({
      project_id: proj.id, kind: "story", segments: c.segments,
      start_ms: c.start_ms, end_ms: c.end_ms, title: c.title,
      hook_reason: c.hook_reason, summary: c.summary, scores: c.scores,
      total_score: c.total_score, risk_flags: c.risk_flags,
      model_info: { model: usedModel, prompt_ver: PROMPT_VER, objective },
    })),
  ];
  const { error: ie } = await sb.from("sc_candidates").insert(rows);
  if (ie) throw new Error("후보 저장 실패: " + ie.message);

  await setProjectStatus(proj.id, "candidates_ready",
    "재료 " + shots.length + "개 · 하이라이트 " + (highlight ? 1 : 0) + "개 · 스토리 " + stories.length + "개");
  await sb.from("sc_usage_log").insert({
    project_id: proj.id, kind: "analyze", duration_sec: durationSec,
    meta: { shots: shots.length, highlight: !!highlight, stories: stories.length, model: usedModel, objective, prompt_ver: PROMPT_VER },
  });
  console.log(`[analyze] 완료 p=${proj.id} 재료=${shots.length} 하이라이트=${highlight ? 1 : 0} 스토리=${stories.length}`);
}

// ---------- 작업: render (숏츠 영상 만들기 — 자르고 붙이고 자막) ----------
import { writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";

function pickFont() {
  const fonts = [
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
  ];
  return fonts.find(f => existsSync(f)) || "";
}

// 자막 줄바꿈: 공백 기준으로 한 줄 최대 12자 안팎
function wrapCaption(text, maxLen = 12) {
  const words = String(text || "").trim().split(/\s+/);
  const lines = [];
  let cur = "";
  for (const w of words) {
    if ((cur + " " + w).trim().length > maxLen && cur) { lines.push(cur); cur = w; }
    else cur = (cur + " " + w).trim();
  }
  if (cur) lines.push(cur);
  return lines.slice(0, 3).join("\n");
}

async function runRender(job) {
  const { data: rec, error: re } = await sb.from("sc_recipes")
    .select("id, project_id, manifest").eq("id", job.recipe_id).single();
  if (re || !rec) throw new Error("레시피 없음");
  const { data: proj, error: pe } = await sb.from("sc_projects")
    .select("id, user_id, source_path").eq("id", rec.project_id).single();
  if (pe || !proj?.source_path) throw new Error("프로젝트/원본 없음");
  const segs = (rec.manifest && Array.isArray(rec.manifest.segments)) ? rec.manifest.segments : [];
  if (!segs.length) throw new Error("레시피에 장면이 없습니다");
  if (segs.length > 8) throw new Error("장면이 너무 많습니다 (최대 8개)");

  await setProjectStatus(proj.id, "rendering", "숏츠 영상을 만드는 중… (0/" + segs.length + ")");
  const tmpDir = await mkdtemp(join(tmpdir(), "ib-rd-"));
  try {
    const partFiles = [];
    for (let i = 0; i < segs.length; i++) {
      const g = segs[i];
      const inS = Math.max(0, Number(g.in_s) || 0);
      const outS = Number(g.out_s) || 0;
      const dur = outS - inS;
      if (dur < 1 || dur > 60) throw new Error((i + 1) + "번째 장면 길이가 비정상입니다");
      const { data: signed, error: se } = await sb.storage
        .from("videos-source").createSignedUrl(proj.source_path, 3600);
      if (se) throw new Error("서명 URL 실패: " + se.message);

      // 세로 1080x1920 변환 — 주인공 위치(left/center/right)를 따라 잘라냄
      // 자막·소리는 넣지 않음 (수강생이 직접 AI 목소리와 자막을 입힘 — 사장님 지시)
      const posX = g.pos === "left" ? "0" : g.pos === "right" ? "iw-ow" : "(iw-ow)/2";
      const vf = `crop='min(iw,ih*9/16)':ih:${posX}:0,scale=1080:1920,setsar=1`;
      const partPath = join(tmpDir, "part-" + i + ".mp4");
      await exec("ffmpeg", [
        "-ss", String(inS), "-t", String(dur), "-i", signed.signedUrl,
        "-vf", vf, "-r", "30", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        partPath, "-y",
      ], { timeout: 900000 });
      partFiles.push(partPath);
      await setJobProgress(job.id, ((i + 1) / (segs.length + 1)) * 100);
      await setProjectStatus(proj.id, "rendering", "숏츠 영상을 만드는 중… (" + (i + 1) + "/" + segs.length + ")");
    }

    // 장면 이어붙이기 (모두 같은 규격으로 인코딩했으므로 무손실 이어붙임)
    const listPath = join(tmpDir, "list.txt");
    await writeFile(listPath, partFiles.map(p => `file '${p}'`).join("\n"), "utf8");
    const outPath = join(tmpDir, "final.mp4");
    await exec("ffmpeg", ["-f", "concat", "-safe", "0", "-i", listPath, "-c", "copy", "-movflags", "+faststart", outPath, "-y"], { timeout: 300000 });

    // 완성본 업로드 (본인 폴더)
    const clipPath = proj.user_id + "/" + rec.id + ".mp4";
    const buf = await readFile(outPath);
    const { error: ue } = await sb.storage.from("videos-clips")
      .upload(clipPath, buf, { contentType: "video/mp4", upsert: true });
    if (ue) throw new Error("완성본 업로드 실패: " + ue.message);

    await sb.from("sc_recipes").update({ render_path: clipPath }).eq("id", rec.id);
    await setProjectStatus(proj.id, "done", "숏츠 완성! 후보 화면에서 내려받을 수 있어요.");
    await sb.from("sc_usage_log").insert({
      project_id: proj.id, kind: "render",
      duration_sec: segs.reduce((a, g) => a + ((Number(g.out_s) || 0) - (Number(g.in_s) || 0)), 0),
      meta: { segments: segs.length, bytes: buf.length, recipe_id: rec.id },
    });
    console.log(`[render] 완료 r=${rec.id} 장면=${segs.length} 크기=${Math.round(buf.length / 1e6)}MB`);
  } finally {
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }
}

// ---------- GPU 일꾼(RunPod) 위임: 계획 → 3대 병렬 작업 → 병합 ----------
async function gpuEndpointBase() {
  let ep = process.env.RUNPOD_ENDPOINT_ID;
  try {
    const { data } = await sb.from("app_settings").select("value").eq("key", "wm_gpu_tier").maybeSingle();
    if (data && String(data.value).toLowerCase() === "h100" && process.env.RUNPOD_ENDPOINT_ID_H100) ep = process.env.RUNPOD_ENDPOINT_ID_H100;
  } catch {}
  return "https://api.runpod.ai/v2/" + ep;
}
async function rpCall(base, input, capMs = 2400000) {
  const hdr = { "Authorization": "Bearer " + process.env.RUNPOD_API_KEY, "Content-Type": "application/json" };
  const r = await fetch(base + "/run", { method: "POST", headers: hdr, body: JSON.stringify({ input }) });
  if (!r.ok) throw new Error("RunPod /run HTTP " + r.status + " " + (await r.text()).slice(0, 200));
  const { id } = await r.json();
  const t0 = Date.now();
  for (;;) {
    await new Promise((res) => setTimeout(res, 5000));
    const s = await fetch(base + "/status/" + id, { headers: hdr });
    if (!s.ok) throw new Error("RunPod /status HTTP " + s.status);
    const j = await s.json();
    if (j.status === "COMPLETED") {
      if (j.output && j.output.error) throw new Error("GPU 처리 오류: " + String(j.output.error).slice(0, 200));
      return j.output || {};
    }
    if (j.status === "FAILED" || j.status === "CANCELLED" || j.status === "TIMED_OUT") throw new Error("RunPod 상태: " + j.status);
    if (Date.now() - t0 > capMs) {
      try { await fetch(base + "/cancel/" + id, { method: "POST", headers: hdr }); } catch {}
      throw new Error("GPU 단계 시간 초과(" + Math.round(capMs / 60000) + "분)");
    }
  }
}
async function rpCallRetry(base, input, tries = 2, capMs = 2400000) {
  // 조각 단위 재시도: 일시 오류(저장소 삐끗, 일꾼 교체 등) 1번으로 전체 작업이
  // 값비싼 예비 경로(Replicate)로 넘어가는 것을 방지. 각 단계는 재실행해도 안전(멱등).
  let last;
  for (let i = 0; i < tries; i++) {
    try { return await rpCall(base, input, capMs); }
    catch (e) { last = e; if (i < tries - 1) await new Promise((res) => setTimeout(res, 8000)); }
  }
  throw last;
}
async function planCapMs() {
  // 감지 단계 제한시간(분). app_settings의 wm_plan_cap_min으로 조절 가능(기본 15분).
  // 강제발동 시험 시 이 값을 1로 바꾸면 재배포 없이 타임아웃 경로를 점검할 수 있다.
  let m = 15;
  try {
    const { data } = await sb.from("app_settings").select("value").eq("key", "wm_plan_cap_min").maybeSingle();
    if (data && data.value != null) { const v = parseInt(data.value, 10); if (v >= 1 && v <= 60) m = v; }
  } catch {}
  return m * 60000;
}
async function runWmRemoveGpu(job) {
  const base = await gpuEndpointBase();
  const t0 = Date.now() / 1000;
  try { await setJobProgress(job.id, 5); } catch {}
  const pCap = await planCapMs();
  let plan;
  try {
    plan = await rpCall(base, { project_id: job.project_id, phase: "plan", t0, scan_step: 12 }, pCap);
  } catch (e1) {
    console.error("[wm-gpu] 감지 1차 실패, 표본 간격 2배로 재시도:", e1.message);
    try {
      await sb.from("sc_projects").update({ status: "wm_running", status_detail: "감지가 오래 걸려 방식을 바꿔 다시 시도하는 중…" }).eq("id", job.project_id);
    } catch {}
    try {
      plan = await rpCall(base, { project_id: job.project_id, phase: "plan", t0, scan_step: 24 }, pCap);
    } catch (e2) {
      try {
        await sb.from("sc_projects").update({ status: "failed_wm", status_detail: "자동 감지에 실패했습니다. [직접 지정] 모드로 자막 위치를 그려주시면 빠르게 처리됩니다." }).eq("id", job.project_id);
      } catch {}
      const err = new Error("감지 단계 2회 실패: " + e2.message);
      err.noFallback = true;
      throw err;
    }
  }
  if (plan.note === "no_target") return;
  try { await setJobProgress(job.id, 30); } catch {}
  const total = plan.chunks || 0;
  const PARTS = total >= 6 ? 3 : total >= 2 ? 2 : 1;
  try {
    await sb.from("sc_projects").update({ status: "wm_running", status_detail: "AI가 배경을 복원하는 중… (GPU " + PARTS + "대 동시 작업)" }).eq("id", job.project_id);
  } catch {}
  const works = await Promise.all(Array.from({ length: PARTS }, (_, k) => rpCallRetry(base, { project_id: job.project_id, phase: "work", part: k, parts: PARTS, t0 })));
  try {
    await sb.from("sc_projects").update({ status: "wm_running", status_detail: "복원한 부분을 원본에 합치는 중… (GPU " + PARTS + "대 동시 작업)" }).eq("id", job.project_id);
  } catch {}
  try { await setJobProgress(job.id, 85); } catch {}
  const segs = await Promise.all(Array.from({ length: PARTS }, (_, k) => rpCallRetry(base, { project_id: job.project_id, phase: "mergeseg", part: k, parts: PARTS, t0 })));
  try { await setJobProgress(job.id, 95); } catch {}
  const tms = { plan: plan.tms || {}, work: works.map(w => (w && w.tms) || {}), seg: segs.map(s => (s && s.tms) || {}) };
  await rpCallRetry(base, { project_id: job.project_id, phase: "finish", parts: PARTS, t0, tms });
}

// ---------- 메인 루프 ----------
console.log(`[시작] 렌더 서버 ${WORKER_ID} — ${POLL_MS / 1000}초 간격 대기열 감시`);
let flagsEnsured = false;
async function ensureFlags() {
  try {
    const { data } = await sb.from("feature_flags").select("key").eq("key", "video_desilence").maybeSingle();
    if (!data) await sb.from("feature_flags").insert({ key: "video_desilence", name: "영상 무음 제거", is_public: false });
  } catch (e) { console.error("[flag] ensure 실패:", e.message); }
}

function parseSilence(txt, durationSec) {
  const sil = []; let curStart = null;
  const re = /silence_(start|end):\s*([0-9.]+)/g; let m;
  while ((m = re.exec(txt))) {
    if (m[1] === "start") curStart = parseFloat(m[2]);
    else { if (curStart != null) { sil.push([curStart, parseFloat(m[2])]); curStart = null; } }
  }
  if (curStart != null) sil.push([curStart, durationSec]);
  return sil;
}

function keepRanges(silences, durationSec, keepPad) {
  const cuts = [];
  for (let i = 0; i < silences.length; i++) {
    const cs = silences[i][0] + keepPad, ce = silences[i][1] - keepPad;
    if (ce - cs > 0.05) cuts.push([cs, ce]);
  }
  const keeps = []; let pos = 0;
  for (let i = 0; i < cuts.length; i++) { if (cuts[i][0] > pos) keeps.push([pos, cuts[i][0]]); pos = cuts[i][1]; }
  if (pos < durationSec) keeps.push([pos, durationSec]);
  let cutTotal = 0; for (let i = 0; i < cuts.length; i++) cutTotal += cuts[i][1] - cuts[i][0];
  return { keeps: keeps, cutTotal: cutTotal };
}

// ---------- 작업: desilence (영상 무음 구간 잘라내기) ----------
async function vadKeepRanges(srcPath, tmpDir, dur, dMin) {
  const wavPath = join(tmpDir, "audio16k.wav");
  await exec("ffmpeg", ["-hide_banner","-nostats","-i", srcPath, "-vn","-ac","1","-ar","16000","-c:a","pcm_s16le","-f","wav", wavPath, "-y"], { timeout: 600000, maxBuffer: 16*1024*1024 });
  const minSilenceMs = Math.round(Math.max(0.05, Math.min(2, dMin)) * 1000);
  const padMs = Math.round(Math.min(0.15, Math.max(0.08, dMin * 0.3)) * 1000);
  const res = await exec("python3", ["/app/vad.py", wavPath, String(minSilenceMs), String(padMs)], { timeout: 1500000, maxBuffer: 64*1024*1024 });
  const parsed = JSON.parse((res.stdout || "").trim());
  const speech = parsed.speech || [];
  if (!speech.length) return { keeps: [[0, dur]], cutTotal: 0 };
  const keeps = speech.map(function(s){ return [Math.max(0, s[0]), Math.min(dur, s[1])]; }).filter(function(s){ return s[1] > s[0]; });
  const kept = keeps.reduce(function(a, s){ return a + (s[1] - s[0]); }, 0);
  return { keeps: keeps, cutTotal: Math.max(0, dur - kept) };
}

async function runDesilence(job) {
  const { data: proj, error } = await sb.from("sc_projects")
    .select("id, user_id, source_path, source_duration_sec, probe, objective, desilence_min").eq("id", job.project_id).single();
  if (error || !proj || !proj.source_path) throw new Error("프로젝트/원본 없음");

  const failStatus = async (msg) => {
    if ((job.attempts || 0) >= 1) await setProjectStatus(proj.id, "failed_desilence", String(msg).slice(0, 200));
  };
  try {
    await setProjectStatus(proj.id, "desilence_running", "무음 구간을 찾는 중…");
    const { data: signed, error: se } = await sb.storage.from("videos-source").createSignedUrl(proj.source_path, 21600);
    if (se) throw new Error("서명 URL 실패: " + se.message);
    const url = signed.signedUrl;

    let dur = proj.source_duration_sec || 0;
    let hasAudio = !!(proj.probe && proj.probe.audio_codec);
    if (!dur || !proj.probe) {
      const { stdout } = await exec("ffprobe", ["-v","error","-print_format","json","-show_format","-show_streams", url], { timeout: 120000, maxBuffer: 16*1024*1024 });
      const info = JSON.parse(stdout); dur = parseFloat((info.format || {}).duration || 0);
      hasAudio = (info.streams || []).some((s) => s.codec_type === "audio");
    }
    if (!hasAudio) throw new Error("소리가 없는 영상은 무음을 찾을 수 없어요.");
    if (!dur) throw new Error("영상 길이를 확인하지 못했어요.");
    if (dur > 10800) throw new Error("지금은 3시간 이하 영상만 지원해요. 더 긴 영상은 나눠서 넣어주세요.");

    const dMin = Math.max(0.05, Math.min(2, Number(proj.desilence_min) || 0.4));
    await setJobProgress(job.id, 15);
    const tmpDir = await mkdtemp(join(tmpdir(), "ib-ds-"));
    try {
      const clipPath = proj.user_id + "/desilence_" + proj.id + ".mp4";
      const outPath = join(tmpDir, "out.mp4");
      const srcPath = join(tmpDir, "src.mkv");
      await setProjectStatus(proj.id, "desilence_running", "영상을 받아 오는 중…");
      await downloadToFile(url, srcPath, proj.id);
      await setProjectStatus(proj.id, "desilence_running", "무음 구간을 찾는 중…");
      let keeps, cutTotal, detMethod;
      try {
        const vr = await vadKeepRanges(srcPath, tmpDir, dur, dMin);
        keeps = vr.keeps; cutTotal = vr.cutTotal; detMethod = "vad";
      } catch (ve) {
        console.error("[VAD] \uc2e4\ud328 \u2192 dB \ubc29\uc2dd\uc73c\ub85c \ub300\uccb4: " + ((ve && ve.message) || ve));
        const det = await exec("ffmpeg", ["-hide_banner","-nostats","-i", srcPath, "-af", ("silencedetect=noise=-40dB:d=" + dMin), "-f","null","-"], { timeout: 1500000, maxBuffer: 64*1024*1024 });
        const silences = parseSilence((det.stderr || "") + (det.stdout || ""), dur);
        const kr = keepRanges(silences, dur, Math.min(0.05, dMin*0.4));
        keeps = kr.keeps; cutTotal = kr.cutTotal; detMethod = "db";
      }
      console.log("[\ubb34\uc74c\ud0d0\uc9c0] method=" + detMethod + " keeps=" + keeps.length + " cut=" + Math.round(cutTotal) + "s");
      let srcFps = 30;
      try {
        const fp = await exec("ffprobe", ["-v","error","-select_streams","v:0","-show_entries","stream=r_frame_rate","-of","default=nw=1:nk=1", srcPath], { timeout: 60000, maxBuffer: 4*1024*1024 });
        const pr = String(fp.stdout).trim().split("/");
        const f = (pr.length === 2 && Number(pr[1])) ? Number(pr[0]) / Number(pr[1]) : Number(pr[0]);
        if (f > 0 && f <= 120) srcFps = Math.round(f * 1000) / 1000;
      } catch (e) {}
      const vts = String(Math.max(600, Math.round(srcFps * 1000)));
      if (cutTotal < 1 || keeps.length === 0) {
        await setProjectStatus(proj.id, "desilence_running", "무음이 거의 없어 그대로 저장 중…");
        await exec("ffmpeg", ["-hide_banner","-nostats","-loglevel","error","-i", srcPath, "-c:v","libx264","-preset","medium","-crf","16","-pix_fmt","yuv420p","-c:a","aac","-b:a","256k","-movflags","+faststart", outPath, "-y"], { timeout: 2400000, maxBuffer: 128*1024*1024 });
      } else {
        const Q = String.fromCharCode(39);
        const NL = String.fromCharCode(10);
        await setProjectStatus(proj.id, "desilence_running", "무음을 잘라 이어붙이는 중… (" + keeps.length + "개 구간, 길면 몇 분 걸려요)");
        await setJobProgress(job.id, 40);
        let vlist = "", alist = "";
        for (let i = 0; i < keeps.length; i++) {
          const s0 = Math.round(keeps[i][0] * srcFps) / srcFps;
          const e0 = Math.round(keeps[i][1] * srcFps) / srcFps;
          const segDur = Math.max(1 / srcFps, e0 - s0);
          const vPath = join(tmpDir, "v_" + String(i).padStart(5, "0") + ".ts");
          const aPath = join(tmpDir, "a_" + String(i).padStart(5, "0") + ".wav");
          await exec("ffmpeg", ["-hide_banner","-nostats","-loglevel","error","-ss", s0.toFixed(3), "-t", segDur.toFixed(3), "-i", srcPath, "-an","-c:v","libx264","-preset","medium","-crf","16","-pix_fmt","yuv420p","-r", String(srcFps), "-vsync","cfr","-f","mpegts", vPath, "-vn","-c:a","pcm_s16le", aPath, "-y"], { timeout: 600000, maxBuffer: 32*1024*1024 });
          vlist += "file " + Q + vPath + Q + NL;
          alist += "file " + Q + aPath + Q + NL;
          if (i % 12 === 0) { await setJobProgress(job.id, Math.min(80, 40 + Math.round((i / keeps.length) * 40))); }
        }
        await writeFile(join(tmpDir, "vlist.txt"), vlist, "utf8");
        await writeFile(join(tmpDir, "alist.txt"), alist, "utf8");
        const vidPath = join(tmpDir, "vid.mp4");
        const audPath = join(tmpDir, "aud.m4a");
        await exec("ffmpeg", ["-hide_banner","-nostats","-loglevel","error","-f","concat","-safe","0","-i", join(tmpDir, "vlist.txt"), "-an","-c:v","copy","-video_track_timescale", vts, "-movflags","+faststart", vidPath, "-y"], { timeout: 1800000, maxBuffer: 128*1024*1024 });
        await setJobProgress(job.id, 84);
        await exec("ffmpeg", ["-hide_banner","-nostats","-loglevel","error","-f","concat","-safe","0","-i", join(tmpDir, "alist.txt"), "-c:a","aac","-b:a","256k", audPath, "-y"], { timeout: 1800000, maxBuffer: 128*1024*1024 });
        await setJobProgress(job.id, 88);
        await exec("ffmpeg", ["-hide_banner","-nostats","-loglevel","error","-i", vidPath, "-i", audPath, "-c","copy","-movflags","+faststart", outPath, "-y"], { timeout: 600000, maxBuffer: 128*1024*1024 });
      }

      await setJobProgress(job.id, 90);
      const buf = await readFile(outPath);
      const { error: ue } = await sb.storage.from("videos-clips").upload(clipPath, buf, { contentType: "video/mp4", upsert: true });
      if (ue) throw new Error("완성본 업로드 실패: " + ue.message);
      const { data: rsigned, error: rse } = await sb.storage.from("videos-clips").createSignedUrl(clipPath, 86400);
      if (rse) throw new Error("결과 링크 생성 실패: " + rse.message);
      const newDur = Math.max(0, dur - cutTotal);
      const detail = JSON.stringify({ result_url: rsigned.signedUrl, orig_sec: Math.round(dur), new_sec: Math.round(newDur), cut_sec: Math.round(cutTotal), spots: keeps.length, min: dMin, cutmap: (function(){var NB=100,cb=new Array(NB).fill(0);for(var i=0;i<keeps.length;i++){var a=Math.max(0,keeps[i][0]),b=Math.min(dur,keeps[i][1]);if(b<=a)continue;var lo=Math.floor(a/dur*NB),hi=Math.ceil(b/dur*NB);for(var bi=lo;bi<hi&&bi<NB;bi++){var bs=bi/NB*dur,be=(bi+1)/NB*dur;cb[bi]+=Math.max(0,Math.min(b,be)-Math.max(a,bs));}}var bl=dur/NB;return cb.map(function(k){return Math.max(0,Math.min(100,Math.round((1-k/bl)*100)));});})() });
      await sb.from("sc_projects").update({ status: "desilence_done", status_detail: detail, updated_at: new Date().toISOString() }).eq("id", proj.id);
      /* 원본과 완성본은 24시간 뒤 cleanupExpired에서 함께 삭제 (그 사이 다시 자르기 가능) */
      try { await sb.from("sc_usage_log").insert({ project_id: proj.id, kind: "desilence", duration_sec: dur, meta: { cut_sec: Math.round(cutTotal) } }); } catch (e) {}
      console.log("[desilence] 완료 p=" + proj.id + " 원본=" + Math.round(dur) + "s 컷=" + Math.round(cutTotal) + "s");
    } finally {
      try { await rm(tmpDir, { recursive: true, force: true }); } catch (e) {}
    }
  } catch (e) {
    await failStatus(e.message);
    throw e;
  }
}

let lastCleanup = 0;
async function cleanupExpired() {
  try {
    const cutoff = new Date(Date.now() - 24*3600*1000).toISOString();
    const { data: rows } = await sb.from("sc_projects")
      .select("id, user_id, source_path")
      .eq("objective", "desilence").eq("status", "desilence_done")
      .is("cleaned_at", null).lt("updated_at", cutoff).limit(20);
    if (!rows || !rows.length) return;
    for (const p of rows) {
      const clip = p.user_id + "/desilence_" + p.id + ".mp4";
      try { await sb.storage.from("videos-clips").remove([clip]); } catch (e) {}
      if (p.source_path) { try { await sb.storage.from("videos-source").remove([p.source_path]); } catch (e) {} }
      await sb.from("sc_projects").update({ cleaned_at: new Date().toISOString() }).eq("id", p.id);
      console.log("[정리] 24시간 경과 삭제 " + p.id);
    }
  } catch (e) { console.error("[정리] cleanupExpired 실패:", e.message); }
}

async function recoverStuck() {
  try {
    const cutoff = new Date(Date.now() - 5400000).toISOString();
    const { data: stuck } = await sb.from("sc_render_jobs").select("id, project_id, job_type, started_at").eq("status", "running").lt("started_at", cutoff).limit(20);
    for (const j of (stuck || [])) {
      await sb.from("sc_render_jobs").update({ status: "failed", error: "처리 시간 초과(자동 복구)" }).eq("id", j.id).eq("status", "running");
      if (j.job_type === "desilence") await setProjectStatus(j.project_id, "failed_desilence", "처리 시간이 초과되어 자동 중단되었습니다. 다시 시도해 주세요.");
      console.log("[복구] 멈춘 작업 정리 job " + j.id);
    }
  } catch (e) { console.error("[복구] 오류:", e.message); }
}


// ================= 유튜브 대본 따기 (yt-dlp → Soniox 정밀 받아쓰기 → 화자 라벨) =================
const SONIOX_KEY = String(process.env.SONIOX_API_KEY || "").trim();
const SONIOX_BASE = "https://api.soniox.com/v1";

async function ytDownloadAudio(videoId, destPath) {
  const url = "https://www.youtube.com/watch?v=" + videoId;
  await exec("yt-dlp", [
    "-f", "bestaudio/best",
    "-x", "--audio-format", "opus", "--audio-quality", "128K",
    "--no-playlist", "--no-warnings", "--no-progress", "--socket-timeout", "30",
    "--extractor-args", "youtube:player_client=android,web",
    "-o", destPath,
    url,
  ], { timeout: 300000, maxBuffer: 64 * 1024 * 1024 });
}

async function ytNormalizeAudio(src, dst) {
  // 작은 소리를 또렷하게 + 저역 잡음 제거 → 받아쓰기 정확도 향상
  await exec("ffmpeg", ["-hide_banner","-nostats","-loglevel","error","-i", src, "-ac","1","-af","highpass=f=90,dynaudnorm=f=200:g=8","-c:a","libopus","-b:a","96k", dst, "-y"], { timeout: 300000, maxBuffer: 64 * 1024 * 1024 });
}

async function sonioxUploadFile(path) {
  const buf = await readFile(path);
  const fd = new FormData();
  fd.append("file", new Blob([buf]), "audio.opus");
  const r = await fetch(SONIOX_BASE + "/files", {
    method: "POST",
    headers: { Authorization: "Bearer " + SONIOX_KEY },
    body: fd,
  });
  const j = await r.json().catch(() => null);
  if (!r.ok || !j || !j.id) throw new Error("Soniox 업로드 실패 HTTP " + r.status);
  return j.id;
}

async function sonioxCreate(fileId) {
  const r = await fetch(SONIOX_BASE + "/transcriptions", {
    method: "POST",
    headers: { Authorization: "Bearer " + SONIOX_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "stt-async-v5",
      file_id: fileId,
      language_hints: ["ko"],
      enable_speaker_diarization: true,
    }),
  });
  const j = await r.json().catch(() => null);
  if (!r.ok || !j || !j.id) throw new Error("Soniox 변환생성 실패 HTTP " + r.status);
  return j.id;
}

async function sonioxWait(id) {
  const deadline = Date.now() + 240000;
  while (Date.now() < deadline) {
    await new Promise((res) => setTimeout(res, 3000));
    const r = await fetch(SONIOX_BASE + "/transcriptions/" + id, { headers: { Authorization: "Bearer " + SONIOX_KEY } });
    const j = await r.json().catch(() => null);
    if (!r.ok) throw new Error("Soniox 상태확인 실패 HTTP " + r.status);
    const st = j && j.status;
    if (st === "completed") return;
    if (st === "error") throw new Error("Soniox 처리 오류: " + ((j && j.error_message) || ""));
  }
  throw new Error("Soniox 시간 초과");
}

async function sonioxGetTokens(id) {
  const r = await fetch(SONIOX_BASE + "/transcriptions/" + id + "/transcript", { headers: { Authorization: "Bearer " + SONIOX_KEY } });
  const j = await r.json().catch(() => null);
  if (!r.ok || !j || !Array.isArray(j.tokens)) throw new Error("Soniox 결과 실패 HTTP " + r.status);
  return j.tokens;
}

async function sonioxDeleteFile(fileId) {
  try { await fetch(SONIOX_BASE + "/files/" + fileId, { method: "DELETE", headers: { Authorization: "Bearer " + SONIOX_KEY } }); } catch (e) {}
}

function ytMsToClock(ms) {
  const s = Math.round(ms / 1000);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function sonioxTokensToLines(tokens) {
  const out = [];
  let cur = null;
  for (const t of tokens) {
    const text = String(t.text || "");
    if (!text.trim()) continue;
    const sp = (t.speaker !== undefined && t.speaker !== null && String(t.speaker) !== "") ? String(t.speaker) : "1";
    const startMs = Number(t.start_ms || 0);
    const endMs = Number(t.end_ms || startMs);
    if (!cur || cur.speaker !== sp || (startMs - cur.endMs) > 1200) {
      if (cur) out.push(cur);
      cur = { speaker: sp, startMs: startMs, endMs: endMs, text: text };
    } else {
      cur.text += text;
      cur.endMs = endMs;
    }
  }
  if (cur) out.push(cur);
  return out.map((l) => ({ ms: l.startMs, end_ms: l.endMs, speaker_num: l.speaker, text: l.text.replace(/\s+/g, " ").trim() })).filter((l) => l.text);
}

const SPEAKER_MAP_PROMPT = `아래는 한 영상을 음성인식한 결과에서, 화자(말한 사람)별 대사 예시입니다. 각 화자번호가 '이야기 속 누구'인지 역할 이름을 붙여주세요.

규칙:
- 내용(호칭·맥락·말투)으로 역할을 추정하세요. 예: 해설, 아내, 남편, 시어머니, 처제, 할머니, 아들, 딸.
- 상황을 설명하는 해설(상황 설명) 말투는 "해설".
- 확신이 없으면 "화자1","화자2"처럼 번호 그대로 두세요.

JSON만 출력:
{"map":{"화자번호":"역할이름"},"voice_type":"AI음성|사람나레이션|원본소리 중 하나","note":"특이사항 한 줄(없으면 빈 문자열)"}`;

async function ytLabelSpeakerMap(lines) {
  const bySp = {};
  for (const l of lines) { const k = l.speaker_num || "1"; (bySp[k] = bySp[k] || []).push(l.text); }
  const nums = Object.keys(bySp);
  let sample = "";
  for (const k of nums) sample += "화자" + k + ": " + bySp[k].slice(0, 6).join(" / ") + "\n";
  try {
    const r = await callGemini({
      contents: [{ parts: [{ text: SPEAKER_MAP_PROMPT + "\n\n=== 화자별 대사 ===\n" + sample }] }],
      generationConfig: { responseMimeType: "application/json", temperature: 0, maxOutputTokens: 4096 },
    });
    const out = parseJsonLoose(geminiText(r.gj)) || {};
    return { map: out.map || {}, voice_type: out.voice_type || "", note: out.note || "", singleSpeaker: nums.length <= 1 };
  } catch (e) {
    return { map: {}, voice_type: "", note: "", singleSpeaker: nums.length <= 1 };
  }
}

function ytLettersOnly(s) { return String(s || "").replace(/[^0-9A-Za-z가-힣]/g, ""); }

const POLISH_PROMPT = `아래는 한 영상을 음성인식으로 정확히 받아쓴 대본입니다. 각 줄은 [화자번호 | 시각] 으로 시작합니다. 이 대본을 '보기 좋게' 다듬어 주세요. 단, 단어는 절대 바꾸지 마세요.

규칙:
- 음성인식이 비슷한 소리로 잘못 받아쓴 것이 문맥상 명백한 경우에만, 올바른 한국어 단어로 고치세요. 예: "그러리가"→"그럴리가", "머라고"→"뭐라고".
- 그 외에는 들리는 단어를 그대로 두세요. 원래 없던 내용을 지어내거나, 문장을 요약하거나, 통째로 빼지 마세요.
- 확실하지 않으면 고치지 말고 그대로 두세요.
- 띄어쓰기와 문장부호(마침표·쉼표·물음표)는 한국어 맞춤법에 맞게 자연스럽게 정리하세요.
- 각 줄이 '이야기 속 누구의 말인지' 역할 이름을 speaker에 붙인세요. 예: 해설, 아내, 남편, 시어머니, 처제, 할머니, 아들, 딸.
- 상황을 설명하는 해설(상황 설명) 말투는 "해설".
- 화자번호가 서로 섞여 있을 수 있으니, 번호보다 '내용과 맥락'으로 누구인지 판단하세요.
- 정말 모르겠으면 "화자1","화자2"처럼 두세요.
- 줄의 순서·개수·시각(t)은 그대로 유지하세요.

JSON만 출력:
{"language":"한국어","voice_type":"AI음성|사람나레이션|원본소리 중 하나","speakers":["등장 화자 목록"],"lines":[{"t":"0:03","speaker":"역할 이름","text":"다듬은 문장"}],"note":"특이사항 한 줄(없으면 빈 문자열)"}`;

async function ytPolishTranscript(lines0) {
  const body = lines0.map((l) => "[화자" + (l.speaker_num || "1") + " | " + ytMsToClock(l.ms) + "] " + l.text).join("\n");
  const r = await callGemini({
    contents: [{ parts: [{ text: POLISH_PROMPT + "\n\n=== 받아쓴 대본 ===\n" + body }] }],
    generationConfig: { responseMimeType: "application/json", temperature: 0, maxOutputTokens: 65536 },
  });
  const out = parseJsonLoose(geminiText(r.gj));
  if (!out || !Array.isArray(out.lines) || !out.lines.length) return null;
  // 단어 보존 검증: 글자(공백·문장부호 제외)만 비교 — 요약·누락 방지
  const inL = ytLettersOnly(lines0.map((l) => l.text).join(""));
  const outL = ytLettersOnly(out.lines.map((l) => l.text || "").join(""));
  if (!inL.length) return null;
  const ratio = outL.length / inL.length;
  if (ratio < 0.9 || ratio > 1.12) { console.warn("[대본따기] 다듬기 폐기: 글자수비율 " + ratio.toFixed(3)); return null; }
  const speakers = [];
  const lines = out.lines.map((l, i) => {
    let role = (l.speaker && String(l.speaker).trim()) || ("화자" + ((lines0[i] && lines0[i].speaker_num) || "1"));
    if (!speakers.includes(role)) speakers.push(role);
    const t = (l.t && String(l.t)) || (lines0[i] ? ytMsToClock(lines0[i].ms) : "0:00");
    const ms = lines0[i] ? lines0[i].ms : 0;
    const end_ms = lines0[i] ? (lines0[i].end_ms || lines0[i].ms) : 0;
    return { t: t, ms: ms, end_ms: end_ms, speaker: role, text: String(l.text || "").trim() };
  }).filter((l) => l.text);
  if (!lines.length) return null;
  return {
    language: "한국어",
    voice_type: out.voice_type || "원본소리",
    speakers: speakers,
    lines: lines,
    onscreen: [],
    note: (out.note ? out.note + " · " : "") + "정밀 받아쓰기(Soniox)",
    engine: "soniox:stt-async-v5+polish",
  };
}

async function runYtTranscript(job) {
  if (!SONIOX_KEY) throw new Error("SONIOX_API_KEY 미설정");
  const tmpDir = await mkdtemp(join(tmpdir(), "ib-yt-"));
  const audioPath = join(tmpDir, "a.opus");
  let fileId = null;
  try {
    await ytDownloadAudio(job.video_id, audioPath);
    const normPath = join(tmpDir, "n.opus");
    let upPath = audioPath;
    try { await ytNormalizeAudio(audioPath, normPath); upPath = normPath; } catch (e) { console.warn("[대본따기] 소리 다듬기 실패, 원본 사용: " + ((e && e.message) || e)); }
    fileId = await sonioxUploadFile(upPath);
    const trId = await sonioxCreate(fileId);
    await sonioxWait(trId);
    const tokens = await sonioxGetTokens(trId);
    const lines0 = sonioxTokensToLines(tokens);
    if (!lines0.length) throw new Error("받아쓴 문장이 없습니다(무음/음악만)");
    let out = null;
    try { out = await ytPolishTranscript(lines0); } catch (e) { out = null; }
    if (out) return out;
    const { map, voice_type, note, singleSpeaker } = await ytLabelSpeakerMap(lines0);
    const speakers = [];
    const lines = lines0.map((l) => {
      let role = map[l.speaker_num];
      if (!role) role = singleSpeaker ? "해설" : ("화자" + (l.speaker_num || "1"));
      if (!speakers.includes(role)) speakers.push(role);
      return { t: ytMsToClock(l.ms), ms: l.ms, end_ms: (l.end_ms || l.ms), speaker: role, text: l.text };
    });
    return {
      language: "한국어",
      voice_type: voice_type || "원본소리",
      speakers: speakers,
      lines: lines,
      onscreen: [],
      note: (note ? note + " · " : "") + "정밀 받아쓰기(Soniox)",
      engine: "soniox:stt-async-v5",
    };
  } finally {
    if (fileId) await sonioxDeleteFile(fileId);
    await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }
}

async function claimYtJob() {
  const { data: rows } = await sb.from("yt_transcript_jobs")
    .select("id, video_id, attempts").eq("status", "queued")
    .order("created_at", { ascending: true }).limit(1);
  if (!rows || !rows.length) return null;
  const row = rows[0];
  const { data: upd } = await sb.from("yt_transcript_jobs")
    .update({ status: "running", started_at: new Date().toISOString() })
    .eq("id", row.id).eq("status", "queued").select("id, video_id, attempts").single();
  return upd || null;
}

async function tryYtTranscriptJob() {
  let yj;
  try { yj = await claimYtJob(); } catch (e) { return; }
  if (!yj) return;
  console.log("[대본따기] 시작 job=" + yj.id + " video=" + yj.video_id);
  try {
    const result = await runYtTranscript(yj);
    await sb.from("yt_transcript_jobs").update({ status: "done", result: result, finished_at: new Date().toISOString() }).eq("id", yj.id);
    console.log("[대본따기] 완료 job=" + yj.id + " 줄=" + ((result.lines || []).length));
  } catch (err) {
    console.error("[대본따기] 실패 job=" + yj.id + ":", err.message);
    await sb.from("yt_transcript_jobs").update({ status: "error", error: String(err.message).slice(0, 400), finished_at: new Date().toISOString() }).eq("id", yj.id);
  }
}
// ================= /유튜브 대본 따기 =================


async function loop() {
  lastPollAt = new Date().toISOString();
  try {
    if (!flagsEnsured) { await ensureFlags(); flagsEnsured = true; }
    if (Date.now() - lastCleanup > 300000) { lastCleanup = Date.now(); cleanupExpired(); recoverStuck(); cleanupWmExpired(); }
    await scanWmQueued();
    const job = await claimJob();
    if (!job) { await tryYtTranscriptJob(); }
    if (job) {
      console.log(`[작업] ${job.job_type} 시작 (job=${job.id}, 시도=${job.attempts + 1})`);
      try {
        if (job.job_type === "probe") await runProbe(job);
        else if (job.job_type === "transcribe") await runTranscribe(job);
        else if (job.job_type === "analyze") await runAnalyze(job);
        else if (job.job_type === "render") await runRender(job);
        else if (job.job_type === "desilence") await runDesilence(job);
        else if (job.job_type === "wmremove") {
          if (process.env.RUNPOD_API_KEY && process.env.RUNPOD_ENDPOINT_ID) {
            try { await runWmRemoveGpu(job); }
            catch (e) {
              if (e && e.noFallback) throw e;
              console.error("[wm-gpu] GPU 서버 실패, 예비(Replicate)로 전환:", e.message);
              if (process.env.REPLICATE_API_TOKEN) await runWmRemove(job); else throw e;
            }
          } else await runWmRemove(job);
        }
        else throw new Error("아직 지원하지 않는 작업 유형: " + job.job_type);
        await finishJob(job.id, true);
        processedCount++;
      } catch (err) {
        console.error(`[작업] 실패 (job=${job.id}):`, err.message);
        // noFallback = 이미 안내 문구(failed_wm)까지 남긴 확정 실패: 재시도·문구 덮어쓰기 모두 하지 않음
        if ((job.attempts || 0) < 1 && !err.noFallback) {
          // 1회 자동 재시도: 다시 대기열로 (사용량 중복 기록 없음)
          await sb.from("sc_render_jobs").update({ status: "queued", error: "재시도 예정: " + err.message.slice(0, 300) }).eq("id", job.id);
        } else {
          await finishJob(job.id, false, err.message);
          const failStatus = job.job_type === "transcribe" ? "failed_transcribe"
            : job.job_type === "analyze" ? "failed_analyze"
            : job.job_type === "render" ? "failed_render"
            : job.job_type === "desilence" ? "failed_desilence"
            : job.job_type === "wmremove" ? "failed_wm" : "failed_probe";
          if (!err.noFallback) await setProjectStatus(job.project_id, failStatus, err.message.slice(0, 200));
        }
      }
    }
  } catch (e) {
    console.error("[루프] 오류:", e.message);
  }
  setTimeout(loop, POLL_MS);
}
loop();
