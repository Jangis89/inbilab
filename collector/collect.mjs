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

// ---------- 유튜브에서 "최근 급상승 쇼츠" 채널 발굴 (search API) ----------
// 무출연 편집형 쇼츠가 많이 쓰는 주제어로 최근 14일 인기 쇼츠를 검색 → 채널 후보 수집
const SHORTS_SEEDS = [
  "영화 요약 쇼츠", "드라마 요약", "명장면 모음", "레전드 모음", "랭킹 top",
  "이슈 정리", "해외반응", "동물 모음", "게임 하이라이트", "애니 썰",
  "역사 이슈", "실화 사건", "썰 애니메이션", "정보 꿀팁 쇼츠", "충격 모음", "스포츠 명장면",
];
async function discoverShortsChannels(maxChannels = 220) {
  const publishedAfter = new Date(Date.now() - 14 * 86400 * 1000).toISOString();
  const hits = new Map(); // channelId -> 검색 노출 횟수(여러 검색어에 걸릴수록 관련성 높음)
  for (const q of SHORTS_SEEDS) {
    try {
      const data = await ytFetch("search", {
        part: "snippet",
        type: "video",
        videoDuration: "short",       // 4분 미만(쇼츠 후보) — 이후 재생시간으로 진짜 쇼츠 확정
        order: "viewCount",           // 조회수 높은 순 = 지금 뜨는 영상
        regionCode: "KR",
        relevanceLanguage: "ko",
        publishedAfter,               // 최근 14일 = 현재 성장 중
        maxResults: "50",
        q,
      });
      for (const it of data.items || []) {
        const cid = it.snippet?.channelId;
        if (cid) hits.set(cid, (hits.get(cid) || 0) + 1);
      }
    } catch (e) {
      console.log(`  쇼츠 발굴 검색 실패(${q}): ${e.message}`);
    }
  }
  const ranked = [...hits.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id);
  console.log(`쇼츠 발굴: 후보 채널 ${ranked.length}개 (검색어 ${SHORTS_SEEDS.length}개) → 상위 ${Math.min(maxChannels, ranked.length)}개 사용`);
  return ranked.slice(0, maxChannels);
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

// ---------- Gemini로 "무출연 편집형" 여부 판정 ----------
async function classifyChannel(ch, vids, gemKey, gold = "", approvedGold = "", adminBad = "") {
  const titles = vids.map((v, i) => `${i + 1}. ${v.title}`).join("\n");
  const goldBlock = gold
    ? `[정답 예시 — 튜브랩이 엄선한 무출연 편집형 쇼츠 채널]\n${gold}\n\n`
    : "";
  // 관리자(사람)의 승인/제외 클릭이 가장 강한 학습 신호
  const approvedBlock = approvedGold
    ? `[정답 예시 — 관리자가 직접 '승인'한 채널. 이런 채널을 최우선으로 찾아라]\n${approvedGold}\n\n`
    : "";
  const adminBadBlock = adminBad
    ? `[오답 예시 — 관리자가 직접 '제외'한 채널. 이와 비슷한 채널은 부적합]\n${adminBad}\n\n`
    : "";
  const negBlock =
    `[오답 예시 — 아래 유형은 부적합(제작자 본인이 출연해야 만들 수 있거나 쇼츠가 아님)]\n` +
    `- 세로형이지만 본인 일상/브이로그\n` +
    `- 얼굴은 안 나와도 '제작자 본인 목소리 해설'이 핵심인 채널\n` +
    `- 3분 이하지만 가로형 롱폼\n` +
    `- 먹방·리액션·페이스캠 게임(본인 직접 출연)\n` +
    `- 영화/방송을 거의 변형 없이 그대로 재업로드\n` +
    `- 음악만 바꾼 반복 영상\n\n`;
  const prompt =
    `너는 유튜브 '쇼츠' 채널 분석가야. 이 채널이 "제작자가 자기 얼굴과 목소리를 노출하지 않고 편집으로 만드는 무출연 쇼츠 채널"인지 판정해.\n` +
    `핵심 규칙: 영상 안에 배우·행인 등 '타인'의 얼굴/목소리가 나오는 건 괜찮다. ` +
    `오직 '제작자 본인'이 자기 얼굴이나 목소리로 출연해야만 만들 수 있는 채널만 제외한다. ` +
    `(예: 영화요약 쇼츠는 배우 얼굴이 나와도 제작자는 무출연이므로 적합)\n\n` +
    approvedBlock + goldBlock + negBlock + adminBadBlock +
    `채널명: ${ch.title}\n최근 영상 제목:\n${titles}\n\n첨부된 썸네일 이미지도 함께 참고해.\n\n` +
    `반드시 아래 JSON만 반환:\n` +
    `{"creator_face":"none|brief|main","other_faces":"none|some|frequent","creator_voice":"none|main",` +
    `"voice_type":"none|ai_tts|original|music","content_format":"movie_recap|drama_recap|ranking|animation|game_edit|issue_tts|animal|sports|music|other",` +
    `"reproducible_beginner":true,"copyright_risk":"low|mid|high","confidence":0.0,"genre":"한국어 장르","reason":"한 줄 근거"}`;
  const parts = [{ text: prompt }];
  for (const v of vids.slice(0, 4)) {
    if (!v.thumbnail) continue;
    const b64 = await fetchImageBase64(v.thumbnail);
    if (b64) parts.push({ inline_data: { mime_type: "image/jpeg", data: b64 } });
  }
  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-pro-latest:generateContent?key=${gemKey}`,
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
      console.log(`  Gemini 오류(${ch.title}): ${data.error.message}`);
      return null;
    }
    const txt = data.candidates?.[0]?.content?.parts?.[0]?.text || "";
    const o = JSON.parse(txt);
    // 무출연 최종 판정 = 제작자 본인 얼굴이 none/brief 이고, 본인 목소리가 none (타인 얼굴/목소리는 무관)
    const faceless = (o.creator_face === "none" || o.creator_face === "brief") && o.creator_voice === "none";
    return {
      faceless,
      confidence: typeof o.confidence === "number" ? o.confidence : 0.6, // 0~1
      reason: (o.reason || "").slice(0, 200),
      genre: (o.genre || o.content_format || "").slice(0, 40),
      copyright_risk: o.copyright_risk || null,
      voice_type: o.voice_type || null,
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

  // ========== 1.5 유튜브에서 급상승 쇼츠 채널 발굴 ==========
  const discovered = await discoverShortsChannels(220);
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
    chRows.push({
      id: c.id,
      title: (c.snippet?.title || "").slice(0, 200),
      handle: c.snippet?.customUrl || "",
      thumbnail: c.snippet?.thumbnails?.medium?.url || c.snippet?.thumbnails?.default?.url || "",
      country: c.snippet?.country || "",
      subscriber_count: subs, view_count: views, video_count: vids,
      daily_views: y ? views - Number(y.view_count) : null,
      daily_subs: y ? subs - Number(y.subscriber_count) : null,
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

    // 관리자 승인/제외 클릭을 학습 데이터로 활용 — 승인=정답, 제외=오답 (설계서 Phase 0-4)
    const okRows = await sbFetch(
      `channels?select=title,ai_genre&admin_status=eq.${encodeURIComponent("승인")}&order=daily_views.desc.nullslast&limit=25`
    );
    const approvedGold = (okRows || []).map((g) => `- ${g.title}${g.ai_genre ? ` (${g.ai_genre})` : ""}`).join("\n");
    const badRows = await sbFetch(
      `channels?select=title,ai_genre&admin_status=eq.${encodeURIComponent("제외")}&source=is.null&order=added_at.desc&limit=20`
    );
    const adminBad = (badRows || []).map((g) => `- ${g.title}${g.ai_genre ? ` (${g.ai_genre})` : ""}`).join("\n");
    console.log(`학습 예시: 튜브랩 ${goldRows?.length || 0} · 관리자 승인 ${okRows?.length || 0} · 관리자 제외 ${badRows?.length || 0}`);

    const AI_LIMIT = 200; // Gemini로 판정할 쇼츠 채널 최대 수 (하루)
    // 튜브랩 채널은 이미 검증됨 → 제외. 새로 발굴/트렌딩된 채널(source 없음)만 검수. 신규 우선.
    const unclassified = await sbFetch(
      `channels?select=id,title&classified_at=is.null&is_active=eq.true&subscriber_count=lt.${MAX_SUBS}&source=is.null&order=added_at.desc&limit=600`
    );
    console.log(`AI 검수 후보(미분류·비튜브랩): ${unclassified.length}개`);
    let aiDone = 0, facelessN = 0, notShorts = 0, noData = 0;
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
          body: { ai_faceless: false, ai_confidence: "하", ai_reason: "쇼츠 영상이 충분치 않음(롱폼 위주)", ai_genre: null, classified_at: new Date().toISOString() },
          prefer: "return=minimal",
        });
        notShorts++;
        continue;
      }
      // (2) 쇼츠 채널만 AI(튜브랩 정답 + 오답 예시)로 '본인' 무출연 판정
      const r = await classifyChannel(ch, vids.slice(0, 5), GEM_KEY, gold, approvedGold, adminBad);
      if (!r) continue;
      const conf = r.confidence;                          // 0~1
      const confText = conf >= 0.8 ? "상" : conf >= 0.5 ? "중" : "하";
      const faceless = conf < 0.5 ? false : r.faceless;   // 신뢰도 0.5 미만은 제외
      await sbFetch(`channels?id=eq.${ch.id}`, {
        method: "PATCH",
        body: {
          ai_faceless: faceless,
          ai_confidence: confText,
          ai_reason: r.reason,
          ai_genre: r.genre,
          ai_copyright_risk: r.copyright_risk,
          ai_voice_type: r.voice_type,
          classified_at: new Date().toISOString(),
        },
        prefer: "return=minimal",
      });
      aiDone++;
      if (faceless) facelessN++;
    }
    console.log(`AI 검수 완료: 쇼츠 채널 ${aiDone}개 판정(무출연 ${facelessN}개), 쇼츠아님 제외 ${notShorts}개, 영상없음 보류 ${noData}개`);
  } else {
    console.log("GEMINI_API_KEY 없음 — AI 분류 단계 건너뜀");
  }

  // ========== 8. 카테고리 분류 (15개 고정 목록) ==========
  //   (1) ai_genre 키워드 규칙으로 1차 매핑 (비용 0)
  //   (2) 규칙으로 못 정한 채널만 Gemini가 15개 중 객관식으로 선택
  const CATEGORIES = [
    "영화·드라마 요약", "연예 이슈", "지식·역사·교양", "스포츠", "정치·시사",
    "국뽕·해외반응", "방송·예능", "게임", "유머·밈·바이럴", "동물·펫",
    "음악", "썰·사연·애니툰", "푸드", "랭킹·순위 정보", "기타",
  ];
  const CAT_RULES = [
    ["게임", /게임|로블록스|LOL|롤 |e스포츠|이스포츠|매드무비|버츄얼/i],
    ["국뽕·해외반응", /국뽕|해외\s?반응|해외\s?이슈|해외\s?바이럴/],
    ["스포츠", /스포츠|축구|격투|무술|야구|중계|하이라이트/],
    ["영화·드라마 요약", /영화|드라마/],
    ["방송·예능", /방송|예능|성우|엔터/],
    ["연예 이슈", /연예|아이돌|트로트/],
    ["음악", /음악|감성/],
    ["동물·펫", /동물|반려|펫|힐링/],
    ["유머·밈·바이럴", /유머|밈|짜집기|짜깁기|바이럴|공감/],
    ["썰·사연·애니툰", /썰|사연|애니|툰|서브컬처/],
    ["랭킹·순위 정보", /랭킹|순위|인포그래픽|모음/],
    ["정치·시사", /정치|시사|사회|뉴스|국방|사건/],
    ["지식·역사·교양", /역사|지식|교양|인문|상식|미스터리|교육|어학|건강|운세|재테크|부동산|경제|정보|산업|실화|스토리텔링/],
    ["푸드", /푸드|음식|먹|쇼핑|공예/],
  ];
  function catFromGenre(g) {
    if (!g || g === "기타/혼합" || g === "종합/기타") return null;
    for (const [name, re] of CAT_RULES) if (re.test(g)) return name;
    return null;
  }
  // (1) 규칙 매핑
  const needCat = await sbFetch(`channels?select=id,title,ai_genre&category=is.null&limit=1000`);
  console.log(`카테고리 미지정: ${needCat.length}개`);
  let ruleDone = 0;
  const forGemini = [];
  for (const ch of needCat) {
    const cat = catFromGenre(ch.ai_genre);
    if (cat) {
      await sbFetch(`channels?id=eq.${ch.id}`, { method: "PATCH", body: { category: cat }, prefer: "return=minimal" });
      ruleDone++;
    } else {
      forGemini.push(ch);
    }
  }
  // '기타'로 분류된 채널도 매일 재분류 시도 — 영상이 쌓여 판단 근거가 생기면 제 카테고리로 이동
  const etcRows = await sbFetch(
    `channels?select=id,title,ai_genre&category=eq.${encodeURIComponent("기타")}&limit=200`
  );
  for (const ch of etcRows || []) forGemini.push(ch);
  console.log(`카테고리 규칙 매핑: ${ruleDone}개 완료, Gemini 판정 대상 ${forGemini.length}개 (기타 재시도 ${(etcRows || []).length}개 포함)`);
  // (2) Gemini 객관식 분류 (15개씩 묶어서)
  if (GEM_KEY && forGemini.length > 0) {
    let gemDone = 0;
    for (let i = 0; i < forGemini.length; i += 15) {
      const batch = forGemini.slice(i, i + 15);
      // 각 채널의 최근 영상 제목 3개를 함께 제공해 판단 근거 강화
      const lines = [];
      for (const ch of batch) {
        const vids = await sbFetch(`channel_videos?select=title&channel_id=eq.${ch.id}&order=published_at.desc&limit=3`);
        const vt = (vids || []).map((v) => v.title).join(" / ");
        lines.push(`${ch.id} | 채널명: ${ch.title}${vt ? ` | 최근영상: ${vt}` : ""}`);
      }
      const prompt =
        `아래 유튜브 채널들을 반드시 다음 15개 카테고리 중 하나로만 분류해.\n` +
        `카테고리 목록: ${CATEGORIES.join(", ")}\n` +
        `애매하면 "기타"를 선택해. 반드시 JSON 배열만 반환: [{"id":"...","category":"..."}]\n\n` +
        lines.join("\n");
      try {
        const res = await fetch(
          `https://generativelanguage.googleapis.com/v1beta/models/gemini-pro-latest:generateContent?key=${GEM_KEY}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              contents: [{ parts: [{ text: prompt }] }],
              generationConfig: { responseMimeType: "application/json", temperature: 0.1 },
            }),
          }
        );
        const data = await res.json();
        if (data.error) { console.log(`  카테고리 Gemini 오류: ${data.error.message}`); continue; }
        const arr = JSON.parse(data.candidates?.[0]?.content?.parts?.[0]?.text || "[]");
        for (const item of arr) {
          if (!item?.id || !CATEGORIES.includes(item.category)) continue;
          await sbFetch(`channels?id=eq.${encodeURIComponent(item.id)}`, {
            method: "PATCH", body: { category: item.category }, prefer: "return=minimal",
          });
          gemDone++;
        }
      } catch (e) {
        console.log(`  카테고리 배치 실패: ${e.message}`);
      }
    }
    console.log(`카테고리 Gemini 분류: ${gemDone}개 완료`);
  }

  // ========== 9. 오래된 일일 기록 자동 청소 (90일 보관) ==========
  //   급상승 계산엔 최근 기록만 필요 → 90일 지난 스냅샷/인기영상 기록 삭제로 저장 용량 관리
  try {
    const cutoff = new Date(Date.now() - 90 * 24 * 3600 * 1000).toISOString().slice(0, 10);
    await sbFetch(`video_snapshots?date=lt.${cutoff}`, { method: "DELETE", prefer: "return=minimal" });
    await sbFetch(`trending_videos?date=lt.${cutoff}`, { method: "DELETE", prefer: "return=minimal" });
    console.log(`일일 기록 청소 완료 (${cutoff} 이전 기록 삭제, 90일 보관)`);
  } catch (e) {
    console.log(`일일 기록 청소 오류(다음 실행에 재시도): ${e.message}`);
  }

  console.log("[인비랩 수집 로봇] 정상 종료 ✅");
}

main().catch((e) => {
  console.error("수집 실패:", e.message);
  process.exit(1);
});
