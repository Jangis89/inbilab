// 인비랩 카카오 알림 — 수집 결과를 관리자들의 카카오톡(나와의 채팅)으로 전송
// 슬롯: kakao_refresh_token(대표), kakao_refresh_token_2(동업자)
const REST_KEY = process.env.KAKAO_REST_KEY;
const SB_URL   = process.env.SUPABASE_URL;
const SB_KEY   = process.env.SUPABASE_SERVICE_ROLE;
const STATUS   = process.env.JOB_STATUS || "unknown";

const TOKEN_KEYS = ["kakao_refresh_token", "kakao_refresh_token_2"];

if (!REST_KEY || !SB_URL || !SB_KEY) {
  console.log("카카오 알림 설정 없음 — 건너뜀");
  process.exit(0);
}

async function getSecret(key) {
  const r = await fetch(SB_URL + "/rest/v1/app_secrets?key=eq." + key + "&select=value", {
    headers: { apikey: SB_KEY, Authorization: "Bearer " + SB_KEY }
  });
  if (!r.ok) return null;
  const rows = await r.json();
  return rows && rows[0] ? rows[0].value : null;
}

async function saveSecret(key, value) {
  try {
    await fetch(SB_URL + "/rest/v1/app_secrets?on_conflict=key", {
      method: "POST",
      headers: {
        apikey: SB_KEY,
        Authorization: "Bearer " + SB_KEY,
        "Content-Type": "application/json",
        Prefer: "resolution=merge-duplicates"
      },
      body: JSON.stringify({ key: key, value: value })
    });
  } catch (e) { console.log(key + " 토큰 저장 실패: " + String(e).slice(0, 80)); }
}

function buildMessage() {
  const ok = STATUS === "success";
  const kst = new Date(Date.now() + 9 * 3600 * 1000);
  const stamp = kst.toISOString().slice(0, 16).replace("T", " ");
  let text = (ok ? "[OK] 인비랩 데이터 수집 성공" : "[!!] 인비랩 데이터 수집 실패") + " (" + stamp + " KST)";
  if (!ok) text += "\n깃허브 액션 로그를 확인해 주세요.";
  return text;
}

async function sendFor(key) {
  const rt = await getSecret(key);
  if (!rt) { console.log(key + ": 등록 안 됨 — 건너뜀"); return; }
  const tr = await fetch("https://kauth.kakao.com/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      client_id: REST_KEY,
      refresh_token: rt
    })
  });
  const tj = await tr.json();
  if (!tj.access_token) { console.log(key + ": 토큰 갱신 실패 " + tr.status); return; }
  if (tj.refresh_token) await saveSecret(key, tj.refresh_token);
  const template = {
    object_type: "text",
    text: buildMessage(),
    link: { web_url: "https://inbilab.ai.kr", mobile_web_url: "https://inbilab.ai.kr" }
  };
  const mr = await fetch("https://kapi.kakao.com/v2/api/talk/memo/default/send", {
    method: "POST",
    headers: {
      Authorization: "Bearer " + tj.access_token,
      "Content-Type": "application/x-www-form-urlencoded"
    },
    body: new URLSearchParams({ template_object: JSON.stringify(template) })
  });
  console.log(key + " 카카오 전송 결과: " + mr.status);
}

for (const k of TOKEN_KEYS) {
  try { await sendFor(k); } catch (e) { console.log(k + " 오류: " + String(e).slice(0, 100)); }
}
