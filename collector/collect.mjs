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
async function classifyChannel(ch, vids, gemKey, gold = "") {
  const titles = vids.map((v, i) => `${i + 1}. ${v.title}`).join("\n");
  const goldBlock = gold
    ? `아래는 우리가 찾는 '정답' 유형이야 — 튜브랩이 엄선한 무출연 편집형 쇼츠 채널 예시:\n${gold}\n\n`
    : "";
  const prompt =
    `너는 유튜브 쇼츠 채널 분류 전문가야. 대상은 "쇼츠(60초 안팎 세로영상) 위주" 채널이고, 그중 ` +
    `"본인 얼굴·목소리 출연 없이 영상 편집으로만 운영되는 무출연 편집형"인지 판정해줘.\n\n` +
    goldBlock +
    `채널명: ${ch.title}\n최근 영상 제목:\n${titles}\n\n` +
    `첨부된 썸네일 이미지도 함께 참고해. 판정 기준:\n` +
    `- 편집형(무출연, faceless=true): 영화/드라마 요약·리뷰, 명장면·짤 편집, 랭킹/TOP, 이슈·정보 나레이션(TTS), 게임화면 편집, 애니메이션, 동물짤/해외반응 등. ` +
    `얼굴이 보여도 "남의 영상(영화·방송·경기)을 편집"한 것이면 편집형이다.\n` +
    `- 출연형(제외, faceless=false): 본인 얼굴/목소리로 하는 브이로그, 페이스캠 게임방송, 직접 말하는 리뷰어, 직접 출연 먹방 등.\n\n` +
    `반드시 아래 JSON 형식으로만 답해:\n` +
    `{"faceless": true 또는 false, "confidence": "상"|"중"|"하", "reason": "한 줄 근거", "genre": "추정 장르(영화요약/랭킹/명장면/이슈정보/게임/애니/동물/해외반응/기타)"}`;
  const parts = [{ text: prompt }];
  for (const v of vids.slice(0, 4)) {
    if (!v.thumbnail) continue;
    const b64 = await fetchImageBase64(v.thumbnail);
    if (b64) parts.push({ inline_data: { mime_type: "image/jpeg", data: b64 } });
  }
  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key=${gemKey}`,
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
    const obj = JSON.parse(txt);
    return {
      faceless: !!obj.faceless,
      confidence: obj.confidence || "중",
      reason: (obj.reason || "").slice(0, 200),
      genre: (obj.genre || "").slice(0, 40),
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
    } else if (dur === 0 || dur > 190) {
      isShort = false;
    } else {
      const real = await checkRealShort(v.id);
      newlyChecked++;
      isShort = real === null ? dur <= 60 : real;
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
      // (1) 기계 규칙: 최근 영상의 쇼츠 비율 60% 미만이면 쇼츠 채널 아님 → 제외
      const shortRatio = vids.filter((v) => v.is_short).length / vids.length;
      if (shortRatio < 0.6) {
        await sbFetch(`channels?id=eq.${ch.id}`, {
          method: "PATCH",
          body: { ai_faceless: false, ai_confidence: "하", ai_reason: "쇼츠 위주 채널이 아님(롱폼 비중 높음)", ai_genre: null, classified_at: new Date().toISOString() },
          prefer: "return=minimal",
        });
        notShorts++;
        continue;
      }
      // (2) 쇼츠 채널만 AI(튜브랩 예시 기반)로 무출연 판정
      const result = await classifyChannel(ch, vids.slice(0, 5), GEM_KEY, gold);
      if (!result) continue;
      await sbFetch(`channels?id=eq.${ch.id}`, {
        method: "PATCH",
        body: {
          ai_faceless: result.faceless,
          ai_confidence: result.confidence,
          ai_reason: result.reason,
          ai_genre: result.genre,
          classified_at: new Date().toISOString(),
        },
        prefer: "return=minimal",
      });
      aiDone++;
      if (result.faceless) facelessN++;
    }
    console.log(`AI 검수 완료: 쇼츠 채널 ${aiDone}개 판정(무출연 ${facelessN}개), 쇼츠아님 제외 ${notShorts}개, 영상없음 보류 ${noData}개`);
  } else {
    console.log("GEMINI_API_KEY 없음 — AI 분류 단계 건너뜀");
  }

  console.log("[인비랩 수집 로봇] 정상 종료 ✅");
}

main().catch((e) => {
  console.error("수집 실패:", e.message);
  process.exit(1);
});
