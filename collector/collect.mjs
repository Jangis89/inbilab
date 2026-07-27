// ============================================
// 인비랩 데이터 수집 로봇
// 매일 자동 실행: 유튜브 인기 영상/채널 데이터를 수집해 Supabase에 저장
// 필요한 환경변수(GitHub Secrets):
//   YOUTUBE_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE
// ============================================

const YT_KEY = process.env.YOUTUBE_API_KEY;
const SB_URL = process.env.SUPABASE_URL;
const SB_KEY = process.env.SUPABASE_SERVICE_ROLE;

if (!YT_KEY || !SB_URL || !SB_KEY) {
  console.error("환경변수(YOUTUBE_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE)가 없습니다.");
  process.exit(1);
}

// 한국 시간 기준 날짜 (YYYY-MM-DD)
function kstDate(offsetDays = 0) {
  const d = new Date(Date.now() + 9 * 3600 * 1000 + offsetDays * 86400 * 1000);
  return d.toISOString().slice(0, 10);
}
const TODAY = kstDate(0);
const YESTERDAY = kstDate(-1);

// 구독자 상한: 이 값 이상(50만 이상) 채널은 수집·저장·표시하지 않음 (입문자 벤치마킹용)
const MAX_SUBS = 500000;

// ---------- Supabase REST 호출 도우미 ----------
async function sbFetch(path, { method = "GET", body, prefer } = {}) {
  const headers = {
    apikey: SB_KEY,
    Authorization: `Bearer ${SB_KEY}`,
    "Content-Type": "application/json",
  };
  if (prefer) headers.Prefer = prefer;
  const res = await fetch(`${SB_URL}/rest/v1/${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(`Supabase ${method} ${path} 실패 (${res.status}): ${t.slice(0, 300)}`);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

// ---------- YouTube API 호출 도우미 ----------
async function ytFetch(endpoint, params) {
  const qs = new URLSearchParams({ ...params, key: YT_KEY });
  const res = await fetch(`https://www.googleapis.com/youtube/v3/${endpoint}?${qs}`);
  const data = await res.json();
  if (data.error) throw new Error(`YouTube ${endpoint} 오류: ${data.error.message}`);
  return data;
}

// ISO8601 재생시간(PT1M30S) → 초
function durationSec(iso) {
  const m = /PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/.exec(iso || "");
  if (!m) return 0;
  return (Number(m[1] || 0) * 3600) + (Number(m[2] || 0) * 60) + Number(m[3] || 0);
}

// 진짜 쇼츠인지 유튜브에 직접 확인 (쇼츠 주소로 열리면 쇼츠)
async function checkRealShort(videoId) {
  try {
    const res = await fetch(`https://www.youtube.com/shorts/${videoId}`, {
      method: "HEAD",
      redirect: "manual",
    });
    if (res.status === 200) return true;       // 쇼츠 주소 그대로 열림 → 진짜 쇼츠
    if (res.status >= 300 && res.status < 400) return false; // 일반 영상으로 이동됨 → 쇼츠 아님
    return null; // 판별 불가
  } catch {
    return null;
  }
}

function chunk(arr, size) {
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

// ---------- Phase 1: 유튜브 급상승 쇼츠 발굴 (시간창 × 검색어 성적 배분) ----------
// 검색어는 DB(search_terms 표)에서 관리: 성적 좋은 검색어에 검색 자원의 대부분을 배분(80%),
// 나머지는 새 검색어 탐색(20%). 시간창(1·3·7일)으로 "오늘 막 터진 채널"까지 잡음.
const DEFAULT_SEEDS = [
  "영화 요약 쇼츠", "드라마 요약", "명장면 모음", "레전드 모음", "랭킹 top",
  "이슈 정리", "해외반응", "동물 모음", "게임 하이라이트", "애니 썰",
  "역사 이슈", "실화 사건", "썰 애니메이션", "정보 꿀팁 쇼츠", "충격 모음", "스포츠 명장면",
];

async function loadSearchTerms() {
  try {
    const rows = await sbFetch("search_terms?select=term,score,last_run&enabled=eq.true&limit=100");
    if (rows && rows.length > 0) return rows;
    // 최초 실행: 기본 검색어를 표에 등록
    await sbFetch("search_terms?on_conflict=term", {
      method: "POST",
      body: DEFAULT_SEEDS.map((t) => ({ term: t, source: "기본", enabled: true })),
      prefer: "resolution=merge-duplicates",
    });
  } catch (e) {
    console.log(`검색어 표 사용 불가(기본 검색어로 진행): ${e.message}`);
  }
  return DEFAULT_SEEDS.map((t) => ({ term: t, score: null, last_run: null }));
}

async function searchOnce(q, days, hits, discoverySrc) {
  const publishedAfter = new Date(Date.now() - days * 86400 * 1000).toISOString();
  const data = await ytFetch("search", {
    part: "snippet",
    type: "video",
    videoDuration: "short",       // 4분 미만(쇼츠 후보) — 이후 재생시간으로 진짜 쇼츠 확정
    order: "viewCount",           // 조회수 높은 순 = 지금 뜨는 영상
    regionCode: "KR",
    relevanceLanguage: "ko",
    publishedAfter,
    maxResults: "50",
    q,
  });
  let n = 0;
  for (const it of data.items || []) {
    const cid = it.snippet?.channelId;
    if (!cid) continue;
    hits.set(cid, (hits.get(cid) || 0) + 1);
    if (!discoverySrc.has(cid)) discoverySrc.set(cid, `검색:${q}|${days}일`);
    n++;
  }
  return n;
}

async function discoverShortsChannels(termRows, maxChannels = 250) {
  const WINDOWS = [1, 3, 7]; // 최근 1일 / 3일 / 7일 (모두 조회수순)
  const proven = termRows.filter((t) => t.last_run).sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0));
  const fresh = termRows.filter((t) => !t.last_run);
  // 성적 상위 8개 = 시간창 3개 모두, 그 다음 8개 = 7일 1개, 새 검색어 최대 6개 = 7일 1개 (탐색)
  let top, rest, explore;
  if (proven.length === 0) {
    top = fresh.slice(0, 8); rest = fresh.slice(8, 16); explore = fresh.slice(16, 22);
  } else {
    top = proven.slice(0, 8); rest = proven.slice(8, 16); explore = fresh.slice(0, 6);
  }
  const plan = [
    ...top.flatMap((t) => WINDOWS.map((d) => ({ term: t.term, days: d }))),
    ...rest.map((t) => ({ term: t.term, days: 7 })),
    ...explore.map((t) => ({ term: t.term, days: 7 })),
  ]; // 최대 38회 검색 = 하루 쿼터의 약 38%
  const hits = new Map();          // channelId -> 노출 횟수
  const discoverySrc = new Map();  // channelId -> "검색:검색어|시간창"
  const ranTerms = new Set();
  for (const p of plan) {
    try {
      await searchOnce(p.term, p.days, hits, discoverySrc);
      ranTerms.add(p.term);
    } catch (e) {
      console.log(`  검색 실패(${p.term}/${p.days}일): ${e.message}`);
    }
  }
  const ranked = [...hits.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id);
  console.log(`쇼츠 발굴: 검색 ${plan.length}회(검색어 ${ranTerms.size}개, 시간창 1·3·7일) → 후보 ${ranked.length}개, 상위 ${Math.min(maxChannels, ranked.length)}개 사용`);
  return { ids: ranked.slice(0, maxChannels), discoverySrc, ranTerms };
}

