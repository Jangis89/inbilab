// ============================================
// 인비랩 AI 채널 진단 (Vercel 서버 함수)
// - 로그인한 회원만 사용 가능, 월 10회 한도
// - Gemini API 키는 서버 환경변수에만 보관 (외부 노출 없음)
// ============================================

const SUPABASE_URL = "https://bdbwawskwdgdsqmoqodt.supabase.co";
const ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJkYndhd3Nrd2RnZHNxbW9xb2R0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODUwNzcxNzcsImV4cCI6MjEwMDY1MzE3N30.sOJU4L75T7MDaeZFIXmzEvWN_ZW4eiZKpX9cWOzqwF4";
const MONTHLY_LIMIT = 10;

export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "authorization, content-type");
  if (req.method === "OPTIONS") return res.status(200).end();
  if (req.method !== "POST") return res.status(405).json({ error: "POST만 지원합니다." });

  try {
    if (!process.env.GEMINI_API_KEY) {
      return res.status(500).json({ error: "AI 키가 아직 설정되지 않았습니다. (관리자: Vercel 환경변수 GEMINI_API_KEY)" });
    }
    // 1. 로그인 확인
    const token = (req.headers.authorization || "").replace("Bearer ", "");
    if (!token) return res.status(401).json({ error: "로그인이 필요합니다." });
    const uRes = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
      headers: { apikey: ANON_KEY, Authorization: `Bearer ${token}` },
    });
    if (!uRes.ok) return res.status(401).json({ error: "로그인이 만료되었습니다. 다시 로그인해 주세요." });
    const user = await uRes.json();

    // 2. 이번 달 사용 횟수 확인
    const now = new Date();
    const monthStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString();
    const cRes = await fetch(
      `${SUPABASE_URL}/rest/v1/ai_reports?select=id&created_at=gte.${monthStart}`,
      { headers: { apikey: ANON_KEY, Authorization: `Bearer ${token}`, Prefer: "count=exact", Range: "0-0" } }
    );
    const used = Number((cRes.headers.get("content-range") || "/0").split("/")[1] || 0);
    if (used >= MONTHLY_LIMIT) {
      return res.status(429).json({ error: `이번 달 AI 진단 한도(${MONTHLY_LIMIT}회)를 모두 사용했습니다. 다음 달에 다시 이용해 주세요.` });
    }

    // 3. 채널 데이터 확인
    const { channel, snapshots, videos } = req.body || {};
    if (!channel || !channel.id) return res.status(400).json({ error: "채널 정보가 없습니다." });

    // 4. Gemini 호출
    const dataText = JSON.stringify({ channel, snapshots, videos }).slice(0, 14000);
    const prompt =
      "너는 한국의 유튜브 채널 성장 컨설턴트다. 아래 채널 데이터를 분석해서, " +
      "유튜브를 처음 하는 40~60대도 이해할 수 있는 아주 쉬운 한국어로 진단 리포트를 써라. " +
      "형식: 마크다운 기호(#, *, - 등) 없이, 아래 4개 섹션 제목과 줄바꿈만 사용. " +
      "[1] 현재 상태 진단 (3~4문장) " +
      "[2] 이 채널의 강점 (2~3가지) " +
      "[3] 아쉬운 점 (2~3가지) " +
      "[4] 지금 당장 할 일 3가지 (구체적으로) " +
      "숫자는 '만', '억' 단위로 읽기 쉽게. 데이터: " + dataText;

    const gRes = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${process.env.GEMINI_API_KEY}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }),
      }
    );
    const g = await gRes.json();
    if (!gRes.ok) {
      return res.status(500).json({ error: "AI 호출 실패: " + (g.error && g.error.message ? g.error.message : gRes.status) });
    }
    const text =
      (g.candidates && g.candidates[0] && g.candidates[0].content &&
       g.candidates[0].content.parts && g.candidates[0].content.parts[0] &&
       g.candidates[0].content.parts[0].text) || "리포트를 생성하지 못했습니다. 다시 시도해 주세요.";

    // 5. 기록 저장
    await fetch(`${SUPABASE_URL}/rest/v1/ai_reports`, {
      method: "POST",
      headers: { apikey: ANON_KEY, Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: user.id, channel_id: channel.id, report: text }),
    });

    return res.status(200).json({ report: text, used: used + 1, limit: MONTHLY_LIMIT });
  } catch (e) {
    return res.status(500).json({ error: "서버 오류: " + e.message });
  }
}
