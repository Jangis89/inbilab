// 수집 결과 카카오톡 알림 (나에게 보내기)
// - GitHub Secrets: KAKAO_REST_KEY / SUPABASE_URL / SUPABASE_SERVICE_ROLE / JOB_STATUS
// - 리프레시 토큰은 Supabase app_secrets 테이블에 저장되며, 갱신 시 자동으로 다시 저장된다.

const KEY = process.env.KAKAO_REST_KEY;
const SB_URL = process.env.SUPABASE_URL;
const SB_KEY = process.env.SUPABASE_SERVICE_ROLE;
const STATUS = (process.env.JOB_STATUS || "unknown").toLowerCase();

if (!KEY) { console.log("카카오 미설정 - 알림 생략"); process.exit(0); }
if (!SB_URL || !SB_KEY) { console.log("Supabase 미설정 - 알림 생략"); process.exit(0); }

async function sbFetch(path, opt = {}) {
  return fetch(`${SB_URL}/rest/v1/${path}`, {
    ...opt,
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      "Content-Type": "application/json",
      ...(opt.headers || {}),
    },
  });
}

try {
  const rows = await (await sbFetch("app_secrets?key=eq.kakao_refresh_token&select=value")).json();
  const refresh = rows && rows[0] && rows[0].value;
  if (!refresh) { console.log("카카오 리프레시 토큰 없음 - 알림 생략"); process.exit(0); }

  const tok = await (await fetch("https://kauth.kakao.com/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "refresh_token", client_id: KEY, refresh_token: refresh }),
  })).json();

  if (tok.refresh_token) {
    await sbFetch("app_secrets?key=eq.kakao_refresh_token", {
      method: "PATCH",
      headers: { Prefer: "return=minimal" },
      body: JSON.stringify({ value: tok.refresh_token }),
    });
    console.log("리프레시 토큰 자동 갱신 저장 완료");
  }

  if (!tok.access_token) { console.log("토큰 갱신 실패:", tok.error_description || tok.error || "unknown"); process.exit(0); }

  const kst = new Date(Date.now() + 9 * 3600 * 1000);
  const hhmm = String(kst.getUTCHours()).padStart(2, "0") + ":" + String(kst.getUTCMinutes()).padStart(2, "0");
  const md = (kst.getUTCMonth() + 1) + "/" + kst.getUTCDate();
  const ok = STATUS === "success";
  const text = (ok ? "[OK] 인비랩 데이터 수집 성공" : "[!!] 인비랩 데이터 수집 실패")
    + `\n${md} ${hhmm} (한국시간)`
    + (ok ? "\n채널·영상·카테고리 업데이트 완료" : "\n원인 확인 필요 - Claude에게 '수집 로그 확인해서 고쳐줘'라고 말하세요.");

  const res = await fetch("https://kapi.kakao.com/v2/api/talk/memo/default/send", {
    method: "POST",
    headers: { Authorization: `Bearer ${tok.access_token}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      template_object: JSON.stringify({
        object_type: "text",
        text,
        link: { web_url: "https://github.com/Jangis89/inbilab/actions", mobile_web_url: "https://github.com/Jangis89/inbilab/actions" },
        button_title: "실행 기록 보기",
      }),
    }),
  });
  console.log("카카오 전송 결과:", res.status);
} catch (e) {
  console.log("알림 처리 오류(수집에는 영향 없음):", String(e).slice(0, 200));
  process.exit(0);
}
