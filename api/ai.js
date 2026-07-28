// ============================================
// 인비랩 AI 중계 서버 (Step 0)
// - Gemini API 키는 서버 환경변수(GEMINI_API_KEY)에만 보관 (사이트에 노출 안 됨)
// - 순서: 로그인 확인 → 관리자 여부 → (비관리자) 공개 여부 + 하루 한도 → AI 호출 → 사용 기록
// ============================================
const SUPABASE_URL = "https://bdbwawskwdgdsqmoqodt.supabase.co";
const ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJkYndhd3Nrd2RnZHNxbW9xb2R0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODUwNzcxNzcsImV4cCI6MjEwMDY1MzE3N30.sOJU4L75T7MDaeZFIXmzEvWN_ZW4eiZKpX9cWOzqwF4";

async function supaGet(path, token) {
  try {
    const r = await fetch(SUPABASE_URL + path, {
      headers: { apikey: ANON_KEY, Authorization: "Bearer " + token },
    });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function recordUsage(userId, feature, token) {
  try {
    await fetch(SUPABASE_URL + "/rest/v1/usage_log", {
      method: "POST",
      headers: {
        apikey: ANON_KEY,
        Authorization: "Bearer " + token,
        "Content-Type": "application/json",
        Prefer: "return=minimal",
      },
      body: JSON.stringify({ user_id: userId, feature: feature }),
    });
  } catch {}
}

// KST(한국시간) 기준 오늘 0시를 UTC ISO로
function kstDayStartIso() {
  const now = new Date();
  const kst = new Date(now.getTime() + 9 * 3600 * 1000);
  kst.setUTCHours(0, 0, 0, 0);
  return new Date(kst.getTime() - 9 * 3600 * 1000).toISOString();
}

module.exports = async (req, res) => {
  try {
    if (req.method !== "POST") {
      res.status(405).json({ error: "POST 요청만 받습니다" });
      return;
    }

    // 1) 로그인 확인
    const token = String(req.headers.authorization || "").replace(/^Bearer\s+/i, "");
    if (!token) {
      res.status(401).json({ error: "로그인이 필요합니다" });
      return;
    }
    const user = await supaGet("/auth/v1/user", token);
    if (!user || !user.id) {
      res.status(401).json({ error: "로그인 정보가 만료되었습니다. 다시 로그인해 주세요." });
      return;
    }

    // 2) 관리자 여부
    const adminRows = await supaGet("/rest/v1/admins?user_id=eq." + user.id + "&select=user_id", token);
    const isAdmin = Array.isArray(adminRows) && adminRows.length > 0;

    const body = req.body || {};
    const feature = String(body.feature || "");
    const action = String(body.action || "");

    // 3) 비관리자: 공개된 기능만 + 하루 한도 확인
    if (!isAdmin) {
      const flags = await supaGet(
        "/rest/v1/feature_flags?key=eq." + encodeURIComponent(feature) + "&select=is_public",
        token
      );
      if (!Array.isArray(flags) || !flags.length || !flags[0].is_public) {
        res.status(403).json({ error: "아직 공개되지 않은 기능입니다" });
        return;
      }
      const limRows = await supaGet(
        "/rest/v1/usage_limits?feature=eq." + encodeURIComponent(feature) + "&select=daily_limit",
        token
      );
      const limit = Array.isArray(limRows) && limRows.length ? Number(limRows[0].daily_limit) : 0;
      const used = await supaGet(
        "/rest/v1/usage_log?user_id=eq." + user.id +
          "&feature=eq." + encodeURIComponent(feature) +
          "&used_at=gte." + encodeURIComponent(kstDayStartIso()) + "&select=id",
        token
      );
      const usedCount = Array.isArray(used) ? used.length : 0;
      if (usedCount >= limit) {
        res.status(429).json({ error: "오늘 이용 한도(" + limit + "회)를 모두 사용했습니다. 내일 다시 이용해 주세요." });
        return;
      }
    }

    // 4) 요청 처리
    if (action === "ping") {
      // 서버-Gemini 연결 테스트 (아주 작은 호출)
      const key = process.env.GEMINI_API_KEY;
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 아직 설정되지 않았습니다 (Vercel 환경변수 필요)" });
        return;
      }
      const g = await fetch(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key=" + key,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: [{ parts: [{ text: "테스트입니다. '연결 성공' 네 글자만 답하세요." }] }],
          }),
        }
      );
      const gj = await g.json().catch(() => null);
      if (!g.ok) {
        const detail = gj && gj.error && gj.error.message ? gj.error.message : "HTTP " + g.status;
        res.status(502).json({ error: "Gemini 호출 실패", detail: detail });
        return;
      }
      let text = "";
      try { text = gj.candidates[0].content.parts[0].text || ""; } catch {}
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, admin: isAdmin, answer: text.trim() });
      return;
    }

    res.status(400).json({ error: "알 수 없는 요청입니다: " + (action || "(없음)") });
  } catch (e) {
    res.status(500).json({ error: "서버 오류", detail: String((e && e.message) || e) });
  }
};
