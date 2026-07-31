// ============================================
// 인비랩 AI 중계 서버
// - Gemini API 키는 서버 환경변수(GEMINI_API_KEY)에만 보관 (사이트에 노출 안 됨)
// - 순서: 로그인 확인 → 관리자 여부 → (비관리자) 공개 여부 + 하루 한도 → AI 호출 → 사용 기록
// - 액션: ping(연결 테스트), analyze(영상 분석), sources(원본 후보), transcript(대본 따기), script(대본 작성), stock(스톡 미리보기), blueprint(제작 설계도), hook(후킹 분석)
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

// Gemini 호출: 앞 모델이 하루 한도(429)에 걸리면 다음 모델로 자동 전환
async function callGemini(models, key, requestBody) {
  let last = null;
  for (let i = 0; i < models.length; i++) {
    const m = models[i];
    let g, gj;
    try {
      g = await fetch(GEMINI_BASE + m + ":generateContent?key=" + key, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      gj = await g.json().catch(() => null);
    } catch (e) {
      last = { ok: false, status: 0, detail: String(e && e.message || e) };
      continue;
    }
    if (g.ok) return { ok: true, gj: gj, model: m };
    const msg = (gj && gj.error && gj.error.message) || "";
    last = { ok: false, status: g.status, detail: msg || ("HTTP " + g.status) };
    // 한도 초과(429)면 다음 모델로 넘어가고, 다른 에러면 중단
    if (!(g.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(msg))) break;
  }
  return last;
}

// 유튜브 데이터 API 호출 (서버 키가 있을 때만)
async function ytApi(path, params) {
  const ytKey = String(process.env.YOUTUBE_API_KEY || "").trim();
  if (!ytKey) return null;
  const p = Object.assign({}, params, { key: ytKey });
  const qs = Object.keys(p).map((k) => k + "=" + encodeURIComponent(p[k])).join("&");
  try {
    const r = await fetch("https://www.googleapis.com/youtube/v3/" + path + "?" + qs);
    const j = await r.json().catch(() => null);
    if (!r.ok) {
      return { __error: (j && j.error && j.error.message) || ("HTTP " + r.status) };
    }
    return j;
  } catch (e) {
    return { __error: String((e && e.message) || e) };
  }
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

const SOURCE_PROMPT = `당신은 영상 원본 추적 전문가입니다. 이 영상을 직접 보고, 이 영상이 어떤 원본 소스를 재활용·편집했는지 추적할 단서를 뽑으세요.

아래 JSON 형식으로만 출력하세요:
{
  "origin_guess": "원본으로 추정되는 것 (예: 'UFC 302 경기 중계', '영화 인터스텔라', '중국 SNS 요리 영상', '커뮤니티 게시글 캡처', '직접 촬영 추정')",
  "origin_type": "방송/경기 | 영화/드라마 | 해외 SNS 영상 | 뉴스 | 게임 화면 | 커뮤니티/캡처 | 직접 촬영 추정 | 스톡/자료 영상 | 기타",
  "confidence": "높음 | 중간 | 낮음",
  "watermark": "영상 속 워터마크·계정명·로고 텍스트 (없으면 빈 문자열)",
  "language": "영상 언어",
  "queries": [ { "q": "실제 검색에 쓸 문장", "lang": "ko|en|zh|ja 등", "type": "watermark|entity|quote|visual|source|full_version", "ko": "한국어 역번역(한국어 검색어면 빈 문자열)", "why": "이 검색어를 고른 근거 한 줄" } ],
  "stock_keywords": ["비슷한 소스를 무료 스톡 사이트에서 찾을 영어 키워드 2~3개"]
}

- queries는 3~5개를 "원본을 가장 잘 찾을 순서"로 정렬하세요. 첫 번째가 대표 검색어입니다.
- 대표 검색어 기준: ① 워터마크·계정명이 보이면 그 텍스트 자체가 최우선 (가장 강력한 단서) ② 고유한 인물·사건·장소·날짜 포함 ③ 제목의 과장·낚시 표현 제거 ④ 너무 일반적인 단어만의 조합 금지 (결과가 넓어짐).
- 언어: 한국어 1개 + 영어 1개 필수. 원산지가 중국 추정이면 간체 중국어 검색어 필수, 고유명사는 중국 통용 표기 사용 (예: 宇树科技). 일본 등 다른 원산지도 같은 원칙.
- type: watermark=워터마크·계정명, entity=인물·사건·장소·날짜, quote=대사·자막 정확 일치, visual=장면 묘사, source=원본·현장영상 표현, full_version=전체본·풀버전 표현.
- 한국어가 아닌 검색어는 ko(한국어 역번역)를 반드시 채우세요.`;

const RANK_PROMPT = `원본 후보 판정 작업입니다.
대상 쇼츠: __TARGET__
추정 원본: __ORIGIN__
후보 목록: __CANDS__

각 후보가 대상 쇼츠의 원본(또는 원본에 가까운 소스)일 가능성을 판정하세요.
규칙: 대상보다 늦게 게시된 후보(days_earlier가 음수)는 원본일 수 없으니 '참고'로. 제목·채널이 추정 원본과 잘 맞고 먼저 게시됐으면 '높음'.
match_type 판정: same_source=동일 원본(같은 영상·공식 업로드), same_event=같은 사건·인물·장소를 다룬 다른 영상, visual_similar=내용은 다르지만 시각적으로 유사, long_full_version=대상을 포함하는 더 긴 전체본(duration_seconds가 대상보다 훨씬 길고 같은 내용이면 이것).

JSON만 출력 (가능성 높은 순, 최대 5개):
{ "ranked": [ { "video_id": "...", "grade": "높음|중간|참고", "match_type": "same_source|same_event|visual_similar|long_full_version", "reason": "한 줄 근거" } ] }`;

const TRANSCRIPT_PROMPT = `당신은 영상 대본 추출 전문가입니다. 이 영상을 직접 보고 들으며 실제 내용을 그대로 추출하세요. 절대 창작하거나 요약하지 말고, 들리는/보이는 그대로 옮기세요.

아래 JSON 형식으로만 출력하세요:
{
  "language": "영상 음성 언어 (예: 한국어)",
  "voice_type": "AI음성 | 사람나레이션 | 원본소리 | 음악만 | 무음",
  "lines": [ { "t": "0:03", "text": "나레이션·대사 한 문장 (들리는 그대로, 시간 순서대로)" } ],
  "onscreen": ["화면에 표시되는 자막·텍스트 중 음성과 다른 것만 순서대로 (없으면 빈 배열)"],
  "note": "참고사항 한 줄 (예: 음성 없음 — 화면 자막만 추출)"
}

- 나레이션이 없고 화면 자막만 있는 영상이면 lines에 화면 자막을 시간 순서대로 담고 note에 그 사실을 적으세요.
- t는 그 문장이 시작되는 대략적인 시각(분:초)입니다.`;

const SCRIPT_PROMPT = `당신은 유튜브 쇼츠 전문 작가입니다. 아래는 성공한 쇼츠 영상의 분석 결과입니다:
__ANALYSIS__

__TOPIC__

이 성공 포맷(훅 방식·구성·전개 원리)을 따르되, 원본 영상을 베끼지 말고 완전히 새로운 소재로 쇼츠 대본을 쓰세요.

규칙:
- 문장은 짧고 명확하게 (TTS 프로그램에 바로 붙여넣기 좋게)
- 전체 낭독 시간 30~60초 분량
- 첫 문장은 2초 안에 궁금증을 만드는 강한 훅
- 마지막은 다음 영상 시청이나 구독을 자연스럽게 유도
- 초보자가 얼굴·목소리 노출 없이 만들 수 있는 구성
- 모든 값은 한국어

아래 JSON 형식으로만 출력하세요:
{
  "topic": "선택한 새 소재 한 줄",
  "title": "영상 제목 추천 (호기심 유발형)",
  "target_length_sec": 45,
  "lines": [
    { "text": "낭독할 문장 (첫 항목이 훅)", "scene": "이 문장에 얹을 화면 설명 (어떤 영상/이미지를 보여줄지)", "kw_en": "이 화면 재료를 무료 스톡 사이트에서 찾을 영어 검색어 (스톡으로 구할 수 있는 화면일 때만, 아니면 빈 문자열)" }
  ],
  "outro": "마무리 문장",
  "hashtags": ["#해시태그 3~5개"],
  "notes": "제작 팁 1~2문장"
}`;

const BLUEPRINT_PROMPT = `당신은 유튜브 쇼츠 제작 설계 전문가입니다. 이 영상을 직접 보고, 초보자가 "같은 방식의 영상"을 새 소재로 만들 수 있도록 장면(컷) 단위로 분해한 제작 설계도를 만드세요.

규칙:
- 비슷한 연속 컷은 하나의 장면으로 묶어서 장면은 최대 12개.
- "베끼기"가 아니라 "같은 방식으로 새로 만들기"가 목적. 확보 방법도 그 관점으로 쓰세요.
- 재료 확보 방법은 40~60대 컴퓨터 초보도 따라할 수 있게 구체적으로 한 줄.
- 시간·비용 같은 숫자 예측은 하지 말고 난이도 등급만 사용하세요.
- 모든 값은 한국어 (kw_en과 ai_prompt만 영어).

JSON만 출력하세요:
{
  "scenes": [
    {
      "t0": "0:00", "t1": "0:03",
      "type": "영상클립|정지이미지|화면캡처|텍스트카드|AI생성|게임화면|직접촬영|기타",
      "visual": "화면에 보이는 것 한 줄",
      "how": "이 재료를 확보하는 방법 한 줄 (예: 네이버뉴스에서 해당 기사 검색 후 캡처)",
      "alt": "더 쉬운 대체 방법 한 줄 (없으면 빈 문자열)",
      "difficulty": "쉬움|보통|어려움",
      "kw_ko": "이 재료를 찾을 한국어 검색어 (검색이 필요 없는 장면이면 빈 문자열)",
      "kw_en": "무료 스톡 사이트용 영어 검색어 (스톡에서 구할 수 있는 장면일 때만, 아니면 빈 문자열)",
      "ai_prompt": "AI 이미지/영상 생성이 적합한 장면이면 바로 붙여넣을 영어 프롬프트 (vertical 9:16 포함), 아니면 빈 문자열"
    }
  ],
  "summary": {
    "recipe": "조립 공식 (예: 화면캡처 40% + 스톡클립 30% + 텍스트카드 30%)",
    "source_types": ["사용된 재료 종류 목록"],
    "cut_count": 9,
    "effects": ["사용된 편집 효과 (예: 줌인, 화면전환, 상단 고정 타이틀)"],
    "bottleneck": "가장 구하기 어려운 재료 하나와 이유 한 줄",
    "difficulty": "쉬움|보통|어려움",
    "go": "GO|주의|비추천",
    "verdict": "무출연 초보자가 이 방식으로 만들 수 있는지 한 줄 판단"
  }
}
cut_count에는 실제 컷 수(숫자)를 넣으세요.`;

const HOOK_PROMPT = `당신은 유튜브 쇼츠 후킹(시청자 붙잡기) 분석 전문가입니다. 이 영상을 직접 보고, 첫 3~5초를 집중 분석하세요.

[후킹 전략 분류표 — 10유형]
1 호기심 갭: 답을 숨겨서 궁금하게 만듦
2 결과 먼저: 가장 놀라운 결과/완성본을 첫 컷에 보여줌
3 충격 비주얼: 이상하거나 놀라운 장면으로 시선 강탈
4 질문 던지기: 시청자에게 직접 질문
5 공감 저격: "이런 적 있으시죠?" 내 얘기처럼 느끼게
6 손실 회피: "모르면 손해" 경고형
7 숫자·랭킹: 순위나 구체적 숫자 제시
8 반전 예고: 뒤에 반전이 있음을 예고
9 권위·증거: 전문가·실험·데이터를 먼저 제시
10 패턴 파괴: 예상 밖의 화면·소리·연출로 주의 환기

[매우 중요한 규칙]
- 첫 3~5초에 실제로 나온 대사·자막·화면을 사실 그대로 먼저 기록하세요. 창작·요약 금지.
- 전략 판정: 10유형 중 확신이 있는 것만 고르세요. 딱 맞는 유형이 없으면 절대 억지로 고르지 말고, type_id를 0으로 하고 당신이 관찰한 전략에 새 이름을 붙여 설명하세요 (is_new: true).
- 전략이 여러 개 섞였으면 주 전략 1개 + 보조 전략 최대 2개로 나누세요.
- confidence는 판정 확신도입니다: 높음 | 중간 | 낮음. 애매하면 솔직하게 낮음으로.
- variations(응용 3버전)는 반드시 이 영상과 똑같은 소재·주제를 유지하세요. 소재를 바꾸지 말고 첫 3초를 여는 방식만 바꿉니다. ① 같은 전략을 더 강하게 ② 다른 전략으로 ③ 또 다른 전략으로.
- 각 버전은 자막(caption)과 나레이션(narration)을 구분해서 만드세요:
  · caption = 화면 맨 위에 박는 두 줄 자막. 시인성이 생명입니다. 각 줄은 띄어쓰기 포함 7~12자, 두 줄 합쳐 15~22자 내외(절대 24자 초과 금지). 짧고 강한 구어체로. 좋은 예: "도로 깔면 뭐해?" / "K9 과녁판인데?" (9자+9자)
  · narration = 첫 3초에 실제로 말하는 문장. 자연스러운 완성형 문장으로, 길이 제한 없음.
- 모든 값은 한국어.

JSON만 출력하세요:
{
  "facts": {
    "duration_sec": 4,
    "lines": ["첫 3~5초의 실제 대사·나레이션 그대로 (없으면 빈 배열)"],
    "onscreen": ["첫 3~5초 화면 자막·텍스트 그대로 (없으면 빈 배열)"],
    "visual": "첫 3~5초 화면에 보이는 것 묘사 2~3문장 (사실만)",
    "sound": "소리·음악의 특징 한 줄"
  },
  "main": { "type_id": 2, "name": "결과 먼저", "confidence": "높음", "is_new": false },
  "sub": [ { "type_id": 3, "name": "충격 비주얼" } ],
  "psychology": "이 후킹이 시청자 머릿속에서 일으키는 일 2~3문장 (심리학 용어 대신 쉬운 말로)",
  "intent": "제작자가 노린 것 한 줄",
  "retention": ["3초 이후에도 시청자를 붙잡아두는 장치들 (자막 리듬, 컷 속도, 전개 방식 등)"],
  "variations": [
    { "kind": "같은 전략 · 더 강한 버전", "strategy": "전략 이름", "caption": ["윗줄 자막 (띄어쓰기 포함 7~12자)", "아랫줄 자막 (7~12자)"], "narration": "첫 3초 나레이션 문장", "first_scene": "첫 화면 설명 한 줄" },
    { "kind": "다른 전략으로 열기 1", "strategy": "다른 전략 이름", "caption": ["...", "..."], "narration": "...", "first_scene": "..." },
    { "kind": "다른 전략으로 열기 2", "strategy": "또 다른 전략 이름", "caption": ["...", "..."], "narration": "...", "first_scene": "..." }
  ]
}
duration_sec에는 분석한 훅 구간 길이(숫자, 3~5)를 넣으세요.`;

const HOOK_MORE_PROMPT = `당신은 유튜브 쇼츠 후킹 카피라이터입니다. 아래는 어떤 쇼츠의 첫 3~5초 분석 결과입니다:
__CONTEXT__

이 영상과 똑같은 소재·주제를 유지하면서, 아래 요청된 전략들 각각으로 첫 3초를 여는 버전을 만드세요. 소재를 바꾸지 말고 여는 방식만 바꿉니다.
요청 전략: __STRATS__

[후킹 전략 분류표]
1 호기심 갭 / 2 결과 먼저 / 3 충격 비주얼 / 4 질문 던지기 / 5 공감 저격 / 6 손실 회피 / 7 숫자·랭킹 / 8 반전 예고 / 9 권위·증거 / 10 패턴 파괴

각 버전 규칙:
- caption: 화면 맨 위에 박는 두 줄 자막. 시인성이 생명. 각 줄 띄어쓰기 포함 7~12자, 두 줄 합쳐 15~22자 내외(절대 24자 초과 금지). 짧고 강한 구어체.
- narration: 첫 3초에 실제로 말하는 자연스러운 완성 문장 (길이 제한 없음).
- first_scene: 첫 화면 설명 한 줄.
- 모든 값은 한국어.

JSON만 출력하세요:
{ "variations": [ { "type_id": 7, "strategy": "숫자·랭킹", "caption": ["윗줄", "아랫줄"], "narration": "...", "first_scene": "..." } ] }`;

// ===== [안전장치] 바이두 예산 상한 + 긴급 중단 (설정은 Supabase app_settings에서 즉시 변경 가능) =====
let __bgCache = { t: 0, s: null };
async function baiduGetSettings() {
  const now = Date.now();
  if (__bgCache.s && now - __bgCache.t < 60000) return __bgCache.s;
  const s = { enabled: true, stop: false, globalLimit: 2000, userLimit: 40 };
  try {
    const r = await fetch(SUPABASE_URL + "/rest/v1/app_settings?select=key,value", {
      headers: { apikey: ANON_KEY, Authorization: "Bearer " + ANON_KEY },
    });
    if (r.ok) {
      const rows = await r.json();
      for (const row of rows) {
        if (row.key === "BAIDU_API_ENABLED") s.enabled = String(row.value) === "true";
        if (row.key === "BAIDU_EMERGENCY_STOP") s.stop = String(row.value) === "true";
        if (row.key === "BAIDU_DAILY_GLOBAL_CALL_LIMIT") s.globalLimit = Number(row.value) || 2000;
        if (row.key === "BAIDU_PER_USER_DAILY_CALL_LIMIT") s.userLimit = Number(row.value) || 40;
      }
      __bgCache = { t: now, s: s };
    }
  } catch {}
  return s;
}
async function logBaidu(userId, token, videoId, fields) {
  try {
    await fetch(SUPABASE_URL + "/rest/v1/baidu_call_log", {
      method: "POST",
      headers: { apikey: ANON_KEY, Authorization: "Bearer " + token, "Content-Type": "application/json", Prefer: "return=minimal" },
      body: JSON.stringify(Object.assign({ user_id: userId, video_id: videoId || null }, fields || {})),
    });
  } catch {}
}

async function baiduGuard(userId, token, isAdmin) {
  const s = await baiduGetSettings();
  if (s.stop) return { allow: false, reason: "emergency_stop" };
  if (!s.enabled && !isAdmin) return { allow: false, reason: "baidu_disabled" };
  // 관리자는 횟수 상한 없이 테스트 가능 (사용량 기록은 남김)
  const gLimit = isAdmin ? 99999999 : s.globalLimit;
  const uLimit = isAdmin ? 99999999 : s.userLimit;
  try {
    const r = await fetch(SUPABASE_URL + "/rest/v1/rpc/baidu_check_and_inc", {
      method: "POST",
      headers: { apikey: ANON_KEY, Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ p_user: userId, p_global_limit: gLimit, p_user_limit: uLimit }),
    });
    if (!r.ok) return { allow: true, reason: "counter_unavailable" };
    const j = await r.json().catch(() => null);
    if (j && j.allow === false) return { allow: false, reason: j.reason || "limit" };
    return { allow: true };
  } catch {
    return { allow: true, reason: "counter_error" };
  }
}

// ===== [안전장치2] 1초1회 속도제한 + 중복요청 방지 =====
function ibSleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
async function sbGetCacheSrv(key, token) {
  try {
    const r = await fetch(SUPABASE_URL + "/rest/v1/search_cache?cache_key=eq." + encodeURIComponent(key) + "&select=payload,created_at", {
      headers: { apikey: ANON_KEY, Authorization: "Bearer " + token },
    });
    if (!r.ok) return null;
    const rows = await r.json();
    if (!rows || !rows[0]) return null;
    if (Date.now() - new Date(rows[0].created_at).getTime() > 30 * 24 * 3600 * 1000) return null;
    return rows[0].payload || null;
  } catch { return null; }
}
async function sbPutCacheSrv(key, videoId, payload, token) {
  try {
    await fetch(SUPABASE_URL + "/rest/v1/search_cache?on_conflict=cache_key", {
      method: "POST",
      headers: { apikey: ANON_KEY, Authorization: "Bearer " + token, "Content-Type": "application/json", Prefer: "resolution=merge-duplicates,return=minimal" },
      body: JSON.stringify({ cache_key: key, engine: "baidu", video_id: videoId || null, payload: payload, created_at: new Date().toISOString() }),
    });
  } catch {}
}
async function baiduLockAcquire(key, token) {
  try {
    const r = await fetch(SUPABASE_URL + "/rest/v1/baidu_inflight?on_conflict=key", {
      method: "POST",
      headers: { apikey: ANON_KEY, Authorization: "Bearer " + token, "Content-Type": "application/json", Prefer: "resolution=ignore-duplicates,return=representation" },
      body: JSON.stringify({ key: key, started: new Date().toISOString() }),
    });
    if (!r.ok) return true;
    const rows = await r.json().catch(function () { return []; });
    if (rows && rows.length) return true;
    const g = await fetch(SUPABASE_URL + "/rest/v1/baidu_inflight?key=eq." + encodeURIComponent(key) + "&select=started", { headers: { apikey: ANON_KEY, Authorization: "Bearer " + token } });
    const gr = g.ok ? await g.json().catch(function () { return []; }) : [];
    if (gr && gr[0] && Date.now() - new Date(gr[0].started).getTime() > 60000) {
      await fetch(SUPABASE_URL + "/rest/v1/baidu_inflight?key=eq." + encodeURIComponent(key), { method: "PATCH", headers: { apikey: ANON_KEY, Authorization: "Bearer " + token, "Content-Type": "application/json", Prefer: "return=minimal" }, body: JSON.stringify({ started: new Date().toISOString() }) });
      return true;
    }
    return false;
  } catch { return true; }
}
async function baiduLockRelease(key, token) {
  try { await fetch(SUPABASE_URL + "/rest/v1/baidu_inflight?key=eq." + encodeURIComponent(key), { method: "DELETE", headers: { apikey: ANON_KEY, Authorization: "Bearer " + token, Prefer: "return=minimal" } }); } catch {}
}
async function baiduRateAcquire(token) {
  for (let i = 0; i < 6; i++) {
    try {
      const r = await fetch(SUPABASE_URL + "/rest/v1/rpc/baidu_try_acquire", { method: "POST", headers: { apikey: ANON_KEY, Authorization: "Bearer " + token, "Content-Type": "application/json" }, body: "{}" });
      if (!r.ok) return true;
      const v = await r.json().catch(function () { return null; });
      if (v === true) return true;
    } catch { return true; }
    await ibSleep(450);
  }
  return false;
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

    // 모델 선택: auto(기본, Flash 우선→Pro 대체) | pro | flash
    // Flash가 기본: 품질 체감 차이가 적고 하루 한도가 넉넉함 (Pro 한도는 새벽 채널 검수 로봇에 양보)
    const pref = String(body.model_pref || "auto");
    const MODELS =
      pref === "pro" ? ["gemini-pro-latest"]
      : pref === "flash" ? ["gemini-flash-latest"]
      : ["gemini-flash-latest", "gemini-pro-latest"];

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

    // ---------- 액션: 댓글 가져오기 (읽기 전용 테스트) ----------
    if (action === "yt_comments") {
      const cvid = String((req.body || {}).videoId || "").trim();
      if (!/^[A-Za-z0-9_-]{11}$/.test(cvid)) { res.status(400).json({ error: "videoId(11자리)가 필요합니다" }); return; }
      const cj = await ytApi("commentThreads", { part: "snippet", videoId: cvid, maxResults: 10, order: "relevance", textFormat: "plainText" });
      if (!cj) { res.status(500).json({ error: "YOUTUBE_API_KEY 미설정" }); return; }
      if (cj.__error) { res.status(502).json({ error: cj.__error }); return; }
      const citems = (cj.items || []).map(function (it) {
        const s = it && it.snippet && it.snippet.topLevelComment && it.snippet.topLevelComment.snippet;
        return s ? { text: String(s.textDisplay || "").slice(0, 200), likes: s.likeCount || 0 } : null;
      }).filter(Boolean);
      res.status(200).json({ ok: true, count: citems.length, comments: citems });
      return;
    }

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

      const r = await callGemini(MODELS, key, {
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
      });
      if (!r.ok) {
        let detail = r.detail || "";
        if (/not\s*found|unsupported|invalid/i.test(detail)) {
          detail = "영상을 불러올 수 없습니다 (비공개·삭제·연령제한 영상일 수 있음)";
        } else if (r.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(detail)) {
          detail = "오늘 AI 사용량이 모두 소진되었습니다. 몇 시간 후 다시 시도해 주세요.";
        }
        res.status(502).json({ error: "영상 분석 실패", detail: detail });
        return;
      }
      const analysis = parseJsonLoose(geminiText(r.gj));
      if (!analysis) {
        res.status(502).json({ error: "분석 결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, video_id: vid, analysis: analysis, model: r.model });
      return;
    }

    // ---------- 액션: 원본 후보 탐색 ----------
    if (action === "sources") {
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정되지 않았습니다" });
        return;
      }
      const vid = extractVideoId(body.video_url);
      if (!vid) {
        res.status(400).json({ error: "유튜브 영상 주소가 아닙니다" });
        return;
      }
      const videoUrl = "https://www.youtube.com/watch?v=" + vid;

      // 0) 캐시 확인: 같은 영상은 다시 탐색하지 않음 (검색 한도 소모 0, 즉시 표시)
      const srcForce = isAdmin && body.force === true;
      if (!srcForce) {
        const cachedSrc = await supaGet(
          "/rest/v1/video_sources?video_id=eq." + vid + "&select=result,created_at",
          token
        );
        if (Array.isArray(cachedSrc) && cachedSrc.length && cachedSrc[0].result) {
          const out = cachedSrc[0].result;
          out.cached = true;
          out.cached_at = cachedSrc[0].created_at;
          res.status(200).json(out);
          return;
        }
      }

      // 1) 영상 판독: 원본 단서 + 다국어 검색어 추출
      const r1 = await callGemini(MODELS, key, {
        contents: [
          { parts: [{ file_data: { file_uri: videoUrl } }, { text: SOURCE_PROMPT }] },
        ],
        generationConfig: {
          responseMimeType: "application/json",
          mediaResolution: "MEDIA_RESOLUTION_LOW",
          temperature: 0.2,
        },
      });
      if (!r1.ok) {
        let detail = r1.detail || "";
        if (/not\s*found|unsupported|invalid/i.test(detail)) {
          detail = "영상을 불러올 수 없습니다 (비공개·삭제·연령제한 영상일 수 있음)";
        } else if (r1.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(detail)) {
          detail = "오늘 AI 사용량이 모두 소진되었습니다. 몇 시간 후 다시 시도해 주세요.";
        }
        res.status(502).json({ error: "원본 단서 분석 실패", detail: detail });
        return;
      }
      const clue = parseJsonLoose(geminiText(r1.gj));
      if (!clue) {
        res.status(502).json({ error: "분석 결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }
      const queries = (Array.isArray(clue.queries) ? clue.queries : [])
        .filter((q) => q && q.q).slice(0, 3);

      // 2) 대상 영상 게시일 (유튜브 API 키가 서버에 있을 때)
      const ytAvailable = !!String(process.env.YOUTUBE_API_KEY || "").trim();
      const friendlyYt = (msg) => /quota/i.test(String(msg || ""))
        ? "오늘의 유튜브 검색 한도를 모두 사용했습니다 (매일 오후 4~5시쯤 초기화). 아래 검색 링크 버튼은 지금도 사용 가능합니다."
        : String(msg || "");
      let ytError = "";
      let target = { video_id: vid };
      if (ytAvailable) {
        const tj = await ytApi("videos", { part: "snippet", id: vid });
        if (tj && tj.__error) ytError = friendlyYt(tj.__error);
        else if (tj && tj.items && tj.items[0]) {
          target.title = tj.items[0].snippet.title;
          target.published_at = tj.items[0].snippet.publishedAt;
        }
      }

      // 3) 후보 수집 (유튜브 검색, 링크만 다룸)
      let candidates = [];
      const seen = {}; seen[vid] = true;
      if (ytAvailable) {
        for (let i = 0; i < queries.length; i++) {
          const sj = await ytApi("search", {
            part: "snippet", q: queries[i].q, type: "video", maxResults: "4",
          });
          if (sj && sj.__error) { if (!ytError) ytError = friendlyYt(sj.__error); continue; }
          if (!sj || !sj.items) continue;
          for (let k = 0; k < sj.items.length; k++) {
            const it = sj.items[k];
            const id2 = it.id && it.id.videoId;
            if (!id2 || seen[id2]) continue;
            seen[id2] = true;
            candidates.push({
              video_id: id2,
              title: it.snippet.title,
              channel: it.snippet.channelTitle,
              published_at: it.snippet.publishedAt,
              found_by: queries[i].q,
            });
          }
        }
      }
      if (target.published_at) {
        candidates.forEach((c) => {
          c.days_earlier = Math.round((new Date(target.published_at) - new Date(c.published_at)) / 86400000);
        });
      }
      candidates = candidates.slice(0, 10);

      // 3.5) 후보 상세정보 1회 조회 (길이·조회수·임베드) — search.list 추가 없음, videos.list 1회(약 1유닛)
      if (ytAvailable && candidates.length) {
        const detIds = candidates.map(function (c) { return c.video_id; }).slice(0, 50).join(",");
        const dj = await ytApi("videos", { part: "contentDetails,statistics,status", id: detIds });
        if (dj && dj.__error) { if (!ytError) ytError = dj.__error; }
        else if (dj && Array.isArray(dj.items)) {
          const dmap = {};
          dj.items.forEach(function (it) { dmap[it.id] = it; });
          candidates.forEach(function (c) {
            const it = dmap[c.video_id];
            if (!it) return;
            const iso = (it.contentDetails && it.contentDetails.duration) || "";
            const mm = iso.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
            let sec = 0;
            if (mm) sec = parseInt(mm[1] || "0", 10) * 3600 + parseInt(mm[2] || "0", 10) * 60 + parseInt(mm[3] || "0", 10);
            c.duration_seconds = sec;
            c.duration_label = Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
            c.view_count = (it.statistics && Number(it.statistics.viewCount)) || 0;
            c.embeddable = !!(it.status && it.status.embeddable);
            c.is_short_candidate = sec > 0 && sec <= 180;
            c.short_confidence = sec === 0 ? "알수없음" : (sec <= 60 ? "높음" : (sec <= 180 ? "중간" : "낮음"));
          });
        }
      }

      // 4) 가벼운 검증: 제목·게시일 기준 가능성 등급 (Flash 텍스트 호출)
      let ranked = [];
      if (candidates.length) {
        const rp = RANK_PROMPT
          .replace("__TARGET__", JSON.stringify(target))
          .replace("__ORIGIN__", String(clue.origin_guess || ""))
          .replace("__CANDS__", JSON.stringify(candidates));
        const r2 = await callGemini(["gemini-flash-latest"], key, {
          contents: [{ parts: [{ text: rp }] }],
          generationConfig: { responseMimeType: "application/json", temperature: 0.2 },
        });
        if (r2.ok) {
          const rr = parseJsonLoose(geminiText(r2.gj));
          if (rr && Array.isArray(rr.ranked)) ranked = rr.ranked;
        }
      }
      const byId = {};
      candidates.forEach((c) => { byId[c.video_id] = c; });
      let finalCands = ranked
        .map((r) => (byId[r.video_id] ? Object.assign({}, byId[r.video_id], { grade: r.grade, reason: r.reason, match_type: r.match_type || "" }) : null))
        .filter(Boolean);
      if (!finalCands.length && candidates.length) {
        finalCands = candidates.slice(0, 5).map((c) => Object.assign({}, c, { grade: "참고", reason: "" }));
      }
      finalCands = finalCands.slice(0, 5);

      // 5) 무료 스톡 검색 링크 + 유튜브 검색 링크 (전부 링크만 제공)
      const stock = (Array.isArray(clue.stock_keywords) ? clue.stock_keywords : []).slice(0, 3).map((k2) => ({
        keyword: k2,
        pexels: "https://www.pexels.com/search/videos/" + encodeURIComponent(k2) + "/",
        pixabay: "https://pixabay.com/videos/search/" + encodeURIComponent(k2) + "/",
      }));
      const ytLinks = queries.map((q) => ({
        q: q.q,
        lang: q.lang || "",
        type: q.type || "",
        ko: q.ko || "",
        why: q.why || "",
        url: "https://www.youtube.com/results?search_query=" + encodeURIComponent(q.q),
      }));

      const srcPayload = {
        ok: true,
        video_id: vid,
        origin: {
          guess: clue.origin_guess || "",
          type: clue.origin_type || "",
          confidence: clue.confidence || "",
          watermark: clue.watermark || "",
          language: clue.language || "",
        },
        target: target,
        candidates: finalCands,
        stock: stock,
        yt_links: ytLinks,
        yt_searched: ytAvailable,
        yt_error: ytError,
        model: r1.model,
      };
      // 캐시 저장: 정상 탐색(한도 오류 없음)일 때만 (실패 결과를 얼려두지 않음)
      if (ytAvailable && !ytError) {
        try {
          await fetch(SUPABASE_URL + "/rest/v1/video_sources?on_conflict=video_id", {
            method: "POST",
            headers: {
              apikey: ANON_KEY,
              Authorization: "Bearer " + token,
              "Content-Type": "application/json",
              Prefer: "resolution=merge-duplicates,return=minimal",
            },
            body: JSON.stringify({ video_id: vid, result: srcPayload }),
          });
        } catch {}
      }
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json(srcPayload);
      return;
    }

    // ---------- 액션: 영상 대본 따기 (실제 나레이션·자막 추출) ----------
    if (action === "transcript") {
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정되지 않았습니다" });
        return;
      }
      const vid = extractVideoId(body.video_url);
      if (!vid) {
        res.status(400).json({ error: "유튜브 영상 주소가 아닙니다" });
        return;
      }
      const videoUrl = "https://www.youtube.com/watch?v=" + vid;
      const r = await callGemini(MODELS, key, {
        contents: [
          {
            parts: [
              { file_data: { file_uri: videoUrl } },
              { text: TRANSCRIPT_PROMPT },
            ],
          },
        ],
        generationConfig: {
          responseMimeType: "application/json",
          mediaResolution: "MEDIA_RESOLUTION_LOW",
          temperature: 0.1,
        },
      });
      if (!r.ok) {
        let detail = r.detail || "";
        if (/not\s*found|unsupported|invalid/i.test(detail)) {
          detail = "영상을 불러올 수 없습니다 (비공개·삭제·연령제한 영상일 수 있음)";
        } else if (r.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(detail)) {
          detail = "오늘 AI 사용량이 모두 소진되었습니다. 몇 시간 후 다시 시도해 주세요.";
        }
        res.status(502).json({ error: "대본 추출 실패", detail: detail });
        return;
      }
      const tr = parseJsonLoose(geminiText(r.gj));
      if (!tr || !Array.isArray(tr.lines)) {
        res.status(502).json({ error: "대본 추출 결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, video_id: vid, transcript: tr, model: r.model });
      return;
    }

    // ---------- 액션: 대본 작성 ----------
    if (action === "script") {
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정되지 않았습니다" });
        return;
      }
      const analysis = body.analysis;
      if (!analysis || typeof analysis !== "object") {
        res.status(400).json({ error: "분석 결과가 필요합니다. 먼저 영상을 분석해 주세요." });
        return;
      }
      const topic = String(body.topic || "").trim().slice(0, 200);
      const prompt = SCRIPT_PROMPT
        .replace("__ANALYSIS__", JSON.stringify(analysis).slice(0, 4000))
        .replace(
          "__TOPIC__",
          topic
            ? "사용자가 원하는 새 소재: " + topic
            : "새 소재는 당신이 직접 제안하세요. 원본과 같은 카테고리에서, 한국 시청자가 궁금해할 검증된 흥미 소재로."
        );
      const r = await callGemini(MODELS, key, {
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { responseMimeType: "application/json", temperature: 0.8 },
      });
      if (!r.ok) {
        let detail = r.detail || "";
        if (r.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(detail)) {
          detail = "오늘 AI 사용량이 모두 소진되었습니다. 몇 시간 후 다시 시도해 주세요.";
        }
        res.status(502).json({ error: "대본 생성 실패", detail: detail });
        return;
      }
      const script = parseJsonLoose(geminiText(r.gj));
      if (!script || !Array.isArray(script.lines) || !script.lines.length) {
        res.status(502).json({ error: "대본 결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, script: script, model: r.model });
      return;
    }

    // ---------- 액션: 스톡 미리보기 (Pexels 검색, 키 없으면 조용히 비활성) ----------
    if (action === "stock") {
      const q = String(body.query || "").trim().slice(0, 80);
      if (!q) {
        res.status(400).json({ error: "검색어가 필요합니다" });
        return;
      }
      const pk = String(process.env.PEXELS_API_KEY || "").trim();
      if (!pk) {
        res.status(200).json({ ok: true, available: false, items: [] });
        return;
      }
      try {
        const pr = await fetch(
          "https://api.pexels.com/videos/search?query=" + encodeURIComponent(q) + "&per_page=9&orientation=portrait",
          { headers: { Authorization: pk } }
        );
        const pj = await pr.json().catch(() => null);
        if (!pr.ok) {
          res.status(502).json({ error: "Pexels 검색 실패", detail: (pj && pj.error) || ("HTTP " + pr.status) });
          return;
        }
        const items = ((pj && pj.videos) || []).map((v) => ({
          id: v.id,
          url: v.url,
          image: v.image,
          duration: v.duration,
          by: (v.user && v.user.name) || "",
        }));
        res.status(200).json({ ok: true, available: true, items: items });
      } catch (e) {
        res.status(502).json({ error: "Pexels 요청 오류", detail: String((e && e.message) || e) });
      }
      return;
    }

    // ---------- 액션: 구글렌즈 2단계 (Vision API 웹 감지 — 키 없으면 조용히 비활성) ----------
    if (action === "lens") {
      const vid = extractVideoId(body.video_url);
      if (!vid) {
        res.status(400).json({ error: "유튜브 영상 주소가 아닙니다" });
        return;
      }
      const vk = String(process.env.GOOGLE_VISION_API_KEY || "").trim();
      if (!vk) {
        res.status(200).json({ ok: true, available: false, items: [], pages: [] });
        return;
      }
      try {
        let thumb = "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg";
        try {
          const oarUrl = "https://i.ytimg.com/vi/" + vid + "/oar2.jpg";
          const oc = await fetch(oarUrl, { method: "HEAD" });
          if (oc.ok) thumb = oarUrl; // 쇼츠 세로 원본(1080x1920) — 흐린 좌우 띠 없음
        } catch {}
        const lReqImg = String((body && body.image_url) || "").trim();
    if (/^https:\/\/i\.ytimg\.com\/vi\/[A-Za-z0-9_-]{6,20}\/[A-Za-z0-9_]{1,20}\.jpg$/.test(lReqImg)) thumb = lReqImg;
    const vr = await fetch("https://vision.googleapis.com/v1/images:annotate?key=" + vk, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            requests: [{
              image: { source: { imageUri: thumb } },
              features: [{ type: "WEB_DETECTION", maxResults: 15 }],
            }],
          }),
        });
        const vj = await vr.json().catch(() => null);
        if (!vr.ok) {
          res.status(502).json({ error: "렌즈 검색 실패", detail: (vj && vj.error && vj.error.message) || ("HTTP " + vr.status) });
          return;
        }
        const w = (vj && vj.responses && vj.responses[0] && vj.responses[0].webDetection) || {};
        const items = [];
        (w.fullMatchingImages || []).forEach((x) => { if (x.url) items.push({ img: x.url, kind: "완전 일치" }); });
        (w.partialMatchingImages || []).forEach((x) => { if (x.url) items.push({ img: x.url, kind: "부분 일치" }); });
        (w.visuallySimilarImages || []).forEach((x) => { if (x.url) items.push({ img: x.url, kind: "비슷함" }); });
        const pages = (w.pagesWithMatchingImages || []).slice(0, 10).map((p) => ({
          url: p.url,
          title: String(p.pageTitle || "").replace(/<[^>]+>/g, ""),
          img: (((p.fullMatchingImages || [])[0] || {}).url) || (((p.partialMatchingImages || [])[0] || {}).url) || "",
        }));
        const labels = (w.bestGuessLabels || []).map((l) => l.label).filter(Boolean);
        if (!isAdmin) await recordUsage(user.id, feature, token);
        res.status(200).json({ ok: true, available: true, items: items.slice(0, 10), pages: pages, labels: labels, img_kind: (thumb.indexOf("oar2") !== -1 ? "세로원본" : "가로썸네일") });
      } catch (e) {
        res.status(502).json({ error: "렌즈 요청 오류", detail: String((e && e.message) || e) });
      }
      return;
    }

    // ---------- 액션: 후킹 분석 (첫 3~5초 집중) ----------
    if (action === "hook") {
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정되지 않았습니다" });
        return;
      }
      const vid = extractVideoId(body.video_url);
      if (!vid) {
        res.status(400).json({ error: "유튜브 영상 주소가 아닙니다" });
        return;
      }
      const videoUrl = "https://www.youtube.com/watch?v=" + vid;
      const r = await callGemini(MODELS, key, {
        contents: [
          { parts: [{ file_data: { file_uri: videoUrl } }, { text: HOOK_PROMPT }] },
        ],
        generationConfig: {
          responseMimeType: "application/json",
          mediaResolution: "MEDIA_RESOLUTION_LOW",
          temperature: 0.3,
        },
      });
      if (!r.ok) {
        let detail = r.detail || "";
        if (/not\s*found|unsupported|invalid/i.test(detail)) {
          detail = "영상을 불러올 수 없습니다 (비공개·삭제·연령제한 영상일 수 있음)";
        } else if (r.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(detail)) {
          detail = "오늘 AI 사용량이 모두 소진되었습니다. 몇 시간 후 다시 시도해 주세요.";
        }
        res.status(502).json({ error: "후킹 분석 실패", detail: detail });
        return;
      }
      const hk = parseJsonLoose(geminiText(r.gj));
      if (!hk || !hk.main || !hk.facts) {
        res.status(502).json({ error: "후킹 분석 결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }
      // "기타(새 유형)" 판정은 자동 기록 → 작업실에서 검토 후 분류표 승격 (실패해도 무시)
      try {
        if (Number(hk.main.type_id) === 0 || hk.main.is_new === true) {
          await fetch(SUPABASE_URL + "/rest/v1/hook_extra_log", {
            method: "POST",
            headers: {
              apikey: ANON_KEY,
              Authorization: "Bearer " + token,
              "Content-Type": "application/json",
              Prefer: "return=minimal",
            },
            body: JSON.stringify({
              video_id: vid,
              name: String(hk.main.name || "").slice(0, 80),
              description: String(hk.psychology || "").slice(0, 300),
            }),
          });
        }
      } catch {}
      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, video_id: vid, hook: hk, model: r.model });
      return;
    }

    // ---------- 액션: 재사용 가능(CC) 영상 검색 — 클릭할 때만 호출 ----------
    if (action === "cc_search") {
      const q = String(body.query || "").trim().slice(0, 100);
      if (!q) {
        res.status(400).json({ error: "검색어가 필요합니다" });
        return;
      }
      if (!String(process.env.YOUTUBE_API_KEY || "").trim()) {
        res.status(200).json({ ok: true, available: false, items: [] });
        return;
      }
      // 캐시: 같은 검색어는 24시간 안에 다시 검색하지 않음 (한도 절약)
      const ccCached = await supaGet(
        "/rest/v1/cc_cache?query=eq." + encodeURIComponent(q) + "&select=items,created_at",
        token
      );
      if (Array.isArray(ccCached) && ccCached.length &&
          Date.now() - new Date(ccCached[0].created_at).getTime() < 24 * 3600 * 1000) {
        res.status(200).json({ ok: true, available: true, items: ccCached[0].items || [], cached: true });
        return;
      }
      const sj = await ytApi("search", {
        part: "snippet", q: q, type: "video", videoLicense: "creativeCommon", maxResults: "6",
      });
      if (sj && sj.__error) {
        const friendly = /quota/i.test(sj.__error)
          ? "오늘의 유튜브 검색 한도를 모두 사용했습니다. 한도는 매일 오후 4~5시쯤 초기화되니 그 이후에 다시 시도해 주세요. (유튜브 링크 버튼은 지금도 사용 가능합니다)"
          : sj.__error;
        res.status(502).json({ error: "CC 영상 검색 실패", detail: friendly });
        return;
      }
      const items = ((sj && sj.items) || []).map((it) => ({
        video_id: it.id && it.id.videoId,
        title: it.snippet.title,
        channel: it.snippet.channelTitle,
        published_at: it.snippet.publishedAt,
      })).filter((x) => x.video_id);
      try {
        await fetch(SUPABASE_URL + "/rest/v1/cc_cache?on_conflict=query", {
          method: "POST",
          headers: {
            apikey: ANON_KEY,
            Authorization: "Bearer " + token,
            "Content-Type": "application/json",
            Prefer: "resolution=merge-duplicates,return=minimal",
          },
          body: JSON.stringify({ query: q, items: items, created_at: new Date().toISOString() }),
        });
      } catch {}
      res.status(200).json({ ok: true, available: true, items: items });
      return;
    }

    // ---------- 액션: 후킹 추가 버전 (영상 재분석 없는 텍스트 호출 — 저비용) ----------
    if (action === "hook_more") {
      if (!key) {
        res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정되지 않았습니다" });
        return;
      }
      const ctx = body.hook;
      if (!ctx || typeof ctx !== "object" || !ctx.facts) {
        res.status(400).json({ error: "먼저 후킹 분석을 실행해 주세요." });
        return;
      }
      const NAMES = { 1: "호기심 갭", 2: "결과 먼저", 3: "충격 비주얼", 4: "질문 던지기", 5: "공감 저격", 6: "손실 회피", 7: "숫자·랭킹", 8: "반전 예고", 9: "권위·증거", 10: "패턴 파괴" };
      const ids = (Array.isArray(body.strategies) ? body.strategies : [])
        .map(Number).filter((n) => n >= 1 && n <= 10).slice(0, 10);
      if (!ids.length) {
        res.status(400).json({ error: "전략을 선택해 주세요." });
        return;
      }
      const context = JSON.stringify({
        facts: ctx.facts, main: ctx.main, sub: ctx.sub, psychology: ctx.psychology,
      }).slice(0, 3500);
      const prompt = HOOK_MORE_PROMPT
        .replace("__CONTEXT__", context)
        .replace("__STRATS__", ids.map((n) => n + " " + NAMES[n]).join(", "));
      const r = await callGemini(["gemini-flash-latest", "gemini-pro-latest"], key, {
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { responseMimeType: "application/json", temperature: 0.7 },
      });
      if (!r.ok) {
        let detail = r.detail || "";
        if (r.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(detail)) {
          detail = "오늘 AI 사용량이 모두 소진되었습니다. 몇 시간 후 다시 시도해 주세요.";
        }
        res.status(502).json({ error: "추가 버전 생성 실패", detail: detail });
        return;
      }
      const mv = parseJsonLoose(geminiText(r.gj));
      if (!mv || !Array.isArray(mv.variations) || !mv.variations.length) {
        res.status(502).json({ error: "결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }
      res.status(200).json({ ok: true, variations: mv.variations, model: r.model });
      return;
    }

    // ---------- 액션: 제작 설계도 (장면 분해 BOM) ----------
    if (action === "blueprint") {
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

      // 1) 캐시 확인: 같은 영상은 다시 분석하지 않음 (비용 0원, 한도 차감 없음)
      const force = isAdmin && body.force === true;
      if (!force) {
        const cached = await supaGet(
          "/rest/v1/video_blueprints?video_id=eq." + vid + "&select=blueprint,model,created_at",
          token
        );
        if (Array.isArray(cached) && cached.length && cached[0].blueprint) {
          res.status(200).json({
            ok: true, video_id: vid,
            blueprint: cached[0].blueprint,
            model: cached[0].model || "",
            cached: true, cached_at: cached[0].created_at,
          });
          return;
        }
      }

      // 2) 새로 분석
      const r = await callGemini(MODELS, key, {
        contents: [
          { parts: [{ file_data: { file_uri: videoUrl } }, { text: BLUEPRINT_PROMPT }] },
        ],
        generationConfig: {
          responseMimeType: "application/json",
          mediaResolution: "MEDIA_RESOLUTION_LOW",
          temperature: 0.2,
        },
      });
      if (!r.ok) {
        let detail = r.detail || "";
        if (/not\s*found|unsupported|invalid/i.test(detail)) {
          detail = "영상을 불러올 수 없습니다 (비공개·삭제·연령제한 영상일 수 있음)";
        } else if (r.status === 429 || /quota|RESOURCE_EXHAUSTED/i.test(detail)) {
          detail = "오늘 AI 사용량이 모두 소진되었습니다. 몇 시간 후 다시 시도해 주세요.";
        }
        res.status(502).json({ error: "제작 설계도 생성 실패", detail: detail });
        return;
      }
      const bp = parseJsonLoose(geminiText(r.gj));
      if (!bp || !Array.isArray(bp.scenes) || !bp.scenes.length) {
        res.status(502).json({ error: "설계도 결과 해석에 실패했습니다. 다시 시도해 주세요." });
        return;
      }

      // 3) 캐시 저장 (실패해도 결과는 정상 반환)
      try {
        await fetch(SUPABASE_URL + "/rest/v1/video_blueprints?on_conflict=video_id", {
          method: "POST",
          headers: {
            apikey: ANON_KEY,
            Authorization: "Bearer " + token,
            "Content-Type": "application/json",
            Prefer: "resolution=merge-duplicates,return=minimal",
          },
          body: JSON.stringify({ video_id: vid, blueprint: bp, model: r.model, created_by: user.id }),
        });
      } catch {}

      if (!isAdmin) await recordUsage(user.id, feature, token);
      res.status(200).json({ ok: true, video_id: vid, blueprint: bp, model: r.model, cached: false });
      return;
    }

    // ---------- 액션: 바이두 유사 이미지 검색 (공식 千帆 相似图搜索 API) ----------
    // ── 구글 렌즈 검색 (SearchApi 중계) — SEARCHAPI_KEY 필요, 없으면 건너뜀 ──
  if (action === "lens_pro") {
    const lpKey = String(process.env.SEARCHAPI_KEY || "").trim();
    if (!lpKey) { res.status(200).json({ ok: true, available: false, pages: [] }); return; }
    const lpVid = extractVideoId(body.video_url);
    let lpImg = lpVid ? ("https://i.ytimg.com/vi/" + lpVid + "/hqdefault.jpg") : "";
    const lpReq = String((body && body.image_url) || "").trim();
    if (/^https:\/\/i\.ytimg\.com\/vi\/[A-Za-z0-9_-]{6,20}\/[A-Za-z0-9_]{1,20}\.jpg$/.test(lpReq)) lpImg = lpReq;
    if (!lpImg) { res.status(400).json({ error: "이미지가 없습니다" }); return; }
    try {
      const lpR = await fetch("https://www.searchapi.io/api/v1/search?engine=google_lens&url=" + encodeURIComponent(lpImg) + "&api_key=" + lpKey);
      const lpJ = await lpR.json().catch(function(){ return null; });
      if (!lpR.ok || !lpJ) { res.status(200).json({ ok: false, error: "렌즈 검색 실패 HTTP " + lpR.status }); return; }
      const lpPages = Array.isArray(lpJ.visual_matches) ? lpJ.visual_matches.map(function(m){
        return { url: m.link, title: m.title || "", img: m.thumbnail || (m.image && m.image.link) || "" };
      }).filter(function(p){ return p.url; }) : [];
      res.status(200).json({ ok: true, available: true, pages: lpPages });
    } catch (e) {
      res.status(200).json({ ok: false, error: "렌즈 오류", detail: String((e && e.message) || e).slice(0, 120) });
    }
    return;
  }

  if (action === "baidu_sim") {
      const bkey = String(process.env.BAIDU_API_KEY || "").trim();
      if (!bkey) { res.status(200).json({ ok: true, available: false, items: [], note: "BAIDU_API_KEY 미설정" }); return; }
      const bvid = extractVideoId(body.video_url);
      if (!bvid) { res.status(400).json({ error: "유튜브 영상 주소가 아닙니다" }); return; }
      let bimg = "https://i.ytimg.com/vi/" + bvid + "/hqdefault.jpg";
      try {
        const oc2 = await fetch("https://i.ytimg.com/vi/" + bvid + "/oar2.jpg", { method: "HEAD" });
        if (oc2.ok) bimg = "https://i.ytimg.com/vi/" + bvid + "/oar2.jpg";
      } catch {}
      const bReqImg = String((body && body.image_url) || "").trim();
    if (/^https:\/\/i\.ytimg\.com\/vi\/[A-Za-z0-9_-]{6,20}\/[A-Za-z0-9_]{1,20}\.jpg$/.test(bReqImg)) bimg = bReqImg;
      // [중복방지] 서버 공유 캐시 먼저 확인 — 있으면 바이두 호출 없이 즉시 반환
      const bCacheKey = "baidu:" + bimg;
      const bCachedSrv = await sbGetCacheSrv(bCacheKey, token);
      if (bCachedSrv && bCachedSrv.ok === true) {
        res.status(200).json(Object.assign({ from_cache: true }, bCachedSrv));
        return;
      }
      // [중복방지] 같은 사진 검색이 진행 중이면 잠시 기다렸다 그 결과 재사용
      const bGotLock = await baiduLockAcquire(bCacheKey, token);
      if (!bGotLock) {
        for (let bw = 0; bw < 7; bw++) {
          await ibSleep(900);
          const bAgain = await sbGetCacheSrv(bCacheKey, token);
          if (bAgain && bAgain.ok === true) { res.status(200).json(Object.assign({ from_cache: true }, bAgain)); return; }
        }
      }
      // [안전장치] 예산 상한·긴급 중단 확인 — 차단 시 구글 렌즈로 안내
      const bguard = await baiduGuard(user.id, token, isAdmin);
      if (!bguard.allow) {
        await baiduLockRelease(bCacheKey, token);
        await logBaidu(user.id, token, bvid, { ok: false, blocked: true, reason: String(bguard.reason || "").slice(0, 60) });
        res.status(200).json({ ok: false, blocked: true, reason: bguard.reason, use_lens: true, items: [] });
        return;
      }
      // [1초 1회] 속도제한 — 혼잡하면 잠시 대기, 계속 혼잡하면 렌즈 전환
      const bRateOk = await baiduRateAcquire(token);
      if (!bRateOk) {
        await baiduLockRelease(bCacheKey, token);
        await logBaidu(user.id, token, bvid, { ok: false, blocked: true, reason: "rate_limited" });
        res.status(200).json({ ok: false, blocked: true, reason: "rate_limited", use_lens: true, items: [] });
        return;
      }
    let bb64 = "";
      try {
        const ir = await fetch(bimg);
        if (!ir.ok) { await baiduLockRelease(bCacheKey, token); res.status(502).json({ error: "썸네일을 가져오지 못했습니다 (HTTP " + ir.status + ")" }); return; }
        bb64 = Buffer.from(await ir.arrayBuffer()).toString("base64");
      } catch (e) {
        await baiduLockRelease(bCacheKey, token); res.status(502).json({ error: "썸네일 다운로드 실패", detail: String((e && e.message) || e) });
        return;
      }
      try {
        const br = await fetch("https://qianfan.baidubce.com/v2/tools/image_similar_info", {
          method: "POST",
          headers: { Authorization: "Bearer " + bkey, "Content-Type": "application/json" },
          body: JSON.stringify({ image: bb64, count: Math.min(Number(body.count) || 20, 50) }),
        });
        const bj = await br.json().catch(() => null);
        const bcode = bj ? String(bj.code != null ? bj.code : (bj.error_code != null ? bj.error_code : "")) : "";
        const bres = bj && bj.result ? bj.result : null;
        const bResErr = bres && typeof bres.err_code !== "undefined" ? Number(bres.err_code) : 0;
        if (!br.ok || !bj || (bcode !== "0" && bcode !== "") || bResErr !== 0) {
          await baiduLockRelease(bCacheKey, token); 
        await logBaidu(user.id, token, bvid, { ok: false, blocked: false, err_code: String(bcode || "").slice(0, 60), http_status: br.status });
          res.status(200).json({
            ok: false, http: br.status,
            err_code: bcode || null,
            err_msg: String((bj && (bj.message || bj.error_msg)) || "").slice(0, 200),
            raw_keys: bj ? Object.keys(bj).slice(0, 10) : [],
          });
          return;
        }
        let barr = (bres && bres.res_data && Array.isArray(bres.res_data.res_items)) ? bres.res_data.res_items : [];
        const bitems = barr.map(function (it) {
          return {
            cate: it.item_cate || "", sim: it.sim_level || 0,
            url: it.fromurl || it.detail_page || "", img: it.objurl || "",
            title: it.title || "", site: it.site_name || "",
            w: it.width || 0, h: it.height || 0,
          };
        });
        const sameN = bitems.filter(function (x) { return x.cate === "CATE_SAME"; }).length;
        await logBaidu(user.id, token, bvid, { ok: true, blocked: false, item_count: bitems.length, same_count: sameN, http_status: 200 });
        const bOut = {
          ok: true, available: true,
          img_kind: (bimg.indexOf("oar2") !== -1 ? "세로원본" : "가로썸네일"),
          count: bitems.length, same_count: sameN, items: bitems,
        };
        await sbPutCacheSrv(bCacheKey, bvid, bOut, token);
        await baiduLockRelease(bCacheKey, token);
        res.status(200).json(bOut);
      } catch (e) {
        await baiduLockRelease(bCacheKey, token); res.status(502).json({ error: "바이두 API 호출 실패", detail: String((e && e.message) || e) });
      }
      return;
    }

    res.status(400).json({ error: "알 수 없는 요청입니다: " + (action || "(없음)") });
  } catch (e) {
    res.status(500).json({ error: "서버 오류", detail: String((e && e.message) || e) });
  }
};