// ---------- Phase 2: 영상 1개 단위 레퍼런스 분석 (제목+썸네일 1차 분석) ----------
async function analyzeVideo(v, ch, gemKey, model = "gemini-pro-latest") {
  const prompt =
    `너는 유튜브 쇼츠 교육 플랫폼의 영상 분석가야. 아래 무출연 쇼츠 '영상 1개'를 40-60대 입문 수강생이 벤치마킹할 수 있게 분석해.\n` +
    `채널: ${ch.title || ""}${ch.ai_genre ? ` (${ch.ai_genre})` : ""}\n영상 제목: ${v.title}\n조회수: ${v.view_count}\n첨부 썸네일도 참고해.\n\n` +
    `반드시 아래 JSON만 반환(모든 필드는 실제 분석값으로 채워라):\n` +
    `{"fit_score":"<0~100 정수: 입문자 벤치마킹 적합도>","hook":"첫 1~2초 훅이 어떻게 시선을 끄는지 한 줄",` +
    `"structure":"영상 구성(도입-전개-마무리) 한 줄","benchmark":"수강생이 배워야 할 핵심 한 줄",` +
    `"caution":"그대로 베끼면 안 되는 것 한 줄","ideas":["같은 구조로 만들 수 있는 새 소재 3개"]}`;
  const parts = [{ text: prompt }];
  if (v.thumbnail) {
    const b64 = await fetchImageBase64(v.thumbnail);
    if (b64) parts.push({ inline_data: { mime_type: "image/jpeg", data: b64 } });
  }
  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${gemKey}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contents: [{ parts }], generationConfig: { responseMimeType: "application/json", temperature: 0.3 } }),
      }
    );
    const data = await res.json();
    if (data.error) {
      console.log(`  영상 분석 오류(${(v.title || "").slice(0, 20)}): ${String(data.error.message).slice(0, 100)}`);
      if (/quota|RESOURCE_EXHAUSTED|rate/i.test(String(data.error.message))) return { quota: true };
      return null;
    }
    let o = JSON.parse(data.candidates?.[0]?.content?.parts?.[0]?.text || "{}");
    if (Array.isArray(o)) o = o[0] || {};
    const fit = Math.max(0, Math.min(100, Math.round(Number(o.fit_score) || 0)));
    if (!fit && !(o.hook || "").trim()) return null; // 응답 불량 → 다음 실행에서 재시도
    return {
      fit,
      hook: (o.hook || "").slice(0, 200),
      structure: (o.structure || "").slice(0, 200),
      benchmark: (o.benchmark || "").slice(0, 200),
      caution: (o.caution || "").slice(0, 200),
      ideas: Array.isArray(o.ideas) ? o.ideas.slice(0, 3).map((s) => String(s).slice(0, 80)) : [],
    };
  } catch (e) {
    console.log(`  영상 분석 실패(${(v.title || "").slice(0, 20)}): ${e.message}`);
    return null;
  }
}

// ---------- 썸네일 이미지를 base64로 (Gemini 비전 입력용) ----------
async function fetchImageBase64(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const buf = Buffer.from(await res.arrayBuffer());
    return buf.toString("base64");
  } catch {
    return null;
  }
}

