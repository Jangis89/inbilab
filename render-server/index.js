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

const exec = promisify(execFile);

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
const PROMPT_VER = "a1"; // 후보 발굴 프롬프트 버전 (결과 비교용)

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
  const allowedTypes = GEMINI_KEY ? ["probe", "transcribe", "analyze"] : ["probe"];
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
    .select("id, source_path").eq("id", job.project_id).single();
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
  if (audio) await enqueueJob(proj.id, "transcribe");
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

  // 3) 처리 완료(ACTIVE) 대기 — 최대 2분
  for (let i = 0; i < 24; i++) {
    if (file.state === "ACTIVE") return file;
    await new Promise(r => setTimeout(r, 5000));
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
  const m = text.match(/\{[\s\S]*\}/);
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

function buildAnalyzePrompt(objective, durationSec, lines) {
  const objText = {
    views: "조회수(널리 퍼지는 것)가 목표입니다. 강한 훅, 놀라움, 감정 반응을 최우선으로 평가하세요.",
    sales: "판매·상담 문의로 이어지는 것이 목표입니다. 신뢰(근거·증거)와 문제 해결 약속이 뚜렷한 구간을 최우선으로 평가하세요.",
    edu: "핵심이 잘 전달되는 교육용이 목표입니다. 정보 밀도와 완결성(그 구간만 봐도 이해됨)을 최우선으로 평가하세요.",
  }[objective] || "조회수가 목표입니다.";

  return `당신은 유튜브 숏츠 편집 전문가입니다. 아래는 ${Math.round(durationSec / 60)}분짜리 긴 영상의 전체 대본(문장별 시작~끝 시각 포함)입니다. 이 중에서 "숏츠(세로 짧은 영상)로 잘라내면 터질 수 있는 구간"을 골라내세요.

[목표] ${objText}

[규칙 — 반드시 지키세요]
1. 후보는 6~10개. 각 후보는 20~75초 사이.
2. start_s와 end_s는 반드시 대본에 실제로 있는 문장의 시작·끝 시각을 사용하세요 (문장 중간에서 자르지 말 것).
3. 후보 구간만 따로 봐도 무슨 말인지 이해돼야 합니다 (완결성). 앞뒤 맥락이 꼭 필요한 구간은 뽑지 마세요.
4. 대본에 실제로 있는 내용만 근거로 쓰세요. 창작·과장 금지.
5. title은 그 구간으로 만든 숏츠의 추천 제목(호기심 유발형, 30자 이내).
6. 각 점수는 0~10 정수. scores 항목: hook(첫 3초 훅 강도), standalone(완결성), density(정보 밀도), emotion(감정 반응), novelty(새로움·의외성), proof(근거·증거), duration_fit(길이 적합), context_dep(앞뒤 맥락 의존도 — 낮을수록 좋음), safety(안전성 — 저작권·선정성 문제 없으면 10).
7. risk_flags: 주의할 점 배열 (예: "배경음악 저작권 확인 필요", "인물 얼굴 노출"). 없으면 빈 배열.

JSON만 출력하세요:
{"candidates":[{"start_s":123.4,"end_s":168.0,"title":"...","hook_reason":"이 구간이 터질 수 있는 이유 한 줄","summary":"구간 내용 요약 한 줄","scores":{"hook":8,"standalone":7,"density":6,"emotion":7,"novelty":9,"proof":5,"duration_fit":8,"context_dep":2,"safety":9},"risk_flags":[]}]}

[전체 대본]
${lines}`;
}

async function runAnalyze(job) {
  const { data: proj, error } = await sb.from("sc_projects")
    .select("id, objective, source_duration_sec").eq("id", job.project_id).single();
  if (error || !proj) throw new Error("프로젝트 없음");
  const { data: tr, error: te } = await sb.from("sc_transcripts")
    .select("sentences").eq("project_id", proj.id).order("created_at", { ascending: false }).limit(1).single();
  if (te || !tr || !Array.isArray(tr.sentences) || tr.sentences.length < 10) {
    throw new Error("전사록이 없거나 문장이 너무 적습니다");
  }
  await setProjectStatus(proj.id, "analyzing", "AI가 숏츠감 구간을 찾는 중…");

  const durationSec = Number(proj.source_duration_sec || 0);
  const objective = proj.objective || "views";
  const lines = tr.sentences.map(x =>
    `[${(x.s / 1000).toFixed(1)}~${(x.e / 1000).toFixed(1)}] ${x.text}`).join("\n");

  const r = await callGemini({
    contents: [{ parts: [{ text: buildAnalyzePrompt(objective, durationSec, lines) }] }],
    generationConfig: { responseMimeType: "application/json", temperature: 0.3, maxOutputTokens: 16384 },
  });
  const out = parseJsonLoose(geminiText(r.gj));
  if (!out || !Array.isArray(out.candidates)) throw new Error("후보 발굴 결과 해석 실패");

  // ---- 코드 검증 (AI를 믿지 않고 전부 다시 확인) ----
  const W = OBJECTIVE_WEIGHTS[objective] || OBJECTIVE_WEIGHTS.views;
  const clamp10 = v => Math.max(0, Math.min(10, Math.round(Number(v) || 0)));
  let cands = [];
  for (const c of out.candidates) {
    const s0 = Number(c.start_s), e0 = Number(c.end_s);
    if (!isFinite(s0) || !isFinite(e0)) continue;
    const dur = e0 - s0;
    if (dur < 15 || dur > 90) continue;                       // 길이 규칙 위반 제외
    if (s0 < 0 || (durationSec > 0 && e0 > durationSec + 5)) continue; // 영상 밖 구간 제외
    const sc = c.scores || {};
    const scores = {
      hook: clamp10(sc.hook), standalone: clamp10(sc.standalone), density: clamp10(sc.density),
      emotion: clamp10(sc.emotion), novelty: clamp10(sc.novelty), proof: clamp10(sc.proof),
      duration_fit: clamp10(sc.duration_fit), context_dep: clamp10(sc.context_dep), safety: clamp10(sc.safety),
    };
    const parts = {
      hook: scores.hook, novelty: scores.novelty, emotion: scores.emotion, density: scores.density,
      standalone: scores.standalone, proof: scores.proof, duration_fit: scores.duration_fit,
      context_indep: 10 - scores.context_dep,
    };
    let total = 0;
    for (const k of Object.keys(W)) total += W[k] * (parts[k] ?? 0);
    cands.push({
      start_ms: Math.round(s0 * 1000), end_ms: Math.round(e0 * 1000),
      title: String(c.title || "").slice(0, 80),
      hook_reason: String(c.hook_reason || "").slice(0, 300),
      summary: String(c.summary || "").slice(0, 300),
      scores, total_score: Math.round(total * 100) / 100,
      risk_flags: Array.isArray(c.risk_flags) ? c.risk_flags.slice(0, 5).map(x => String(x).slice(0, 100)) : [],
    });
  }
  // 겹침 제거: 점수 높은 순으로 훑으며 이미 뽑힌 후보와 절반 이상 겹치면 버림
  cands.sort((a, b) => b.total_score - a.total_score);
  const kept = [];
  for (const c of cands) {
    const overlaps = kept.some(k => {
      const ov = Math.min(c.end_ms, k.end_ms) - Math.max(c.start_ms, k.start_ms);
      return ov > 0.5 * Math.min(c.end_ms - c.start_ms, k.end_ms - k.start_ms);
    });
    if (!overlaps) kept.push(c);
    if (kept.length >= 10) break;
  }
  if (kept.length === 0) throw new Error("규칙을 통과한 후보가 없습니다 (영상에 숏츠감 구간이 부족할 수 있음)");

  // 다시 실행해도 겹치지 않게: 이전 후보 삭제 후 저장 (멱등성)
  await sb.from("sc_candidates").delete().eq("project_id", proj.id);
  const rows = kept.map(c => ({
    project_id: proj.id, start_ms: c.start_ms, end_ms: c.end_ms, title: c.title,
    hook_reason: c.hook_reason, summary: c.summary, scores: c.scores,
    total_score: c.total_score, risk_flags: c.risk_flags,
    model_info: { model: r.model, prompt_ver: PROMPT_VER, objective },
  }));
  const { error: ie } = await sb.from("sc_candidates").insert(rows);
  if (ie) throw new Error("후보 저장 실패: " + ie.message);

  await setProjectStatus(proj.id, "candidates_ready", "후보 " + kept.length + "개 발굴 완료");
  await sb.from("sc_usage_log").insert({
    project_id: proj.id, kind: "analyze", duration_sec: durationSec,
    meta: { candidates: kept.length, model: r.model, objective, prompt_ver: PROMPT_VER },
  });
  console.log(`[analyze] 완료 p=${proj.id} 후보=${kept.length} (${kept.map(c => fmtTime(c.start_ms) + "~" + fmtTime(c.end_ms)).join(", ")})`);
}

// ---------- 메인 루프 ----------
console.log(`[시작] 렌더 서버 ${WORKER_ID} — ${POLL_MS / 1000}초 간격 대기열 감시`);
async function loop() {
  lastPollAt = new Date().toISOString();
  try {
    const job = await claimJob();
    if (job) {
      console.log(`[작업] ${job.job_type} 시작 (job=${job.id}, 시도=${job.attempts + 1})`);
      try {
        if (job.job_type === "probe") await runProbe(job);
        else if (job.job_type === "transcribe") await runTranscribe(job);
        else if (job.job_type === "analyze") await runAnalyze(job);
        else throw new Error("아직 지원하지 않는 작업 유형: " + job.job_type);
        await finishJob(job.id, true);
        processedCount++;
      } catch (err) {
        console.error(`[작업] 실패 (job=${job.id}):`, err.message);
        if ((job.attempts || 0) < 1) {
          // 1회 자동 재시도: 다시 대기열로 (사용량 중복 기록 없음)
          await sb.from("sc_render_jobs").update({ status: "queued", error: "재시도 예정: " + err.message.slice(0, 300) }).eq("id", job.id);
        } else {
          await finishJob(job.id, false, err.message);
          const failStatus = job.job_type === "transcribe" ? "failed_transcribe"
            : job.job_type === "analyze" ? "failed_analyze" : "failed_probe";
          await setProjectStatus(job.project_id, failStatus, err.message.slice(0, 200));
        }
      }
    }
  } catch (e) {
    console.error("[루프] 오류:", e.message);
  }
  setTimeout(loop, POLL_MS);
}
loop();
