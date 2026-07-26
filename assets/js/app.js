// ============================================
// 인비랩 공통 스크립트
// - Supabase 연결, 로그인 상태 관리, 헤더 표시
// ============================================
(function () {
  const cfg = window.INBILAB_CONFIG || {};
  window.sb = null; // Supabase 클라이언트 (전역)

  // Supabase 설정이 되어 있고 SDK가 로드됐으면 클라이언트 생성
  if (cfg.SUPABASE_URL && cfg.SUPABASE_ANON_KEY && window.supabase) {
    window.sb = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  }

  // ---------- 유틸 ----------
  window.$id = (id) => document.getElementById(id);

  window.showMsg = function (id, text, type) {
    const el = $id(id);
    if (!el) return;
    el.textContent = text;
    el.className = "msg " + (type || "err");
  };

  window.fmtDate = function (iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    const diff = (now - d) / 86400000; // 일 단위
    if (diff < 1 && d.getDate() === now.getDate()) {
      return d.getHours().toString().padStart(2, "0") + ":" + d.getMinutes().toString().padStart(2, "0");
    }
    return d.getFullYear() + "." + (d.getMonth() + 1).toString().padStart(2, "0") + "." + d.getDate().toString().padStart(2, "0");
  };

  window.escapeHtml = function (s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  };

  // ---------- 로그인 상태 ----------
  window.getUser = async function () {
    if (!sb) return null;
    const { data } = await sb.auth.getUser();
    return data && data.user ? data.user : null;
  };

  window.getNickname = function (user) {
    if (!user) return "";
    return (user.user_metadata && user.user_metadata.nickname) || (user.email ? user.email.split("@")[0] : "회원");
  };

  window.logout = async function () {
    if (sb) await sb.auth.signOut();
    location.href = "index.html";
  };

  // 로그인 필수 페이지에서 사용: 미로그인 시 로그인 페이지로 이동
  window.requireLogin = async function () {
    if (!sb) return null; // 설정 전이면 통과(미리보기 모드)
    const user = await getUser();
    if (!user) {
      location.href = "login.html?next=" + encodeURIComponent(location.pathname.split("/").pop());
      return null;
    }
    return user;
  };

  // ---------- 헤더 렌더링 ----------
  // 각 페이지의 <div id="site-header"></div> 에 공통 헤더를 그린다
  window.renderHeader = async function (active) {
    const el = $id("site-header");
    if (!el) return;
    const user = sb ? await getUser() : null;
    const menu = [
      ["dashboard.html", "dashboard", "홈"],
      ["today.html", "today", "뜨는 채널"],
      ["rocket.html", "rocket", "로켓 채널"],
      ["channel.html", "channel", "채널 분석"],
      ["favorites.html", "favorites", "관심 채널"],
      ["community.html", "community", "성장 기록실"],
      ["study.html", "study", "학습자료"],
    ];
    const menuHtml = menu
      .map(([href, key, label]) =>
        `<a href="${href}" class="${key === active ? "on" : ""}">${label}</a>`)
      .join("");
    const right = user
      ? `<span class="hello">${escapeHtml(getNickname(user))} 님</span>
         <a class="btn btn-ghost btn-sm" href="profile.html">내 정보</a>
         <button class="btn btn-ghost btn-sm" onclick="logout()">로그아웃</button>`
      : `<a class="btn btn-ghost btn-sm" href="login.html">로그인</a>
         <a class="btn btn-main btn-sm" href="register.html">무료로 시작</a>`;
    el.innerHTML = `
      <header class="site">
        <div class="wrap nav">
          <a class="logo" href="${user ? "dashboard.html" : "index.html"}">인비<span>랩</span></a>
          <nav class="menu">${menuHtml}</nav>
          <div class="btns">${right}</div>
        </div>
      </header>`;

    // Supabase 설정 전이면 안내 배너 표시
    if (!sb) {
      const b = document.createElement("div");
      b.className = "setup-banner";
      b.innerHTML = "⚙️ <b>미리보기 모드</b>입니다. Supabase 연결값(config.js)을 넣으면 회원가입·게시판이 실제로 작동합니다.";
      el.appendChild(b);
    }
  };

  // ---------- 푸터 ----------
  window.renderFooter = function () {
    const el = $id("site-footer");
    if (!el) return;
    el.innerHTML = `
      <footer class="site">
        <div class="wrap">
          인비랩(InbiLab) · inbilab.ai.kr<br>
          이용약관 · 개인정보처리방침 · 사업자정보
        </div>
      </footer>`;
  };
})();
