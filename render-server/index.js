// ============================================
// 인비랩 숏츠 제작기 — 렌더 서버 v0.1 (A-1 뼈대)
// 역할: sc_render_jobs 대기열을 감시해 작업을 하나씩 처리한다.
// 이 버전에서 지원하는 작업: probe(파일 검사)
// 다음 버전에서 추가: transcribe(전사), analyze(후보), render(컷·자막)
// 원칙: 원본 무수정 / 멱등성 / 실패 시 1회 자동 재시도 / 전부 기록
// ============================================
import { createClient } from "@supabase/supabase-js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import http from "node:http";

const exec = promisify(execFile);

const SUPABASE_URL = process.env.SUPABASE_URL;
const SERVICE_KEY = process.env.SUPABASE_SERVICE_ROLE;
if (!SUPABASE_URL || !SERVICE_KEY) {
  console.error("환경변수(SUPABASE_URL, SUPABASE_SERVICE_ROLE)가 없습니다.");
  process.exit(1);
}
const sb = createClient(SUPABASE_URL, SERVICE_KEY, { auth: { persistSession: false } });

const WORKER_ID = "worker-" + Math.random().toString(36).slice(2, 8);
const POLL_MS = 5000;

// ---------- 상태 확인용 웹 응답 (Railway 헬스체크) ----------
const PORT = process.env.PORT || 8080;
let lastPollAt = null;
let processedCount = 0;
http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({
    ok: true, service: "inbilab-render-server", worker: WORKER_ID,
    lastPollAt, processedCount, ffmpeg: FFMPEG_OK,
  }));
}).listen(PORT, () => console.log(`[서버] 상태 페이지 :${PORT}`));

// ---------- ffmpeg 존재 확인 ----------
let FFMPEG_OK = false;
try {
  const { stdout } = await exec("ffmpeg", ["-version"]);
  FFMPEG_OK = true;
  console.log("[준비] ffmpeg OK:", stdout.split("\n")[0]);
} catch {
  console.error("[준비] ffmpeg를 찾을 수 없음 — nixpacks.toml 확인 필요");
}

// ---------- 대기열 처리 루프 ----------
async function claimJob() {
  // queued 상태의 가장 오래된 작업 1개를 원자적으로 가져온다 (경쟁 방지: status 조건부 업데이트)
  const { data: jobs, error } = await sb
    .from("sc_render_jobs")
    .select("id, project_id, recipe_id, job_type, attempts")
    .eq("status", "queued")
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

async function setProjectStatus(projectId, status, detail = "") {
  await sb.from("sc_projects").update({
    status, status_detail: detail, updated_at: new Date().toISOString(),
  }).eq("id", projectId);
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
          await setProjectStatus(job.project_id, "failed_probe", err.message.slice(0, 200));
        }
      }
    }
  } catch (e) {
    console.error("[루프] 오류:", e.message);
  }
  setTimeout(loop, POLL_MS);
}
loop();