// ---------- Gemini로 "레퍼런스 가치" 판정 (무출연 여부 + 이식성/반복성/실행성/교육가치) ----------
// model: 기본 Pro(품질 우선). Pro 일일 한도(250회) 도달 시 호출부에서 Flash로 자동 전환.
async function classifyChannel(ch, vids, gemKey, gold = "", model = "gemini-pro-latest") {
  const titles = vids.map((v, i) => `${i + 1}. ${v.title}`).join("\n");
  const goldBlock = gold
    ? `[정답 예시 — 튜브랩이 엄선한 무출연 편집형 쇼츠 채널]\n${gold}\n\n`
    : "";
  const negBlock =
    `[탈락 신호 — 아래에 해당할수록 낮은 점수를 줘]\n` +
    `- 제작자 본인 얼굴/목소리/캐릭터성이 성과의 핵심 (브이로그, 먹방, 리액션, 페이스캠, 본인 해설)\n` +
    `- 채널 유명세·팬덤, 유명 연예인/인플루언서/선수의 인기 자체에 의존\n` +
    `- 독점 촬영, 희귀 사건, 우연한 장면이 성과의 핵심 (재현 불가)\n` +
    `- 영화/방송/스포츠/뉴스/타인 SNS 원본을 거의 그대로 재사용 (자막·TTS만 추가한 저변형)\n` +
    `- 단순 모음·짜깁기, 매번 똑같은 반복 양산형\n` +
    `- 고가 장비·전문 촬영팀·고급 3D가 필수\n` +
    `- 일회성 이슈라 같은 포맷으로 10개 이상 확장이 어려움\n\n`;
  const prompt =
    `너는 유튜브 쇼츠 교육 플랫폼의 채널 분석가야. 수강생(40-60대 입문자)이 "자기 얼굴·목소리 없이, 훅·구성·편집 원리를 배워서 다른 소재로 재구성해 반복 운영"할 가치가 있는 채널인지 판정해.\n` +
    `핵심 규칙 1(무출연): 영상 속 배우·행인 등 '타인'의 얼굴/목소리는 괜찮다. '제작자 본인'이 출연해야만 만들 수 있는 채널만 출연형이다. (영화요약 쇼츠는 배우가 나와도 무출연)\n` +
    `핵심 규칙 2(가치): 단순히 조회수가 높거나 무출연이라고 합격이 아니다. 포맷 자체가 성과를 만들고, 다른 소재로 바꿔도 작동하며, 초보자가 같은 구조로 10개 이상 만들 수 있어야 한다.\n\n` +
    goldBlock + negBlock +
    `채널명: ${ch.title}\n최근 영상 제목:\n${titles}\n\n첨부된 썸네일 이미지도 함께 참고해.\n\n` +
    `점수 기준(총 100): transfer_score 0~25(성공원인 이식성: 팬덤/유명인/독점소스 의존 낮고 훅·포맷 자체가 성과 원인), ` +
    `monetize_score 0~25(수익화 생존성: 독창적 해설·구성 충분, 저변형 재업로드 위험 낮음, 광고 친화), ` +
    `repeat_score 0~20(시리즈 반복성: 같은 포맷으로 소재만 바꿔 10~20개 확장 가능, 소스 지속 확보 가능), ` +
    `feasible_score 0~15(초보 실행성: 무출연·TTS 가능·직접촬영 불필요·일반 PC 편집도구로 제작), ` +
    `educate_score 0~15(교육 가치: 첫 1~2초 훅이 명확, 구성·편집 원리를 추출해 가르칠 수 있음).\n` +
    `series_repeated: 이 채널이 같은 포맷을 여러 영상에서 반복 운영 중이면 true(채널 검증), 한두 개만 터진 상태면 false.\n\n` +
    `반드시 아래 형태의 JSON만 반환해. 모든 점수 필드는 반드시 네가 평가한 실제 정수 점수로 채워라(0은 '해당 영역 완전 낙제'일 때만 사용):\n` +
    `{"creator_face":"none|brief|main","creator_voice":"none|main","voice_type":"none|ai_tts|original|music",` +
    `"content_format":"movie_recap|drama_recap|ranking|animation|game_edit|issue_tts|animal|sports|music|other",` +
    `"transfer_score":"<0~25 정수>","monetize_score":"<0~25 정수>","repeat_score":"<0~20 정수>","feasible_score":"<0~15 정수>","educate_score":"<0~15 정수>",` +
    `"series_repeated":true,"copyright_risk":"low|mid|high","confidence":"<0~1 소수>","genre":"한국어 장르","reason":"한 줄 근거",` +
    `"benchmark":"수강생이 참고할 포인트 한 줄","caution":"그대로 따라하면 안 되는 것 한 줄"}`;
  const parts = [{ text: prompt }];
  for (const v of vids.slice(0, 4)) {
    if (!v.thumbnail) continue;
    const b64 = await fetchImageBase64(v.thumbnail);
    if (b64) parts.push({ inline_data: { mime_type: "image/jpeg", data: b64 } });
  }
  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${gemKey}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts }],
          generationConfig: { responseMimeType: "application/json", temperature: 0.2 },
        }),
      }
    );
    const data = await res.json();
    if (data.error) {
      console.log(`  Gemini 오류(${ch.title}): ${String(data.error.message).slice(0, 140)}`);
      // 일일 한도/쿼터 초과는 호출부에 알려 모델 전환(Pro→Flash)을 유도
      if (/quota|RESOURCE_EXHAUSTED|rate/i.test(String(data.error.message))) return { quota: true };
      return null;
    }
    const txt = data.candidates?.[0]?.content?.parts?.[0]?.text || "";
    let o = JSON.parse(txt);
    if (Array.isArray(o)) o = o[0] || {};                       // 배열로 감싸서 주는 경우
    if (o.scores && typeof o.scores === "object") o = { ...o, ...o.scores }; // 점수를 중첩해 주는 경우
    // 무출연 최종 판정 = 제작자 본인 얼굴이 none/brief 이고, 본인 목소리가 none (타인 얼굴/목소리는 무관)
    const faceless = (o.creator_face === "none" || o.creator_face === "brief") && o.creator_voice === "none";
    // 점수 필드가 누락됐거나(파싱 실패) 전부 0인데 근거도 없으면 → 저장하지 않고 다음 실행에서 재시도
    const rawScores = [o.transfer_score, o.monetize_score, o.repeat_score, o.feasible_score, o.educate_score];
    const missing = rawScores.some((v) => v == null || v === "" || isNaN(Number(v)));
    const allZero = rawScores.every((v) => Number(v) === 0);
    if (missing || (allZero && !(o.reason || "").trim())) {
      console.log(`  점수 응답 불량(${ch.title}): ${txt.slice(0, 120)} → 재시도 예정`);
      return null;
    }
    const cap = (x, max) => Math.max(0, Math.min(max, Math.round(Number(x) || 0)));
    const scores = {
      transfer: cap(o.transfer_score, 25),
      monetize: cap(o.monetize_score, 25),
      repeat: cap(o.repeat_score, 20),
      feasible: cap(o.feasible_score, 15),
      educate: cap(o.educate_score, 15),
    };
    return {
      faceless,
      confidence: isNaN(Number(o.confidence)) ? 0.6 : Math.max(0, Math.min(1, Number(o.confidence))), // 0~1
      reason: (o.reason || "").slice(0, 200),
      genre: (o.genre || o.content_format || "").slice(0, 40),
      copyright_risk: o.copyright_risk || null,
      voice_type: o.voice_type || null,
      scores,
      refScore: scores.transfer + scores.monetize + scores.repeat + scores.feasible + scores.educate,
      series: !!o.series_repeated,
      benchmark: (o.benchmark || "").slice(0, 200),
      caution: (o.caution || "").slice(0, 200),
    };
  } catch (e) {
    console.log(`  분류 실패(${ch.title}): ${e.message}`);
    return null;
  }
}

