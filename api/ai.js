// ============================================
// 인비랩 AI 중계 서버 (Step 0 + Step 1)
// - Gemini API 키는 서버 환경변수(GEMINI_API_KEY)에만 보관 (사이트에 노출 안 됨)
// - 순서: 로그인 확인 → 관리자 여부 → (비관리자) 공개 여부 + 하루 한도 → AI 호출 → 사용 기록
// - 액션: ping(연결 테스트), analyze(영상 분석)
// ============================================
const SUPABASE_URL = "https://bdbwawskwdgdsqmoqodt.supabase.co";
const ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJkYndhd3Nrd2RnZHNxbW9xb2R0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODUwNzcxNzcsImV4cCI6MjEwMDY1MzE3N30.sOJU4L75T7MDaeZFIXmzEvWN_ZW4eiZKpX9cWOzqwF4";
const GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/";

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

// Gemini 응답에서 텍스트 꺼내기
function geminiText(gj) {
  try {
    return gj.candidates[0].content.parts.map(function (p) { return p.text || ""; }).join("");
  } catch {
    return "";
  }
}

// 텍스트에서 JSON 안전하게 파싱
function parseJsonLoose(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch {}
  const m = text.match(/\{[\s\S]*\}/);
  if (m) { try { return JSON.parse(m[0]); } catch {} }
  return null;
}

// 유튜브 주소에서 영상 ID 추출
function extractVideoId(url) {
  const m = String(url || "").match(
    /(?:youtube\.com\/(?:watch\?[^#]*v=|shorts\/|embed\/|live\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/
  );
  return m ? m[1] : null;
}

const ANALYZE_PROMPT = `당신은 유튜브 쇼츠 제작 전문 분석가입니다. 이 영상을 직접 보고 아래 JSON 형식으로만 답하세요. 다른 텍스트 없이 JSON만 출력하세요. 모든 값은 한국어로 작성하세요.

{
  "category": "카테고리 한 단어~두 단어 (예: 게임, 스포츠, 동물, 영화요약, 음식, 교육, 이슈정리, 애니메이션 등)",
  "topic": "이 영상의 주제 한 줄",
  "summary": "영상 내용 요약 2~3문장",
  "keywords": ["핵심 키워드 5~10개 (검색에 쓸 수 있는 단어들)"],
  "hook": "첫 1~3초가 시청자를 붙잡는 방식 한 줄 (화면과 소리 기준)",
  "format": "콘텐츠 형식 (예: 랭킹, TTS 정보전달, 하이라이트 편집, 밈 편집, 스토리텔링, 비교, 실험 등)",
  "voice_type": "음성 종류: AI음성 | 원본소리 | 음악만 | 무음 | 사람나레이션",
  "creator_face": "제작자 본인 얼굴 노출: 없음 | 잠깐 | 계속",
  "faceless_grade": "무출연 재현 가능성: 가능 | 부분가능 | 어려움  (입문자가 자기 얼굴·목소리 없이 비슷한 포맷을 만들 수 있는가)",
  "faceless_reason": "그렇게 판단한 이유 한 줄",
  "needed_sources": ["이 포맷을 재현할 때 필요한 소스 종류 (예: 무료 스톡영상, 게임 플레이 화면, AI 이미지, 자료 사진, 경기 영상 등)"],
  "difficulty": "편집 난이도 1~5 사이 숫자 (1=아주 쉬움, 5=전문가급)",
  "tip": "이 포맷을 따라 만들 때 가장 중요한 팁 한 줄 (그대로 베끼지 말고 새 소재로 재구성하는 방향)"
}`;

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

    const key = process.env.GEMINI_API_KEY;

    // ---------- 액션: 연결 테스트 ----------
    if (action === "ping") {
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 아직 설정되지 않았습니다 (Vercel 환경변수 필요)" });
        return;
      }
      const g = await fetch(GEMINI_BASE + "gemini-flash-latest:generateContent?key=" + key, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts: [{ text: "테스트입니다. '연결 성공' 네 글자만 답하세요." }] }],
        }),
      });
      const gj = await g.json().catch(() => null);
      if (!g.ok) {
        const detail = gj && gj.error && gj.error.message ? gj.error.message : "HTTP " + g.status;
        res.status(502).json({ error: "Gemini 호출 실패", detail: detail });
        return;
      }
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, admin: isAdmin, answer: geminiText(gj).trim() });
      return;
    }

    // ---------- 액션: 영상 분석 ----------
    if (action === "analyze") {
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정되지 않았습니다" });
        return;
      }
      const vid = extractVideoId(body.video_url);
      if (!vid) {
        res.status(400).json({ error: "유튜브 영상 주소가 아닙니다. 예: https://youtube.com/shorts/XXXXXXXXXXX" });
        return;
      }
      const videoUrl = "https://www.youtube.com/watch?v=" + vid;

      const g = await fetch(GEMINI_BASE + "gemini-pro-latest:generateContent?key=" + key, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [
            {
              parts: [
                { file_data: { file_uri: videoUrl } },
                { text: ANALYZE_PROMPT },
              ],
            },
          ],
          generationConfig: {
            responseMimeType: "application/json",
            mediaResolution: "MEDIA_RESOLUTION_LOW",
            temperature: 0.2,
          },
        }),
      });
      const gj = await g.json().catch(() => null);
      if (!g.ok) {
        let detail = gj && gj.error && gj.error.message ? gj.error.message : "HTTP " + g.status;
        if (/not\s*found|unsupported|invalid/i.test(detail)) {
          detail = "영상을 불러올 수 없습니다 (비공개·삭제·연령제한 영상일 수 있음)";
        }
        res.status(502).json({ error: "영상 분석 실패", detail: detail });
        return;
      }
      const analysis = parseJsonLoose(geminiText(gj));
      if (!analysis) {
        res.status(502).json({ error: "분석 결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, video_id: vid, analysis: analysis });
      return;
    }

    res.status(400).json({ error: "알 수 없는 요청입니다: " + (action || "(없음)") });
  } catch (e) {
    res.status(500).json({ error: "서버 오류", detail: String((e && e.message) || e) });
  }
};