async function main() {
  console.log(`[인비랩 수집 로봇] 시작 — 기준일: ${TODAY} (KST)`);

  // ========== 1. 한국 인기 영상 수집 (최대 100개) ==========
  const videos = [];
  let pageToken = "";
  for (let page = 0; page < 2; page++) {
    const data = await ytFetch("videos", {
      part: "snippet,statistics,contentDetails",
      chart: "mostPopular",
      regionCode: "KR",
      maxResults: "50",
      ...(pageToken ? { pageToken } : {}),
    });
    videos.push(...(data.items || []));
    pageToken = data.nextPageToken;
    if (!pageToken) break;
  }
  console.log(`인기 영상 ${videos.length}개 수집`);

  const trendingRows = [];
  for (let i = 0; i < videos.length; i++) {
    const v = videos[i];
    const dur = durationSec(v.contentDetails?.duration);
    // 3분 10초 이하 영상만 쇼츠 후보로 보고, 유튜브에 직접 확인
    let isShort = false;
    if (dur > 0 && dur <= 190) {
      const real = await checkRealShort(v.id);
      isShort = real === null ? dur <= 60 : real; // 확인 실패 시 60초 이하만 쇼츠로
    }
    trendingRows.push({
      video_id: v.id,
      date: TODAY,
      title: (v.snippet?.title || "").slice(0, 300),
      channel_id: v.snippet?.channelId || "",
      channel_title: v.snippet?.channelTitle || "",
      thumbnail: v.snippet?.thumbnails?.medium?.url || "",
      published_at: v.snippet?.publishedAt || null,
      view_count: Number(v.statistics?.viewCount || 0),
      like_count: Number(v.statistics?.likeCount || 0),
      is_short: isShort,
      rank: i + 1,
    });
  }
  console.log(`쇼츠 판별 완료: ${trendingRows.filter((r) => r.is_short).length}개가 진짜 쇼츠`);
  // 인기 영상 저장은 채널 구독자 수를 확인한 뒤(50만 미만만) 진행 → 아래 3단계 이후

  // ========== 1.5 유튜브에서 급상승 쇼츠 채널 발굴 (Phase 1) ==========
  const termRows = await loadSearchTerms();
  const disc = await discoverShortsChannels(termRows, 250);
  const discovered = disc.ids;
  const discoveredSet = new Set(discovered);

  // ========== 2. 수집 대상 채널 목록 만들기 ==========
  // 기존 등록 채널 + 오늘 인기 영상에 등장한 채널 + 새로 발굴한 쇼츠 채널
  const existing = await sbFetch("channels?select=id&is_active=eq.true&limit=1500");
  const idSet = new Set(existing.map((c) => c.id));
  trendingRows.forEach((r) => r.channel_id && idSet.add(r.channel_id));
  discovered.forEach((cid) => idSet.add(cid));
  const channelIds = [...idSet];
  console.log(`수집 대상 채널: ${channelIds.length}개 (발굴 신규 포함)`);

  // ========== 3. 채널 통계 일괄 조회 (50개씩) ==========
  const stats = [];
  for (const ids of chunk(channelIds, 50)) {
    const data = await ytFetch("channels", {
      part: "snippet,statistics",
      id: ids.join(","),
      maxResults: "50",
    });
    stats.push(...(data.items || []));
  }
  console.log(`채널 통계 ${stats.length}개 조회`);

  // 채널별 구독자 수 지도 (50만 이상 제외에 사용)
  const subsMap = new Map();
  for (const c of stats) subsMap.set(c.id, Number(c.statistics?.subscriberCount || 0));
  const isBig = (channelId) => (subsMap.get(channelId) || 0) >= MAX_SUBS;

  // 인기 영상 저장 — 대형(50만 이상) 채널 영상은 제외
  const trendingSmall = trendingRows.filter((r) => !isBig(r.channel_id));
  for (const part of chunk(trendingSmall, 100)) {
    await sbFetch("trending_videos?on_conflict=video_id,date", {
      method: "POST",
      body: part,
      prefer: "resolution=merge-duplicates",
    });
  }
  console.log(`인기 영상 저장 완료 (50만 미만 ${trendingSmall.length}개 / 전체 ${trendingRows.length}개)`);

  // ========== 4. 어제 기록 불러오기 (성장률 계산용) ==========
  const yRows = await sbFetch(
    `channel_snapshots?select=channel_id,view_count,subscriber_count&date=eq.${YESTERDAY}&limit=2000`
  );
  const yMap = new Map(yRows.map((r) => [r.channel_id, r]));

  // 구독자 하루 증가 '추정'용: 최근 14일 기록
  // (유튜브는 구독자 수를 반올림해 공개하므로 하루 변화가 0으로 보일 수 있음
  //  → 공개 숫자가 마지막으로 달랐던 날과 비교해 하루 평균 증가량을 계산)
  const histRows = await sbFetch(
    `channel_snapshots?select=channel_id,date,subscriber_count&date=gte.${kstDate(-14)}&order=date.desc&limit=20000`
  );
  const histMap = new Map();
  for (const h of histRows) {
    const a = histMap.get(h.channel_id) || [];
    a.push(h);
    histMap.set(h.channel_id, a);
  }

  // ========== 5. 오늘 기록 저장 + 채널 정보 갱신 ==========
  const snapRows = [];
  const chRows = [];
  for (const c of stats) {
    const subs = Number(c.statistics?.subscriberCount || 0);
    if (subs >= MAX_SUBS) continue; // 50만 이상 대형 채널은 저장하지 않음
    const views = Number(c.statistics?.viewCount || 0);
    const vids = Number(c.statistics?.videoCount || 0);
    snapRows.push({
      channel_id: c.id, date: TODAY,
      subscriber_count: subs, view_count: views, video_count: vids,
    });
    const y = yMap.get(c.id);
    // 구독자 하루 증가 추정: 공개 숫자가 마지막으로 달랐던 날과 비교해 하루 평균으로 환산
    let subsEst = null;
    for (const h of histMap.get(c.id) || []) {
      if (h.date >= TODAY) continue;
      if (Number(h.subscriber_count) !== subs) {
        const days = Math.max(1, Math.round((Date.parse(TODAY) - Date.parse(h.date)) / 86400000));
        const perDay = (subs - Number(h.subscriber_count)) / days;
        const abs = Math.abs(perDay);
        const unit = abs >= 1000 ? 100 : abs >= 100 ? 10 : 1; // 보기 좋은 단위로 반올림
        subsEst = Math.round(perDay / unit) * unit;
        break;
      }
    }
    chRows.push({
      id: c.id,
      title: (c.snippet?.title || "").slice(0, 200),
      handle: c.snippet?.customUrl || "",
      thumbnail: c.snippet?.thumbnails?.medium?.url || c.snippet?.thumbnails?.default?.url || "",
      country: c.snippet?.country || "",
      subscriber_count: subs, view_count: views, video_count: vids,
      daily_views: y ? views - Number(y.view_count) : null,
      daily_subs: y ? subs - Number(y.subscriber_count) : null,
      daily_subs_est: subsEst,
      stats_date: TODAY,
      is_active: true,
    });
  }
  // 순서 중요: 채널 명단을 먼저 등록한 뒤에 일일 기록을 저장해야 함
  for (const part of chunk(chRows, 200)) {
    await sbFetch("channels?on_conflict=id", {
      method: "POST", body: part, prefer: "resolution=merge-duplicates",
    });
  }
  for (const part of chunk(snapRows, 200)) {
    await sbFetch("channel_snapshots?on_conflict=channel_id,date", {
      method: "POST", body: part, prefer: "resolution=merge-duplicates",
    });
  }
  console.log("채널 정보/일일 기록 저장 완료");

  // ========== 5.5 안전장치: 오늘 통계가 안 잡힌 채널은 자동 강등 ==========
  // 유튜브에서 삭제·정지된 채널은 새 데이터가 안 잡혀 마지막 증가량이 얼어붙음
  // → 오늘 갱신 안 된 채널은 증가량을 0으로 만들어 순위 아래로 내려보냄
  try {
    await sbFetch(`channels?stats_date=lt.${TODAY}&is_active=eq.true`, {
      method: "PATCH",
      body: { daily_views: 0, daily_subs: 0 },
      prefer: "return=minimal",
    });
    // 7일 넘게 데이터가 안 잡히면 휴면 처리(수집·표시 대상에서 제외)
    await sbFetch(`channels?stats_date=lt.${kstDate(-7)}&is_active=eq.true`, {
      method: "PATCH",
      body: { is_active: false },
      prefer: "return=minimal",
    });
    console.log("안전장치: 오늘 데이터 누락 채널 증가량 0 처리 + 7일 무응답 채널 휴면 처리 완료");
  } catch (e) {
    console.log(`안전장치 처리 실패(다음 실행에서 재시도): ${e.message}`);
  }

  // ========== 5.6 성장 미달 자동 제외 (관리자 방침) ==========
  // AI 발굴 채널 중 하루 성장(조회수 증가 + 구독자 증가)이 합쳐서 100 미만이면
  // 수강생이 볼 가치가 없으므로 검토 대기에 올리지 않고 자동 제외.
  // (성장 데이터가 아직 없는 신규 발굴 채널은 다음 날 성장 확인 후 판단)
  try {
    const pendingAi = await sbFetch(
      `channels?select=id,daily_views,daily_subs&source=is.null&is_active=eq.true` +
      `&daily_views=not.is.null&daily_subs=not.is.null` +
      `&or=(admin_status.is.null,admin_status.eq.${encodeURIComponent("대기")})&limit=2000`
    );
    const lowGrowth = (pendingAi || [])
      .filter((c) => (Number(c.daily_views) || 0) + (Number(c.daily_subs) || 0) < 100)
      .map((c) => c.id);
    for (const ids of chunk(lowGrowth, 100)) {
      await sbFetch(`channels?id=in.(${ids.map((i) => `"${i}"`).join(",")})`, {
        method: "PATCH",
        body: { admin_status: "제외" },
        prefer: "return=minimal",
      });
    }
    if (lowGrowth.length) console.log(`성장 미달(하루 증가 합계 100 미만) 자동 제외: ${lowGrowth.length}개`);
  } catch (e) {
    console.log(`성장 미달 제외 처리 실패(다음 실행에서 재시도): ${e.message}`);
  }

  // 발견 경로 기록: 어떤 검색어·시간창이 이 채널을 찾았는지 (이미 기록된 채널은 유지)
  const srcGroups = new Map();
  for (const [cid, src] of disc.discoverySrc) {
    if (!discoveredSet.has(cid)) continue;
    const a = srcGroups.get(src) || []; a.push(cid); srcGroups.set(src, a);
  }
  for (const [src, ids] of srcGroups) {
    for (const part of chunk(ids, 80)) {
      try {
        await sbFetch(`channels?discovery_source=is.null&id=in.(${part.map((i) => `"${i}"`).join(",")})`, {
          method: "PATCH",
          body: { discovery_source: src },
          prefer: "return=minimal",
        });
      } catch { /* 기록 실패해도 수집은 계속 */ }
    }
  }

  // ========== 5.7 검색어 성적표 갱신 (Phase 1) ==========
  // 각 검색어가 발굴한 채널들이 이후 AI 합격/관리자 승인으로 이어졌는지 집계 → 점수화
  // → 다음 실행 때 점수 높은 검색어에 검색 자원(시간창 3개)을 배분
  try {
    const dsRows = await sbFetch(
      `channels?select=discovery_source,ai_ref_class,admin_status&discovery_source=like.${encodeURIComponent("검색:")}*&limit=3000`
    );
    const stat = new Map();
    for (const r of dsRows || []) {
      const m = /^검색:(.+)\|/.exec(r.discovery_source || "");
      if (!m) continue;
      const t = m[1];
      const s = stat.get(t) || { discovered: 0, passed: 0, approved: 0 };
      s.discovered++;
      if (["검증후보", "급등후보", "검토필요"].includes(r.ai_ref_class)) s.passed++;
      if (r.admin_status === "승인") s.approved++;
      stat.set(t, s);
    }
    for (const t of new Set([...disc.ranTerms, ...stat.keys()])) {
      const s = stat.get(t) || { discovered: 0, passed: 0, approved: 0 };
      const score = s.approved * 3 + s.passed + s.discovered * 0.1; // 승인이 가장 큰 가산점
      const body = { discovered: s.discovered, passed: s.passed, approved: s.approved, score };
      if (disc.ranTerms.has(t)) body.last_run = TODAY;
      await sbFetch(`search_terms?term=eq.${encodeURIComponent(t)}`, {
        method: "PATCH", body, prefer: "return=minimal",
      });
    }
    console.log(`검색어 성적표 갱신 완료 (${stat.size}개 검색어 집계)`);
  } catch (e) {
    console.log(`검색어 성적표 갱신 실패(다음 실행에서 재시도): ${e.message}`);
  }

  // ========== 5.8 튜브랩에서 새 검색어 자동 추출 (Phase 1) ==========
  // 검색어가 28개 미만이면, 튜브랩 상위 채널 이름·장르에서 새 검색어 후보를 AI로 뽑아 추가
  const GEM_KEY_EXTRACT = process.env.GEMINI_API_KEY;
  if (GEM_KEY_EXTRACT && termRows.length < 28) {
    try {
      const tl = await sbFetch(`channels?select=title,ai_genre&source=eq.tubelab&order=daily_views.desc.nullslast&limit=40`);
      const prompt =
        `아래는 지금 성장 중인 한국 무출연 쇼츠 채널 목록이야. 이런 채널을 유튜브 검색으로 발굴할 때 쓸 새로운 한국어 검색어 5개를 제안해.\n` +
        `기존 검색어와 겹치지 않게: ${termRows.map((t) => t.term).join(", ")}\n\n채널 목록:\n` +
        (tl || []).map((c) => `- ${c.title}${c.ai_genre ? ` (${c.ai_genre})` : ""}`).join("\n") +
        `\n\n반드시 JSON 배열로만 반환: ["검색어1","검색어2","검색어3","검색어4","검색어5"]`;
      const res = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key=${GEM_KEY_EXTRACT}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { responseMimeType: "application/json", temperature: 0.7 } }),
        }
      );
      const data = await res.json();
      const arr = JSON.parse(data.candidates?.[0]?.content?.parts?.[0]?.text || "[]");
      const news = (Array.isArray(arr) ? arr : [])
        .map((s) => String(s).trim().slice(0, 40))
        .filter((s) => s && !termRows.some((t) => t.term === s))
        .slice(0, 5);
      if (news.length) {
        await sbFetch("search_terms?on_conflict=term", {
          method: "POST",
          body: news.map((t) => ({ term: t, source: "자동", enabled: true })),
          prefer: "resolution=merge-duplicates",
        });
        console.log(`새 검색어 ${news.length}개 추가: ${news.join(", ")}`);
      }
    } catch (e) {
      console.log(`새 검색어 추출 실패(다음 실행에서 재시도): ${e.message}`);
    }
  }

  // ========== 6. 채널별 최근 영상 수집 (쇼츠 랭킹의 재료) ==========
  // 구독자 많은 순으로 최대 400개 채널의 최근 업로드를 확인
  // 새로 발굴한 쇼츠 채널을 우선 처리하고, 그 다음 구독자 많은 순
  const targetChannels = chRows
    .slice()
    .sort((a, b) => {
      const da = discoveredSet.has(a.id) ? 1 : 0;
      const db = discoveredSet.has(b.id) ? 1 : 0;
      if (da !== db) return db - da;
      return b.subscriber_count - a.subscriber_count;
    })
    .slice(0, 600);

  // 이미 판별해 둔 영상은 다시 검사하지 않음
  const known = await sbFetch("channel_videos?select=video_id,is_short&limit=20000");
  const knownMap = new Map(known.map((r) => [r.video_id, r.is_short]));

  const cutoff = Date.now() - 30 * 86400 * 1000; // 최근 30일 영상만
  const candidateIds = [];
  for (const ch of targetChannels) {
    const uploadsPlaylist = "UU" + ch.id.slice(2); // 채널의 업로드 목록 ID
    try {
      const pl = await ytFetch("playlistItems", {
        part: "contentDetails",
        playlistId: uploadsPlaylist,
        maxResults: "10",
      });
      for (const it of pl.items || []) {
        const vid = it.contentDetails?.videoId;
        const pub = new Date(it.contentDetails?.videoPublishedAt || 0).getTime();
        if (vid && pub > cutoff) candidateIds.push(vid);
      }
    } catch { /* 업로드 목록이 없는 채널은 건너뜀 */ }
  }
  console.log(`채널 영상 후보: ${candidateIds.length}개`);

  const vstats = [];
  for (const ids of chunk(candidateIds, 50)) {
    const data = await ytFetch("videos", {
      part: "snippet,statistics,contentDetails",
      id: ids.join(","),
      maxResults: "50",
    });
    vstats.push(...(data.items || []));
  }

  const yv = await sbFetch(
    `video_snapshots?select=video_id,view_count&date=eq.${YESTERDAY}&limit=20000`
  );
  const yvMap = new Map(yv.map((r) => [r.video_id, Number(r.view_count)]));

  const videoRows = [];
  const vSnapRows = [];
  let newlyChecked = 0;
  for (const v of vstats) {
    const dur = durationSec(v.contentDetails?.duration);
    let isShort;
    if (knownMap.has(v.id)) {
      isShort = knownMap.get(v.id);
    } else {
      // 재생시간 기준 빠른 판정(180초 이하 = 쇼츠 후보). 2024-10 이후 쇼츠 최대 3분 반영
      isShort = dur > 0 && dur <= 180;
      newlyChecked++;
    }
    const views = Number(v.statistics?.viewCount || 0);
    const y = yvMap.get(v.id);
    videoRows.push({
      video_id: v.id,
      channel_id: v.snippet?.channelId || null,
      title: (v.snippet?.title || "").slice(0, 300),
      thumbnail: v.snippet?.thumbnails?.medium?.url || "",
      published_at: v.snippet?.publishedAt || null,
      duration_sec: dur,
      is_short: isShort,
      view_count: views,
      like_count: Number(v.statistics?.likeCount || 0),
      daily_views: y != null ? views - y : null,
      stats_date: TODAY,
    });
    vSnapRows.push({ video_id: v.id, date: TODAY, view_count: views });
  }
  for (const part of chunk(videoRows, 200)) {
    await sbFetch("channel_videos?on_conflict=video_id", {
      method: "POST", body: part, prefer: "resolution=merge-duplicates",
    });
  }
  for (const part of chunk(vSnapRows, 200)) {
    await sbFetch("video_snapshots?on_conflict=video_id,date", {
      method: "POST", body: part, prefer: "resolution=merge-duplicates",
    });
  }
  const shortCount = videoRows.filter((r) => r.is_short).length;
  console.log(`채널 영상 ${videoRows.length}개 저장 (쇼츠 ${shortCount}개, 새로 판별 ${newlyChecked}개)`);

  // ========== 7. AI 무출연(편집형) '쇼츠' 검수 — 튜브랩이 아닌 채널만 ==========
  //   (1) 재생시간 기준 '쇼츠 위주 채널'만 통과(기계 규칙)
  //   (2) 통과분만 튜브랩 정답 예시(few-shot)로 AI가 무출연 판정
  const GEM_KEY = process.env.GEMINI_API_KEY;
  if (GEM_KEY) {
    // 튜브랩 정답 예시(few-shot): "무출연 편집형 쇼츠"의 기준을 AI에게 학습시킴
    const goldRows = await sbFetch(
      `channels?select=title,ai_genre&source=eq.tubelab&order=daily_views.desc.nullslast&limit=18`
    );
    const gold = (goldRows || []).map((g) => `- ${g.title}${g.ai_genre ? ` (${g.ai_genre})` : ""}`).join("\n");

    const AI_LIMIT = 400; // Gemini로 판정할 쇼츠 채널 최대 수 (하루) — Pro 250회 초과분은 Flash가 처리
    let gemModel = "gemini-pro-latest"; // Pro 한도 도달 시 Flash로 자동 전환
    // 튜브랩 채널은 이미 검증됨 → 제외. 새로 발굴/트렌딩된 채널(source 없음)만 검수. 신규 우선.
    const unclassified = await sbFetch(
      `channels?select=id,title,admin_status&classified_at=is.null&is_active=eq.true&subscriber_count=lt.${MAX_SUBS}&source=is.null&order=added_at.desc&limit=600`
    );
    console.log(`AI 검수 후보(미분류·비튜브랩): ${unclassified.length}개`);
    let aiDone = 0, facelessN = 0, notShorts = 0, noData = 0, rejectN = 0;
    for (const ch of unclassified) {
      if (aiDone >= AI_LIMIT) break;
      const vids = await sbFetch(
        `channel_videos?select=title,thumbnail,is_short,published_at&channel_id=eq.${ch.id}&order=published_at.desc&limit=12`
      );
      if (!vids || vids.length === 0) { noData++; continue; } // 영상 없으면 분류 보류(다음 기회)
      // (1) 완화된 기계 규칙: 최근 영상 중 쇼츠가 3개 미만이면 쇼츠 채널로 보기 어려움 → 제외
      const shortsCount = vids.filter((v) => v.is_short).length;
      if (shortsCount < 3) {
        await sbFetch(`channels?id=eq.${ch.id}`, {
          method: "PATCH",
          body: { ai_faceless: false, ai_confidence: "하", ai_reason: "쇼츠 영상이 충분치 않음(롱폼 위주)", ai_genre: null, ai_ref_class: "부적합", classified_at: new Date().toISOString() },
          prefer: "return=minimal",
        });
        notShorts++;
        continue;
      }
      // (2) 쇼츠 채널만 AI(튜브랩 정답 + 오답 예시)로 '본인' 무출연 판정
      let r = await classifyChannel(ch, vids.slice(0, 5), GEM_KEY, gold, gemModel);
      if (r && r.quota && gemModel === "gemini-pro-latest") {
        console.log("  ⚠ Pro 일일 한도 도달 → Flash 모델로 전환해 계속 진행");
        gemModel = "gemini-flash-latest";
        r = await classifyChannel(ch, vids.slice(0, 5), GEM_KEY, gold, gemModel);
      }
      if (!r || r.quota) continue;
      const conf = r.confidence;                          // 0~1
      const confText = conf >= 0.8 ? "상" : conf >= 0.5 ? "중" : "하";
      const faceless = conf < 0.5 ? false : r.faceless;   // 신뢰도 0.5 미만은 제외
      // 레퍼런스 등급 자동 분류 (승인은 항상 관리자 몫 — 자동 확정 없음)
      //   검증후보: 적합도 80+ & 신뢰도 0.85+ & 채널에 같은 포맷 반복 구조 있음
      //   급등후보: 적합도 80+ & 신뢰도 0.85+ 이지만 반복 성공은 미검증 (작은 채널 환영)
      //   검토필요: 그 사이 애매한 구간 → 관리자 판단
      //   부적합: 출연형이거나, 적합도 70 미만이거나, 성공원인 이식성 18 미만 → 자동 '제외' 처리(복구 가능)
      let klass;
      if (!faceless) klass = "부적합";
      else if (r.refScore < 70 || r.scores.transfer < 18) klass = "부적합";
      else if (r.refScore >= 80 && conf >= 0.85) klass = r.series ? "검증후보" : "급등후보";
      else klass = "검토필요";
      const body = {
        ai_faceless: faceless,
        ai_confidence: confText,
        ai_reason: r.reason,
        ai_genre: r.genre,
        ai_copyright_risk: r.copyright_risk,
        ai_voice_type: r.voice_type,
        ai_ref_score: r.refScore,
        ai_ref_class: klass,
        ai_scores: r.scores,
        ai_benchmark: r.benchmark,
        ai_caution: r.caution,
        ai_series: r.series,
        classified_at: new Date().toISOString(),
      };
      // 부적합(무출연이긴 함)은 검토대기 목록을 어지럽히지 않도록 자동 '제외'로 이동 (관리자가 되돌리기 가능)
      if (klass === "부적합" && faceless && (!ch.admin_status || ch.admin_status === "대기")) {
        body.admin_status = "제외";
        rejectN++;
      }
      await sbFetch(`channels?id=eq.${ch.id}`, {
        method: "PATCH",
        body,
        prefer: "return=minimal",
      });
      aiDone++;
      if (faceless) facelessN++;
    }
    console.log(`AI 검수 완료: 쇼츠 채널 ${aiDone}개 판정(무출연 ${facelessN}개, 점수미달 자동제외 ${rejectN}개), 쇼츠아님 제외 ${notShorts}개, 영상없음 보류 ${noData}개`);

    // ========== 7.5 영상 단위 레퍼런스 분석 (Phase 2) ==========
    // 승인 채널 + AI 무출연 합격 채널의 쇼츠 중 조회수 상위 영상을 골라
    // 훅·구성·배울 점·새 소재 아이디어를 영상 1개 단위로 분석 → 레퍼런스 영상 게시판의 재료
    const VIDEO_AI_LIMIT = 60; // 하루 최대 분석 영상 수
    try {
      const okCh = await sbFetch(
        `channels?select=id,title,ai_genre&or=(admin_status.eq.${encodeURIComponent("승인")},ai_faceless.eq.true)&is_active=eq.true&limit=1000`
      );
      const chInfo = new Map((okCh || []).map((c) => [c.id, c]));
      const okIds = [...chInfo.keys()];
      let cand = [];
      for (const part of chunk(okIds, 60)) {
        const rows = await sbFetch(
          `channel_videos?select=video_id,channel_id,title,thumbnail,view_count&is_short=eq.true&analyzed_at=is.null&channel_id=in.(${part.map((i) => `"${i}"`).join(",")})&order=view_count.desc&limit=150`
        );
        cand.push(...(rows || []));
      }
      cand.sort((a, b) => Number(b.view_count) - Number(a.view_count));
      cand = cand.slice(0, VIDEO_AI_LIMIT);
      let vDone = 0;
      for (const v of cand) {
        let r = await analyzeVideo(v, chInfo.get(v.channel_id) || {}, GEM_KEY, gemModel);
        if (r && r.quota && gemModel === "gemini-pro-latest") {
          console.log("  ⚠ Pro 일일 한도 도달 → Flash 모델로 전환해 계속 진행");
          gemModel = "gemini-flash-latest";
          r = await analyzeVideo(v, chInfo.get(v.channel_id) || {}, GEM_KEY, gemModel);
        }
        if (!r || r.quota) continue;
        await sbFetch(`channel_videos?video_id=eq.${v.video_id}`, {
          method: "PATCH",
          body: {
            ai_fit: r.fit, ai_hook: r.hook, ai_structure: r.structure,
            ai_benchmark: r.benchmark, ai_caution: r.caution, ai_ideas: r.ideas,
            analyzed_at: new Date().toISOString(),
          },
          prefer: "return=minimal",
        });
        vDone++;
      }
      console.log(`영상 레퍼런스 분석 완료: ${vDone}/${cand.length}개`);
    } catch (e) {
      console.log(`영상 레퍼런스 분석 실패(다음 실행에서 재시도): ${e.message}`);
    }
  } else {
    console.log("GEMINI_API_KEY 없음 — AI 분류 단계 건너뜀");
  }

  console.log("[인비랩 수집 로봇] 정상 종료 ✅");
}

main().catch((e) => {
  console.error("수집 실패:", e.message);
  process.exit(1);
});
