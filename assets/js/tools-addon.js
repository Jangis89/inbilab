// ============================================
// 인비랩 도구 애드온
// - analyze.html: 🪝 후킹 분석 박스 (첫 3~5초 해부)
// - lab.html: 새 후킹 유형 후보 목록 (관리자)
// 페이지 원본을 건드리지 않고 <script src>로 부착되는 방식
// ============================================
(function () {
  "use strict";

  const TYPE_NAMES = {
    1: "호기심 갭", 2: "결과 먼저", 3: "충격 비주얼", 4: "질문 던지기", 5: "공감 저격",
    6: "손실 회피", 7: "숫자·랭킹", 8: "반전 예고", 9: "권위·증거", 10: "패턴 파괴",
  };

  // ---------------- 후킹 분석 (analyze.html) ----------------
  function initHook() {
    const anRes = document.getElementById("an-res");
    if (!anRes) return; // 분석 페이지가 아니면 통과

    const css = `
    .hk-box{margin-top:16px;background:#fff;border:2px solid #fecdd3;border-radius:14px;padding:18px;}
    .hk-box h3{font-size:16.5px;font-weight:800;margin:0 0 4px;}
    .hk-box .hk-desc{font-size:13.5px;color:var(--sub);margin-bottom:12px;line-height:1.55;}
    .hk-box .hkbtn{padding:12px 22px;border-radius:11px;border:none;background:#e11d48;color:#fff;font-size:15px;font-weight:800;cursor:pointer;font-family:inherit;}
    .hk-box .hkbtn:disabled{opacity:.5;}
    .hk-res{display:none;margin-top:14px;}
    .hk-res.show{display:block;}
    .hk-sec{border-top:1px solid var(--line);padding:12px 0;}
    .hk-sec .hs-t{font-size:13px;font-weight:800;color:#9f1239;margin-bottom:6px;}
    .hk-facts{background:#fff1f2;border:1px solid #fecdd3;border-radius:11px;padding:12px 15px;font-size:14px;line-height:1.7;}
    .hk-facts .fl{display:flex;gap:8px;}
    .hk-facts .fl .fk{flex-shrink:0;font-weight:800;color:#9f1239;font-size:12.5px;padding-top:2px;width:58px;}
    .hk-strat-main{display:inline-block;background:#e11d48;color:#fff;font-size:14px;font-weight:800;padding:5px 15px;border-radius:999px;margin-right:6px;}
    .hk-strat-new{background:#7c3aed;}
    .hk-strat-sub{display:inline-block;background:#fff1f2;color:#9f1239;border:1px solid #fecdd3;font-size:12.5px;font-weight:700;padding:4px 12px;border-radius:999px;margin-right:6px;}
    .hk-conf{display:inline-block;font-size:11.5px;font-weight:800;padding:2px 10px;border-radius:999px;vertical-align:middle;}
    .hc-높음{background:#dcfce7;color:#166534;}
    .hc-중간{background:#fef9c3;color:#854d0e;}
    .hc-낮음{background:#fee2e2;color:#991b1b;}
    .hk-lowwarn{margin-top:8px;background:#fffbeb;border:1px solid #fde68a;border-radius:9px;padding:8px 12px;font-size:12.5px;color:#92400e;line-height:1.55;}
    .hk-txt{font-size:14px;line-height:1.7;color:#334155;}
    .hk-var{background:var(--bg-soft);border:1px solid var(--line);border-radius:11px;padding:11px 14px;margin-bottom:8px;}
    .hk-var .hv-k{font-size:11.5px;font-weight:800;color:#e11d48;margin-bottom:6px;}
    .hk-var .hv-cap{background:#111;border-radius:9px;padding:11px 10px;text-align:center;line-height:1.35;}
    .hk-var .hv-cap span{display:block;font-size:16.5px;font-weight:900;color:#fff;letter-spacing:-0.3px;text-shadow:1.5px 1.5px 0 #000;}
    .hk-var .hv-cap span.y{color:#ffd400;}
    .hk-var .hv-cnt{font-size:11px;color:#94a3b8;font-weight:600;margin:4px 0 6px;text-align:center;}
    .hk-var .hv-nar{font-size:13.5px;font-weight:600;line-height:1.55;color:#334155;margin-top:2px;}
    .hk-var .hv-l{font-size:14px;font-weight:700;line-height:1.5;}
    .hk-var .hv-s{font-size:12.5px;color:var(--sub);margin-top:3px;}
    .hk-var .hv-s:before{content:"🎬 ";}
    .hk-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;}
    .hk-chip{padding:6px 13px;border-radius:999px;border:1.5px solid #fecdd3;background:#fff;color:#9f1239;font-size:12.5px;font-weight:700;cursor:pointer;font-family:inherit;}
    .hk-chip:hover{background:#fff1f2;}
    .hk-chip:disabled{opacity:.5;cursor:default;}
    .hk-chip.done{background:#fff1f2;border-color:#e11d48;cursor:default;opacity:.65;}
    .hk-chip.all{background:#e11d48;color:#fff;border-color:#e11d48;}
    .hk-note{font-size:12px;color:#94a3b8;margin-top:8px;line-height:1.5;}
    .hk-dl{display:flex;gap:9px;margin-top:12px;flex-wrap:wrap;}
    .hk-dl button{padding:10px 16px;border-radius:10px;border:1.5px solid #fecdd3;background:#fff;color:#e11d48;font-size:13.5px;font-weight:800;cursor:pointer;font-family:inherit;}
    .hk-dl button.main{background:#e11d48;color:#fff;border-color:#e11d48;}`;
    document.head.insertAdjacentHTML("beforeend", "<style>" + css + "</style>");

    const box = document.createElement("div");
    box.className = "hk-box";
    box.id = "hk-box";
    box.style.display = "none";
    box.innerHTML = `
      <h3>🪝 후킹 분석 <span style="font-size:12px;color:#94a3b8;font-weight:600;">첫 3~5초 해부</span></h3>
      <div class="hk-desc">이 영상이 첫 3초 동안 <b>무엇으로, 어떤 심리 전략으로</b> 시청자를 붙잡았는지 분석하고, <b>같은 영상을 다르게 여는</b> 응용 3버전을 제안합니다.</div>
      <button class="hkbtn" id="btn-hk">후킹 분석하기</button>
      <div class="an-load" id="hk-load"><div class="spin"></div><span id="hk-load-txt">AI가 첫 3초를 해부하고 있습니다…</span></div>
      <div class="an-err" id="hk-err"></div>
      <div class="hk-res" id="hk-res">
        <div class="hk-sec" style="border-top:none;">
          <div class="hs-t">1️⃣ 첫 3~5초에 실제로 나온 것 (사실)</div>
          <div class="hk-facts" id="hk-facts"></div>
        </div>
        <div class="hk-sec">
          <div class="hs-t">2️⃣ 전략 판정</div>
          <div id="hk-strat"></div>
        </div>
        <div class="hk-sec">
          <div class="hs-t">3️⃣ 심리 해설 — 시청자 머릿속에서 일어나는 일</div>
          <div class="hk-txt" id="hk-psy"></div>
        </div>
        <div class="hk-sec">
          <div class="hs-t">4️⃣ 제작자의 의도 (AI의 추정)</div>
          <div class="hk-txt" id="hk-intent"></div>
        </div>
        <div class="hk-sec">
          <div class="hs-t">5️⃣ 3초 이후에도 붙잡아두는 장치</div>
          <div class="hk-txt" id="hk-ret"></div>
        </div>
        <div class="hk-sec">
          <div class="hs-t">6️⃣ 이 영상을 다르게 열어보기 — 3가지 버전</div>
          <div id="hk-vars"></div>
        </div>
        <div class="hk-sec">
          <div class="hs-t">🎯 다른 전략으로도 열어보기 <span style="font-weight:600;color:#94a3b8;font-size:11px;">궁금한 전략을 누르면 그 버전을 즉석에서 만들어 드립니다 (영상 재분석 없음 · 빠름)</span></div>
          <div class="hk-chips" id="hk-chips"></div>
          <div class="an-err" id="hkm-err"></div>
        </div>
        <div class="hk-note">※ 전략 판정과 의도는 AI의 해석이며 참고용입니다. 1️⃣의 실제 내용을 직접 보고 스스로도 판단해 보세요.</div>
        <div class="hk-dl">
          <button class="main" onclick="dlHook()">📄 후킹 분석.txt 다운로드</button>
          <button onclick="copyHook()">📋 전체 복사</button>
        </div>
        <div id="hk-model" style="margin-top:8px;font-size:12px;color:#94a3b8;font-weight:600;"></div>
      </div>`;
    // 원본 후보 탐색 박스(src-box)가 있으면 그 앞에, 없으면 an-res 뒤에
    const srcBox = document.getElementById("src-box");
    if (srcBox) srcBox.insertAdjacentElement("beforebegin", box);
    else anRes.insertAdjacentElement("afterend", box);
    document.getElementById("btn-hk").addEventListener("click", runHook);

    let canHook = false;
    (async function gate() {
      try {
        const u = await getUser();
        if (!u) return;
        const adm = await isAdmin(u);
        if (adm) { canHook = true; }
        else {
          const { data } = await sb.from("feature_flags").select("is_public").eq("key", "hook_analyze").maybeSingle();
          canHook = !!(data && data.is_public);
        }
        // 이미 분석 결과가 열려 있으면 즉시 반영
        if (anRes.className.indexOf("show") >= 0 && canHook) box.style.display = "block";
      } catch {}
    })();

    // 분석 결과가 열리고 닫힐 때 후킹 박스도 함께
    new MutationObserver(function () {
      const show = anRes.className.indexOf("show") >= 0;
      box.style.display = show && canHook ? "block" : "none";
      if (show) {
        document.getElementById("hk-res").className = "hk-res";
        document.getElementById("hk-err").className = "an-err";
      }
    }).observe(anRes, { attributes: true, attributeFilter: ["class"] });

    function curVid() {
      const t = document.getElementById("r-thumb");
      const m = t && t.src ? t.src.match(/\/vi\/([A-Za-z0-9_-]{11})\//) : null;
      return m ? m[1] : null;
    }
    function pref() {
      const el = document.querySelector('input[name="msel"]:checked');
      return el ? el.value : "auto";
    }

    let lastHook = null;
    let hkTimer = null;

    async function runHook() {
      const vid = curVid();
      if (!vid) return;
      const btn = document.getElementById("btn-hk");
      const load = document.getElementById("hk-load");
      const err = document.getElementById("hk-err");
      err.className = "an-err";
      document.getElementById("hk-res").className = "hk-res";
      btn.disabled = true;
      load.className = "an-load show";
      let sec = 0;
      document.getElementById("hk-load-txt").textContent = "AI가 첫 3초를 해부하고 있습니다… (0초)";
      hkTimer = setInterval(function () {
        sec++;
        document.getElementById("hk-load-txt").textContent = "AI가 첫 3초를 해부하고 있습니다… (" + sec + "초)";
      }, 1000);
      try {
        const { data } = await sb.auth.getSession();
        const token = data && data.session ? data.session.access_token : "";
        const r = await fetch("/api/ai", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
          body: JSON.stringify({ feature: "hook_analyze", action: "hook", video_url: "https://www.youtube.com/watch?v=" + vid, model_pref: pref() }),
        });
        const j = await r.json().catch(function () { return {}; });
        if (!r.ok || !j.ok) {
          err.textContent = "후킹 분석 실패: " + (j.error || "HTTP " + r.status) + (j.detail ? " — " + j.detail : "");
          err.className = "an-err show";
        } else {
          lastHook = j.hook;
          renderHook(j.hook, j.model);
          document.getElementById("hk-res").className = "hk-res show";
        }
      } catch (e) {
        err.textContent = "요청이 실패했습니다: " + e.message;
        err.className = "an-err show";
      }
      clearInterval(hkTimer);
      load.className = "an-load";
      btn.disabled = false;
    }

    function stratName(s) {
      if (!s) return "";
      const id = Number(s.type_id);
      if (id >= 1 && id <= 10) return "유형 " + id + " · " + (TYPE_NAMES[id] || s.name || "");
      return s.name || "새 유형";
    }

    function renderHook(h, model) {
      const f = h.facts || {};
      let fh = "";
      if (Array.isArray(f.lines) && f.lines.length)
        fh += "<div class='fl'><span class='fk'>🎙️ 대사</span><span>" + f.lines.map(escapeHtml).join("<br>") + "</span></div>";
      if (Array.isArray(f.onscreen) && f.onscreen.length)
        fh += "<div class='fl'><span class='fk'>🔤 자막</span><span>" + f.onscreen.map(escapeHtml).join("<br>") + "</span></div>";
      if (f.visual) fh += "<div class='fl'><span class='fk'>🖼️ 화면</span><span>" + escapeHtml(f.visual) + "</span></div>";
      if (f.sound) fh += "<div class='fl'><span class='fk'>🔊 소리</span><span>" + escapeHtml(f.sound) + "</span></div>";
      document.getElementById("hk-facts").innerHTML = fh || "정보 없음";

      const m = h.main || {};
      const isNew = Number(m.type_id) === 0 || m.is_new === true;
      const conf = ["높음", "중간", "낮음"].indexOf(m.confidence) >= 0 ? m.confidence : "중간";
      let sh = "<span class='hk-strat-main" + (isNew ? " hk-strat-new" : "") + "'>" +
        (isNew ? "🆕 새 유형: " : "") + escapeHtml(stratName(m)) + "</span>";
      sh += "<span class='hk-conf hc-" + conf + "'>확신도 " + conf + "</span>";
      const subs = Array.isArray(h.sub) ? h.sub.filter(function (s) { return s && (s.name || s.type_id); }) : [];
      if (subs.length) sh += "<div style='margin-top:8px;'>보조: " + subs.map(function (s) { return "<span class='hk-strat-sub'>" + escapeHtml(stratName(s)) + "</span>"; }).join("") + "</div>";
      if (conf === "낮음" || isNew) {
        sh += "<div class='hk-lowwarn'>⚠️ " + (isNew
          ? "교과서 10유형에 없는 새로운 방식입니다. AI가 관찰한 내용을 그대로 보여드리니 1️⃣의 실제 장면과 함께 판단해 보세요. (이 사례는 자동으로 수집되어 분류표 개선에 사용됩니다)"
          : "판정 확신도가 낮습니다 — 교과서 유형에 딱 맞지 않는 변형일 수 있어요. 1️⃣의 실제 내용을 직접 보고 판단해 보세요.") + "</div>";
      }
      document.getElementById("hk-strat").innerHTML = sh;

      document.getElementById("hk-psy").textContent = h.psychology || "-";
      document.getElementById("hk-intent").textContent = h.intent || "-";
      document.getElementById("hk-ret").innerHTML = (Array.isArray(h.retention) && h.retention.length)
        ? h.retention.map(function (x) { return "✔ " + escapeHtml(x); }).join("<br>") : "-";

      document.getElementById("hk-vars").innerHTML = (Array.isArray(h.variations) ? h.variations : []).map(varCardHtml).join("") || "-";
      renderChips();

      document.getElementById("hk-model").textContent = model
        ? (model.indexOf("pro") >= 0 ? "🤖 분석 모델: Gemini Pro (고성능)" : "🤖 분석 모델: Gemini Flash (기본)")
        : "";
    }

    function varCardHtml(v) {
      let inner = "<div class='hv-k'>" + escapeHtml(v.kind || "") + (v.strategy ? " — " + escapeHtml(v.strategy) : "") + "</div>";
      const cap = Array.isArray(v.caption) ? v.caption.filter(Boolean) : [];
      if (cap.length) {
        inner += "<div class='hv-cap'>" + cap.map(function (line, i) {
          return "<span class='" + (i === 1 ? "y" : "") + "'>" + escapeHtml(line) + "</span>";
        }).join("") + "</div>";
        inner += "<div class='hv-cnt'>자막 글자수 (띄어쓰기 포함): " + cap.map(function (line, i) {
          return (i === 0 ? "윗줄 " : "아랫줄 ") + line.length + "자";
        }).join(" · ") + "</div>";
      }
      const nar = v.narration || v.first_line || "";
      if (nar) inner += "<div class='hv-nar'>🎙️ 나레이션: “" + escapeHtml(nar) + "”</div>";
      if (v.first_scene) inner += "<div class='hv-s'>시작 장면: " + escapeHtml(v.first_scene) + "</div>";
      return "<div class='hk-var'>" + inner + "</div>";
    }

    // ---- 전략 칩: 원하는 전략으로 추가 버전 생성 (텍스트 호출 — 저비용·빠름) ----
    function nameToId(name) {
      for (const k in TYPE_NAMES) if (TYPE_NAMES[k] === name) return Number(k);
      return 0;
    }
    function computeDone() {
      const s = new Set();
      const h = lastHook || {};
      if (h.main && Number(h.main.type_id) >= 1) s.add(Number(h.main.type_id));
      (h.variations || []).forEach(function (v) {
        const id = v.type_id ? Number(v.type_id) : nameToId(v.strategy || "");
        if (id >= 1) s.add(id);
      });
      return s;
    }
    function renderChips() {
      const el = document.getElementById("hk-chips");
      if (!el) return;
      const done = computeDone();
      let html = "";
      for (let i = 1; i <= 10; i++) {
        html += done.has(i)
          ? "<button class='hk-chip done' disabled>✓ " + TYPE_NAMES[i] + "</button>"
          : "<button class='hk-chip' data-id='" + i + "'>" + TYPE_NAMES[i] + "</button>";
      }
      if (done.size < 10) html += "<button class='hk-chip all' id='hk-all'>⚡ 나머지 전략 전부 보기</button>";
      el.innerHTML = html;
      el.querySelectorAll(".hk-chip[data-id]").forEach(function (b) {
        b.addEventListener("click", function () { runHookMore([Number(b.dataset.id)]); });
      });
      const all = document.getElementById("hk-all");
      if (all) all.addEventListener("click", function () {
        const d = computeDone();
        const rest = [];
        for (let i = 1; i <= 10; i++) if (!d.has(i)) rest.push(i);
        if (rest.length) runHookMore(rest);
      });
    }
    let hkMoreBusy = false;
    async function runHookMore(ids) {
      if (hkMoreBusy || !lastHook) return;
      hkMoreBusy = true;
      const err = document.getElementById("hkm-err");
      err.className = "an-err";
      const chipsEl = document.getElementById("hk-chips");
      chipsEl.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      const note = document.createElement("div");
      note.style.cssText = "font-size:12.5px;color:#9f1239;font-weight:700;padding:6px 0;";
      note.textContent = "✍️ 새 버전을 쓰는 중… (3~10초)";
      chipsEl.insertAdjacentElement("afterend", note);
      try {
        const { data } = await sb.auth.getSession();
        const token = data && data.session ? data.session.access_token : "";
        const r = await fetch("/api/ai", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
          body: JSON.stringify({
            feature: "hook_analyze", action: "hook_more",
            hook: { facts: lastHook.facts, main: lastHook.main, sub: lastHook.sub, psychology: lastHook.psychology },
            strategies: ids,
          }),
        });
        const j = await r.json().catch(function () { return {}; });
        if (!r.ok || !j.ok) {
          err.textContent = "추가 버전 실패: " + (j.error || "HTTP " + r.status) + (j.detail ? " — " + j.detail : "");
          err.className = "an-err show";
        } else {
          (j.variations || []).forEach(function (v) {
            const vv = {
              kind: "선택한 전략으로 열기",
              strategy: v.strategy || TYPE_NAMES[Number(v.type_id)] || "",
              type_id: v.type_id, caption: v.caption, narration: v.narration, first_scene: v.first_scene,
            };
            lastHook.variations = lastHook.variations || [];
            lastHook.variations.push(vv);
            document.getElementById("hk-vars").insertAdjacentHTML("beforeend", varCardHtml(vv));
          });
        }
      } catch (e) {
        err.textContent = "요청이 실패했습니다: " + e.message;
        err.className = "an-err show";
      }
      note.remove();
      hkMoreBusy = false;
      renderChips();
    }

    function hookText() {
      if (!lastHook) return "";
      const h = lastHook, f = h.facts || {}, m = h.main || {};
      const L = [];
      L.push("■ 인비랩 후킹 분석 (첫 3~5초)");
      L.push("영상: https://www.youtube.com/watch?v=" + (curVid() || ""));
      L.push("");
      L.push("[1. 실제로 나온 것]");
      (f.lines || []).forEach(function (x) { L.push("  대사: " + x); });
      (f.onscreen || []).forEach(function (x) { L.push("  자막: " + x); });
      if (f.visual) L.push("  화면: " + f.visual);
      if (f.sound) L.push("  소리: " + f.sound);
      L.push("");
      L.push("[2. 전략 판정] " + stratName(m) + " (확신도 " + (m.confidence || "중간") + ")");
      (h.sub || []).forEach(function (s) { L.push("  보조: " + stratName(s)); });
      L.push("");
      L.push("[3. 심리 해설] " + (h.psychology || ""));
      L.push("[4. 제작자 의도(추정)] " + (h.intent || ""));
      L.push("[5. 이탈 방지 장치]");
      (h.retention || []).forEach(function (x) { L.push("  - " + x); });
      L.push("");
      L.push("[6. 이 영상을 다르게 열어보기 - 3버전]");
      (h.variations || []).forEach(function (v, i) {
        L.push("  " + (i + 1) + ") " + (v.kind || "") + (v.strategy ? " — " + v.strategy : ""));
        const cap = Array.isArray(v.caption) ? v.caption.filter(Boolean) : [];
        if (cap.length) {
          L.push("     자막(상단 두 줄):");
          cap.forEach(function (line) { L.push("       " + line + "  (" + line.length + "자)"); });
        }
        const nar = v.narration || v.first_line || "";
        if (nar) L.push("     나레이션: " + nar);
        if (v.first_scene) L.push("     시작 장면: " + v.first_scene);
      });
      L.push("");
      L.push("※ 전략 판정과 의도는 AI의 해석이며 참고용입니다.");
      return L.join("\n");
    }

    window.dlHook = function () {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob(["﻿" + hookText()], { type: "text/plain;charset=utf-8" }));
      a.download = "후킹분석_" + (curVid() || "video") + ".txt";
      a.click();
    };
    window.copyHook = async function () {
      try { await navigator.clipboard.writeText(hookText()); alert("후킹 분석이 복사되었습니다."); }
      catch { alert("복사에 실패했습니다. 다운로드 버튼을 이용해 주세요."); }
    };
  }

  // ---------------- 원본 후보 탐색 확장: 해외 플랫폼 + 구글렌즈 (analyze.html) ----------------
  function fmtV(n){
    n = Number(n) || 0;
    if (n >= 100000000) return (n/100000000).toFixed(1).replace(/\.0$/,"") + "억";
    if (n >= 10000) return (n/10000).toFixed(1).replace(/\.0$/,"") + "만";
    if (n >= 1000) return (n/1000).toFixed(1).replace(/\.0$/,"") + "천";
    return String(n);
  }
  function ibReorder(){
    var host = document.getElementById("src-res");
    if (!host) return;
    ["src-origin","ib-prev-sum","src-cards","ib-long-wrap","src-ytlinks","src-intl","src-lens","src-cc","src-stock"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el && el.parentNode === host) host.appendChild(el);
    });
  }
  function ibLangChips(j){
    var box = document.getElementById("src-intl");
    if (!box || box.__ibLang) return;
    box.__ibLang = true;
    var byLang = {};
    ((j && j.yt_links) || []).forEach(function (l) { var L = (l.lang || "").slice(0, 2); if (L && !byLang[L]) byLang[L] = l.q; });
    var bar = document.createElement("div");
    bar.style.cssText = "margin:4px 0 8px;font-size:12px;color:#92400e;display:flex;align-items:center;gap:6px;flex-wrap:wrap";
    var lab = document.createElement("span"); lab.style.fontWeight = "700"; lab.textContent = "틱톡·인스타 검색 언어:";
    bar.appendChild(lab);
    [["auto","자동 추천"],["ko","한국어"],["en","영어"],["zh","중국어"]].forEach(function (m) {
      if (m[0] !== "auto" && !byLang[m[0]]) return;
      var b = document.createElement("button"); b.type = "button"; b.textContent = m[1];
      b.style.cssText = "border:1px solid #fde68a;background:" + (m[0] === "auto" ? "#f59e0b" : "#fff") + ";color:" + (m[0] === "auto" ? "#fff" : "#92400e") + ";border-radius:999px;padding:2px 10px;font-size:11.5px;font-weight:700;cursor:pointer;font-family:inherit";
      b.addEventListener("click", function () {
        bar.querySelectorAll("button").forEach(function (x) { x.style.background = "#fff"; x.style.color = "#92400e"; });
        b.style.background = "#f59e0b"; b.style.color = "#fff";
        var q2 = m[0] === "auto" ? null : byLang[m[0]];
        box.querySelectorAll(".intl-row").forEach(function (row) {
          var iq = row.querySelector(".iq"); if (!iq) return;
          var rowQ = iq.textContent.replace(/^「|」$/g, "").trim();
          row.querySelectorAll("a").forEach(function (a) {
            var label = a.textContent.trim();
            if (label !== "틱톡" && label !== "인스타 릴스") return;
            if (!a.getAttribute("data-oh")) a.setAttribute("data-oh", a.getAttribute("href"));
            if (q2 === null) { a.setAttribute("href", a.getAttribute("data-oh")); a.removeAttribute("data-q"); }
            else { a.setAttribute("href", a.getAttribute("data-oh").replace(encodeURIComponent(rowQ), encodeURIComponent(q2))); a.setAttribute("data-q", q2); }
          });
        });
        ibToast(m[0] === "auto" ? "틱톡·인스타가 각 검색어의 원래 언어로 검색됩니다" : "틱톡·인스타가 " + m[1] + " 검색어로 검색됩니다");
      });
      bar.appendChild(b);
    });
    var t = box.querySelector(".ib-t");
    if (t && t.nextSibling) box.insertBefore(bar, t.nextSibling); else box.appendChild(bar);
  }
  function ibToast(msg){
    var t = document.getElementById("ib-toast");
    if (!t) { t = document.createElement("div"); t.id = "ib-toast"; t.style.cssText = "position:fixed;left:50%;bottom:30px;transform:translateX(-50%);background:#111827;color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;z-index:99999;opacity:0;transition:opacity .2s;max-width:90%;text-align:center;box-shadow:0 6px 20px rgba(0,0,0,.3)"; document.body.appendChild(t); }
    t.textContent = msg; t.style.opacity = "1";
    clearTimeout(t.__ibTimer); t.__ibTimer = setTimeout(function () { t.style.opacity = "0"; }, 2800);
  }
  function ibSmartLinks(j){
    var box = document.getElementById("src-intl");
    if (box && !box.__ibWired) {
      box.__ibWired = true;
      box.addEventListener("click", function (e) {
        var a = e.target.closest ? e.target.closest("a[target='_blank']") : null;
        if (!a || !box.contains(a)) return;
        var row = a.closest(".intl-row"); var q = (a.getAttribute("data-q") || "").trim();
        if (row) { var iq = row.querySelector(".iq"); if (iq) q = iq.textContent.replace(/^「|」$/g, "").trim(); }
        if (q) { try { navigator.clipboard.writeText(q); ibToast("검색어를 복사했어요 — 검색창이 비어 있으면 붙여넣기(Ctrl+V) 하세요"); } catch (_) {} }
      });
    }
    var lens = document.getElementById("src-lens");
    if (lens && !lens.__ibBaidu && j && j.video_id) {
      lens.__ibBaidu = true;
      var thumb = "https://i.ytimg.com/vi/" + j.video_id + "/hqdefault.jpg";
      var b = document.createElement("button");
      b.className = "lens-btn"; b.type = "button";
      b.style.cssText = "border:none;cursor:pointer;font-family:inherit;margin-top:6px;background:#2932e1;color:#fff";
      b.textContent = "🅑 바이두 식별(识图)로 원본 찾기 — 썸네일 자동 복사";
      function ibLoadImg(u) {
        return new Promise(function (res) {
          try { var im = new Image(); im.crossOrigin = "anonymous"; im.onload = function () { res(im); }; im.onerror = function () { res(null); }; im.src = u; setTimeout(function () { res(null); }, 6000); } catch (e) { res(null); }
        });
      }
      function ibToPng(cv) { return new Promise(function (res) { try { cv.toBlob(function (bb) { res(bb || null); }, "image/png"); } catch (e) { res(null); } }); }
      var prep = (async function () {
        try {
          var vid2 = (thumb.match(/\/vi\/([^/]+)\//) || [])[1] || "";
          // 1순위: 쇼츠 전용 세로 원본 썸네일 (흐린 좌우 띠 없음, 1080x1920)
          if (vid2) {
            var oar = await ibLoadImg("https://i.ytimg.com/vi/" + vid2 + "/oar2.jpg");
            if (oar && oar.naturalHeight > oar.naturalWidth) {
              var cvA = document.createElement("canvas"); cvA.width = oar.naturalWidth; cvA.height = oar.naturalHeight;
              cvA.getContext("2d").drawImage(oar, 0, 0);
              var bA = await ibToPng(cvA); if (bA) return bA;
            }
          }
          // 2순위: 가로 썸네일에서 가운데 9:16 세로 영역만 잘라내기
          var base = null;
          if (vid2) { base = await ibLoadImg("https://i.ytimg.com/vi/" + vid2 + "/maxresdefault.jpg"); if (base && base.naturalWidth < 200) base = null; }
          if (!base) base = await ibLoadImg(thumb);
          if (!base) return null;
          var w = base.naturalWidth, h = base.naturalHeight;
          var tw = Math.round(h * 9 / 16);
          var cvB = document.createElement("canvas");
          if (tw < w) { cvB.width = tw; cvB.height = h; cvB.getContext("2d").drawImage(base, Math.round((w - tw) / 2), 0, tw, h, 0, 0, tw, h); }
          else { cvB.width = w; cvB.height = h; cvB.getContext("2d").drawImage(base, 0, 0); }
          return await ibToPng(cvB);
        } catch (e) { return null; }
      })();
      b.addEventListener("click", async function () {
        var blob = null;
        try { blob = await prep; } catch (_) {}
        if (blob && typeof ClipboardItem !== "undefined") {
          try {
            await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
            ibToast("✅ 썸네일 이미지가 복사됐어요! 열린 바이두 페이지에서 📷 카메라 아이콘을 누른 뒤 Ctrl+V 하면 검색이 바로 시작됩니다");
            window.open("https://graph.baidu.com/pcpage/index?tpl_from=pc", "_blank");
            return;
          } catch (_) {}
        }
        try { navigator.clipboard.writeText(thumb); } catch (_) {}
        ibToast("이미지 주소를 복사했어요. 바이두 페이지의 카메라 아이콘 → 주소 붙여넣기 → 识图一下를 누르세요");
        window.open("https://graph.baidu.com/pcpage/index?tpl_from=pc", "_blank");
      });
      lens.appendChild(b);
      var tip2 = document.createElement("div");
      tip2.style.cssText = "font-size:11.5px;color:#666;margin-top:5px;line-height:1.5";
      tip2.textContent = "💡 중국에서 온 영상은 바이두 식별이 원본을 특히 잘 찾습니다. 버튼 클릭 → 열린 페이지의 📷 아이콘 클릭 → Ctrl+V, 이 세 번이면 끝!";
      lens.appendChild(tip2);
    }
  }
  function ibPlayerModal(vid, isShort) {
    var old = document.getElementById("ib-player-modal"); if (old) old.remove();
    var m = document.createElement("div"); m.id = "ib-player-modal";
    m.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:99998;display:flex;align-items:center;justify-content:center;padding:16px";
    var w = isShort ? "min(380px, 90vw)" : "min(860px, 94vw)";
    var pt = isShort ? "177.78%" : "56.25%";
    m.innerHTML = '<div style="background:#000;border-radius:12px;overflow:hidden;width:' + w + '">' +
      '<div style="position:relative;padding-top:' + pt + '">' +
      '<iframe src="https://www.youtube.com/embed/' + vid + '?autoplay=1" style="position:absolute;inset:0;width:100%;height:100%;border:0" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe></div>' +
      '<div style="display:flex;gap:8px;justify-content:space-between;align-items:center;padding:8px 12px;background:#111">' +
      '<a href="https://www.youtube.com/watch?v=' + vid + '" target="_blank" style="color:#fff;font-size:13px;text-decoration:none">▶ YouTube에서 열기 →</a>' +
      '<button type="button" id="ib-pm-close" style="background:#333;color:#fff;border:none;border-radius:6px;padding:5px 14px;cursor:pointer;font-size:13px;font-family:inherit">닫기 ✕</button></div></div>';
    m.addEventListener("click", function (e) { if (e.target === m) m.remove(); });
    document.body.appendChild(m);
    m.querySelector("#ib-pm-close").addEventListener("click", function () { m.remove(); });
  }
  function ibEnrichPreview(j){
    var cards = (j && j.candidates) || [];
    var host = document.getElementById("src-cards");
    if (!host) return;
    var oldSum = document.getElementById("ib-prev-sum");
    if (oldSum) oldSum.remove();
    var oldWrap = document.getElementById("ib-long-wrap");
    if (oldWrap) oldWrap.remove();
    var oldPm = document.getElementById("ib-player-modal");
    if (oldPm) oldPm.remove();
    if (!document.getElementById("ib-prev-style")) {
      var st = document.createElement("style");
      st.id = "ib-prev-style";
      st.textContent = ".ib-badges{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}.ib-b{font-size:11px;padding:2px 7px;border-radius:999px;background:var(--line,#eee);color:var(--sub,#555);white-space:nowrap}.ib-short{background:#ffe8d6;color:#b5480a}.ib-long{background:#e6ecff;color:#274bd1}.ib-embed{background:#e4f7e8;color:#1c8a3b}.ib-sum{font-size:12px;color:var(--sub,#666);margin:2px 0 8px}";
      document.head.appendChild(st);
    }
    var nodes = host.querySelectorAll(".src-card");
    var nShort = 0, nLong = 0, nUnknown = 0, longNodes = [];
    for (var i = 0; i < nodes.length && i < cards.length; i++) {
      var c = cards[i], node = nodes[i];
      if (!node || node.querySelector(".ib-badges")) continue;
      var badges = [];
      var hasDur = typeof c.duration_seconds === "number" && c.duration_seconds > 0;
      if (hasDur) badges.push('<span class="ib-b">⏱ ' + (c.duration_label || "") + '</span>');
      if (typeof c.view_count === "number" && c.view_count > 0) badges.push('<span class="ib-b">👁 ' + fmtV(c.view_count) + '</span>');
      if (c.is_short_candidate) { badges.push('<span class="ib-b ib-short">쇼츠 후보' + (c.short_confidence ? (' · 확신 ' + c.short_confidence) : '') + '</span>'); nShort++; }
      else if (hasDur) { badges.push('<span class="ib-b ib-long">긴 원본 후보</span>'); nLong++; longNodes.push(node); }
      else { nUnknown++; }
      if (c.match_type) { var mt = ({same_source:"🎯 동일 원본 후보",same_event:"🔁 같은 사건·인물",visual_similar:"👀 시각적 유사",long_full_version:"📼 전체본 후보"})[c.match_type]; if (mt) badges.push('<span class="ib-b" style="background:#ede9fe;color:#5b21b6">' + mt + ' <span style="opacity:.65">(추정)</span></span>'); }
      if (c.found_by) { var fbq = String(c.found_by).replace(/[<>&"\']/g, "").slice(0, 24); badges.push('<span class="ib-b">🔎 「' + fbq + '」로 발견</span>'); }
      if (c.embeddable) badges.push('<span class="ib-b ib-embed">인비랩 재생 가능</span>');
      if (c.embeddable && c.video_id) { (function (nd, cid, isS) { nd.querySelectorAll("a").forEach(function (a) { if ((a.getAttribute("href") || "").indexOf(cid) !== -1) { a.addEventListener("click", function (ev) { ev.preventDefault(); ibPlayerModal(cid, isS); }); } }); })(node, c.video_id, !!c.is_short_candidate); }
      if (badges.length) {
        var d = document.createElement("div");
        d.className = "ib-badges";
        d.innerHTML = badges.join(" ");
        var info = node.querySelector(".sc-info") || node;
        info.appendChild(d);
      }
    }
    if (longNodes.length) {
      var lwrap = document.createElement("div"); lwrap.id = "ib-long-wrap";
      var ltg = document.createElement("button"); ltg.type = "button";
      ltg.style.cssText = "margin:8px 0 2px;background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;border-radius:8px;padding:7px 14px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit";
      ltg.textContent = "📼 긴 원본 후보 " + longNodes.length + "개 보기 ▾";
      var lcards = document.createElement("div"); lcards.style.display = "none";
      lwrap.appendChild(ltg); lwrap.appendChild(lcards);
      if (host.parentNode) host.parentNode.insertBefore(lwrap, host.nextSibling);
      for (var k2 = 0; k2 < longNodes.length; k2++) lcards.appendChild(longNodes[k2]);
      ltg.addEventListener("click", function () { var open = lcards.style.display !== "none"; lcards.style.display = open ? "none" : "block"; ltg.textContent = "📼 긴 원본 후보 " + longNodes.length + "개 " + (open ? "보기 ▾" : "접기 ▴"); });
    }
    if ((nShort + nLong) > 0 && !document.getElementById("ib-prev-sum")) {
      var sum = document.createElement("div");
      sum.id = "ib-prev-sum";
      sum.className = "ib-sum";
      sum.textContent = "🎬 쇼츠 후보 " + nShort + "개 · 긴 원본 후보 " + nLong + "개" + (nUnknown ? (" · 길이 미확인 " + nUnknown + "개") : "");
      if (host.parentNode) host.parentNode.insertBefore(sum, host);
    }
  }
  function initSources() {
    if (!document.getElementById("src-res") || typeof window.renderSources !== "function") return;

    const css2 = `
    .intl-box{margin-top:14px;background:#fffbeb;border:1px solid #fde68a;border-radius:11px;padding:12px 15px;}
    .intl-box .ib-t{font-size:13px;font-weight:800;color:#92400e;margin-bottom:7px;}
    .intl-row{margin-bottom:7px;font-size:13px;}
    .intl-row .iq{font-weight:700;color:#334155;margin-right:6px;}
    .intl-row a{display:inline-block;background:#fff;border:1px solid #fde68a;border-radius:999px;padding:3px 11px;font-size:12px;font-weight:700;color:#92400e;text-decoration:none;margin:2px 5px 2px 0;}
    .intl-row a:hover{border-color:#d97706;color:#d97706;}
    .intl-warn{font-size:11.5px;color:#a16207;line-height:1.5;margin-top:6px;}
    .cc-box{margin-top:10px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:11px;padding:12px 15px;}
    .cc-box .ib-t{font-size:13px;font-weight:800;color:#166534;margin-bottom:7px;}
    .cc-row{margin-bottom:7px;font-size:13px;}
    .cc-row .iq{font-weight:700;color:#334155;}
    .cc-row a,.cc-row button{display:inline-block;background:#fff;border:1px solid #bbf7d0;border-radius:999px;padding:3px 11px;font-size:12px;font-weight:700;color:#166534;text-decoration:none;margin:2px 5px 2px 0;cursor:pointer;font-family:inherit;}
    .cc-row a:hover,.cc-row button:hover{border-color:#16a34a;}
    .cc-row button:disabled{opacity:.5;}
    .cc-results{margin-top:4px;}
    .cc-card{display:flex;gap:10px;align-items:center;padding:7px 0;border-top:1px solid #dcfce7;text-decoration:none;}
    .cc-card img{width:86px;height:48px;object-fit:cover;border-radius:7px;background:#000;flex-shrink:0;}
    .cc-card .cc-t{font-size:13px;font-weight:700;color:#1e293b;line-height:1.4;}
    .cc-card .cc-m{font-size:11.5px;color:#64748b;}
    .cc-note{font-size:11.5px;color:#15803d;line-height:1.55;margin-top:6px;}
    .lens-box{margin-top:10px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:11px;padding:12px 15px;}
    .lens-box .ib-t{font-size:13px;font-weight:800;color:#1e40af;margin-bottom:7px;}
    .lens-btn{display:inline-block;background:#2563eb;color:#fff;border-radius:999px;padding:7px 16px;font-size:13px;font-weight:800;text-decoration:none;}
    .lens-tip{font-size:11.5px;color:#3b5bab;line-height:1.55;margin-top:7px;}`;
    document.head.insertAdjacentHTML("beforeend", "<style>" + css2 + "</style>");

    const PLATFORMS = [
    ["바이두검색", function (q) { return "https://www.baidu.com/s?wd=" + encodeURIComponent(q); }],
    ["바이두이미지", function (q) { return "https://image.baidu.com/search/index?tn=baiduimage&word=" + encodeURIComponent(q); }],
      ["도우인", function (q) { return "https://so.douyin.com/s?keyword=" + encodeURIComponent(q); }],
      ["샤오홍슈", function (q) { return "https://www.xiaohongshu.com/search_result?keyword=" + encodeURIComponent(q); }],
      ["틱톡", function (q) { return "https://www.tiktok.com/search?q=" + encodeURIComponent(q); }],
      ["빌리빌리", function (q) { return "https://search.bilibili.com/all?keyword=" + encodeURIComponent(q); }],
      ["인스타 릴스", function (q) { return "https://www.instagram.com/explore/search/keyword/?q=" + encodeURIComponent(q); }],
    ];

    const orig = window.renderSources;
    window.renderSources = function (j) {
      orig(j);
      try { enhance(j);
        try { ibEnrichPreview(j); } catch (e) {}
        try { ibSmartLinks(j); } catch (e) {}
        try { ibLangChips(j); } catch (e) {}
        try { ibReorder(); } catch (e) {} } catch {}
    };

    let srcAdmin = false;
    (async function () {
      try { const u = await getUser(); srcAdmin = u ? await isAdmin(u) : false; } catch {}
    })();

    async function forceSources(vid, btn) {
      if (btn) { btn.disabled = true; btn.textContent = "새로 탐색 중… (20~60초)"; }
      try {
        const { data } = await sb.auth.getSession();
        const token = data && data.session ? data.session.access_token : "";
        const el = document.querySelector('input[name="msel"]:checked');
        const r = await fetch("/api/ai", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
          body: JSON.stringify({ feature: "source_finder", action: "sources", video_url: "https://www.youtube.com/watch?v=" + vid, model_pref: el ? el.value : "auto", force: true }),
        });
        const j = await r.json().catch(function () { return {}; });
        if (r.ok && j.ok) window.renderSources(j);
        else if (btn) { btn.disabled = false; btn.textContent = "실패 — 다시 시도"; }
      } catch { if (btn) { btn.disabled = false; btn.textContent = "실패 — 다시 시도"; } }
    }

    function enhance(j) {
      const host = document.getElementById("src-res");
      if (!host) return;
      // 이전에 붙인 확장 블록 제거 (재실행 대비)
      const old1 = document.getElementById("src-intl"); if (old1) old1.remove();
      const old2 = document.getElementById("src-lens"); if (old2) old2.remove();
      const old0 = document.getElementById("src-cachenote"); if (old0) old0.remove();

      // 캐시에서 온 결과면 안내 (검색 한도 소모 0)
      if (j.cached) {
        const n = document.createElement("div");
        n.id = "src-cachenote";
        n.style.cssText = "background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:9px 13px;font-size:13px;color:#3730a3;margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;line-height:1.5;";
        n.innerHTML = "<span>💾 이전에 탐색해 둔 결과를 바로 불러왔습니다 (검색 한도 소모 없음" + (j.cached_at ? " · " + fmtDate(j.cached_at) + " 탐색" : "") + ")</span>" +
          (srcAdmin ? "<button id='src-force' style='padding:5px 13px;border-radius:9px;border:1.5px solid #4f46e5;background:#fff;color:#4f46e5;font-size:12.5px;font-weight:800;cursor:pointer;font-family:inherit;'>🔄 새로 탐색 (관리자)</button>" : "");
        host.insertAdjacentElement("afterbegin", n);
        const fb = n.querySelector("#src-force");
        if (fb) fb.addEventListener("click", function () { forceSources(j.video_id, fb); });
      }

      // 1) 해외 플랫폼 직접 검색 (검색어가 미리 입력된 바로가기)
      const qs = (j.yt_links || []).slice(0, 3);
      if (qs.length) {
        const div = document.createElement("div");
        div.className = "intl-box"; div.id = "src-intl";
        div.innerHTML = "<div class='ib-t'>🌏 해외 플랫폼에서 직접 검색 <span style='font-weight:600;color:#a16207;'>— 해외 쇼츠는 도우인·샤오홍슈에서 온 경우가 많아요</span></div>" +
          qs.map(function (q, qi) {
            return "<div class='intl-row'>" + (qi === 0 ? "<span style='background:#4f46e5;color:#fff;font-size:10.5px;font-weight:800;border-radius:999px;padding:2px 8px;margin-right:6px'>대표</span>" : "") + "<span class='iq'>「" + escapeHtml(q.q) + "」</span>" + (q.type && ({watermark:"워터마크",entity:"인물·사건",quote:"대사·자막",visual:"장면 묘사",source:"원본 표현",full_version:"전체본"}[q.type] || "") ? "<span style='background:#fde68a;color:#92400e;font-size:10.5px;font-weight:700;border-radius:999px;padding:2px 7px;margin-left:6px'>" + ({watermark:"워터마크",entity:"인물·사건",quote:"대사·자막",visual:"장면 묘사",source:"원본 표현",full_version:"전체본"}[q.type] || "") + "</span>" : "") + (q.ko && q.lang !== "ko" ? "<span style='color:#8a8a8a;font-size:11.5px;margin-left:6px'>(" + escapeHtml(q.ko) + ")</span>" : "") + "<br>" +
              PLATFORMS.map(function (p) { return "<a href='" + p[1](q.q) + "' target='_blank' rel='noopener'>" + p[0] + "</a>"; }).join("") + "</div>";
          }).join("") +
          "<div class='intl-warn'>⚠️ 해외 영상은 참고·재구성용입니다. 그대로 내려받아 쓰면 저작권 문제가 될 수 있어요.</div>";
        host.appendChild(div);
      }

      // 1.5) 재사용 가능(CC) 영상 찾기 — 합법적으로 편집·재사용할 수 있는 재료
      if (qs.length) {
        const old3 = document.getElementById("src-cc"); if (old3) old3.remove();
        const cc = document.createElement("div");
        cc.className = "cc-box"; cc.id = "src-cc";
        cc.innerHTML = "<div class='ib-t'>♻️ 재사용 가능(CC) 영상 찾기 <span style='font-weight:600;color:#4d7c0f;'>— 제작자가 재사용을 허락한 영상만 골라 검색합니다</span></div>" +
          qs.map(function (q, i) {
            return "<div class='cc-row'><span class='iq'>「" + escapeHtml(q.q) + "」</span> " +
              "<a href='https://www.youtube.com/results?search_query=" + encodeURIComponent(q.q) + "&sp=EgIwAQ%253D%253D' target='_blank' rel='noopener'>유튜브에서 CC 검색 열기</a>" +
              "<button data-q='" + escapeHtml(q.q) + "' data-i='" + i + "'>🖼️ 화면 안에서 보기</button>" +
              "<div class='cc-results' id='cc-res-" + i + "'></div></div>";
          }).join("") +
          "<div class='cc-note'>✅ CC(크리에이티브 커먼즈) 영상은 <b>출처 표기</b>(영상 설명란에 원작자 채널명·원본 링크)를 하면 합법적으로 편집·재사용할 수 있습니다.<br>⚠️ 사용 전 해당 영상 페이지의 라이선스 표시가 정말 '크리에이티브 커먼즈'인지 한 번 더 확인하세요. 일반 영상의 무단 재사용은 저작권 침해입니다.</div>";
        host.appendChild(cc);
        cc.querySelectorAll("button[data-q]").forEach(function (b) {
          b.addEventListener("click", function () { runCcSearch(b.dataset.q, "cc-res-" + b.dataset.i, b); });
        });
      }

      // 2) 구글렌즈로 원본 찾기 (썸네일이 이미 업로드된 상태로 열림)
      const vid = j.video_id;
      if (vid) {
        const thumb = "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg";
        const div2 = document.createElement("div");
        div2.className = "lens-box"; div2.id = "src-lens";
        div2.innerHTML = "<div class='ib-t'>🔍 웹 이미지로 원본 찾기 (역검색)</div>" +
          "<a class='lens-btn' href='https://lens.google.com/uploadbyurl?url=" + encodeURIComponent(thumb) + "' target='_blank' rel='noopener'>Google Lens에서 직접 확인 →</a> " +
          "<button class='lens-btn' id='lens-run' style='border:none;cursor:pointer;font-family:inherit;background:#1e40af;'>🖼️ 웹 이미지 일치 후보 — Google Vision</button>" +
          "<div class='prev-grid' id='lens-grid' style='grid-template-columns:repeat(2,1fr);'></div>" +
          "<div class='lens-tip'>썸네일과 똑같거나 비슷한 이미지가 있는 페이지를 구글이 찾아줍니다.<br>💡 영상 속 <b>특정 장면</b>으로 찾고 싶다면: 그 장면에서 일시정지 → 스크린샷 → <a href='https://lens.google.com/' target='_blank' rel='noopener' style='color:#1d4ed8;'>lens.google.com</a>에 직접 올려보세요.</div>";
        host.appendChild(div2);
        const runBtn = div2.querySelector("#lens-run");
        if (runBtn) runBtn.addEventListener("click", function () { runLens(vid); });
      }
    }
  }

  // CC(재사용 가능) 영상 검색 — 클릭한 검색어만 조회
  async function runCcSearch(q, containerId, btn) {
    const box = document.getElementById(containerId);
    if (!box) return;
    if (box.innerHTML) { box.innerHTML = ""; return; } // 다시 누르면 접기
    if (btn) btn.disabled = true;
    box.innerHTML = "<div style='font-size:12.5px;color:#15803d;padding:5px 0;'>재사용 가능 영상을 찾는 중…</div>";
    try {
      const { data } = await sb.auth.getSession();
      const token = data && data.session ? data.session.access_token : "";
      const r = await fetch("/api/ai", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: JSON.stringify({ feature: "source_finder", action: "cc_search", query: q }),
      });
      const j = await r.json().catch(function () { return {}; });
      if (!r.ok || !j.ok) {
        box.innerHTML = "<div style='font-size:12.5px;color:#991b1b;padding:5px 0;'>검색 실패: " + escapeHtml(j.error || "HTTP " + r.status) + (j.detail ? " — " + escapeHtml(j.detail) : "") + "</div>";
      } else if (!j.available) {
        box.innerHTML = "<div style='font-size:12.5px;color:#15803d;padding:5px 0;'>화면 안 검색은 준비 중입니다. 'CC 검색 열기' 링크를 이용해 주세요.</div>";
      } else if (!j.items.length) {
        box.innerHTML = "<div style='font-size:12.5px;color:#15803d;padding:5px 0;'>이 검색어로는 재사용 가능 영상이 없습니다. 다른 검색어로 시도하거나 링크로 직접 찾아보세요.</div>";
      } else {
        box.innerHTML = j.items.map(function (it) {
          return "<a class='cc-card' href='https://www.youtube.com/watch?v=" + escapeHtml(it.video_id) + "' target='_blank' rel='noopener'>" +
            "<img src='https://i.ytimg.com/vi/" + escapeHtml(it.video_id) + "/mqdefault.jpg' alt='' loading='lazy'>" +
            "<span><span class='cc-t'>" + escapeHtml(it.title) + "</span><br><span class='cc-m'>" + escapeHtml(it.channel || "") + (it.published_at ? " · " + fmtDate(it.published_at) : "") + " · ♻️ 재사용 허용</span></span></a>";
        }).join("");
      }
    } catch (e) {
      box.innerHTML = "<div style='font-size:12.5px;color:#991b1b;padding:5px 0;'>요청이 실패했습니다: " + escapeHtml(e.message) + "</div>";
    }
    if (btn) btn.disabled = false;
  }

  // 구글렌즈 2단계: Vision API 결과를 화면 안 그리드로 (키 없으면 안내만)
  async function runLens(vid) {
    const box = document.getElementById("lens-grid");
    if (!box) return;
    if (box.className.indexOf("show") >= 0) { box.className = "prev-grid"; return; } // 다시 누르면 접기
    box.innerHTML = "<div class='prev-msg' style='grid-column:1/3;'>구글에서 이 썸네일을 찾는 중…</div>";
    box.className = "prev-grid show";
    try {
      const { data } = await sb.auth.getSession();
      const token = data && data.session ? data.session.access_token : "";
      const r = await fetch("/api/ai", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: JSON.stringify({ feature: "source_finder", action: "lens", video_url: "https://www.youtube.com/watch?v=" + vid }),
      });
      const j = await r.json().catch(function () { return {}; });
      if (!r.ok || !j.ok) {
        box.innerHTML = "<div class='prev-msg' style='grid-column:1/3;'>렌즈 검색 실패: " + escapeHtml(j.error || "HTTP " + r.status) + (j.detail ? " — " + escapeHtml(j.detail) : "") + "</div>";
        return;
      }
      if (!j.available) {
        box.innerHTML = "<div class='prev-msg' style='grid-column:1/3;'>🖼️ 화면 안 결과 보기는 준비 중입니다. 위의 '렌즈 검색 열기' 버튼을 이용해 주세요.</div>";
        return;
      }
      let html = "";
      if (Array.isArray(j.labels) && j.labels.length) {
        html += "<div style='grid-column:1/3;font-size:12.5px;font-weight:700;color:#1e40af;'>구글의 추측: " + j.labels.map(escapeHtml).join(", ") + "</div>";
      }
      const pages = Array.isArray(j.pages) ? j.pages.filter(function (p) { return p.url; }) : [];
      if (pages.length) {
        html += "<div style='grid-column:1/3;font-size:12px;font-weight:800;color:#64748b;margin-top:4px;'>📄 이 이미지가 실린 페이지 (원본일 가능성)</div>";
        html += pages.slice(0, 6).map(function (p) {
          return "<a href='" + escapeHtml(p.url) + "' target='_blank' rel='noopener' style='display:flex;gap:8px;align-items:center;background:#fff;border:1px solid #bfdbfe;border-radius:9px;padding:7px 9px;text-decoration:none;grid-column:1/3;'>" +
            (p.img ? "<img src='" + escapeHtml(p.img) + "' alt='' loading='lazy' style='width:64px;height:40px;object-fit:cover;border-radius:6px;background:#000;flex-shrink:0;' onerror=\"this.style.display='none'\">" : "") +
            "<span style='font-size:12.5px;font-weight:700;color:#1e293b;line-height:1.4;overflow:hidden;'>" + escapeHtml(p.title || p.url) + "<br><span style='color:#64748b;font-weight:600;font-size:11px;'>" + escapeHtml((function () { try { return new URL(p.url).hostname; } catch { return ""; } })()) + "</span></span></a>";
        }).join("");
      }
      const items = Array.isArray(j.items) ? j.items.filter(function (it) { return it.img; }) : [];
      if (items.length) {
        html += "<div style='grid-column:1/3;font-size:12px;font-weight:800;color:#64748b;margin-top:4px;'>🖼️ 똑같거나 비슷한 이미지</div>";
        html += items.slice(0, 8).map(function (it) {
          return "<a href='" + escapeHtml(it.img) + "' target='_blank' rel='noopener'><img src='" + escapeHtml(it.img) + "' alt='' loading='lazy' onerror=\"this.parentNode.style.display='none'\"><span class='pv-d'>" + escapeHtml(it.kind || "") + "</span></a>";
        }).join("");
      }
      if (!pages.length && !items.length) {
        html += "<div class='prev-msg' style='grid-column:1/3;'>일치하는 결과를 찾지 못했습니다. '렌즈 검색 열기' 버튼으로 직접 확인해 보세요.</div>";
      }
      box.innerHTML = html + "<div class='prev-note' style='grid-column:1/3;'>출처: Google Vision 웹 감지 · 결과를 누르면 해당 페이지가 열립니다</div>";
    } catch (e) {
      box.innerHTML = "<div class='prev-msg' style='grid-column:1/3;'>요청이 실패했습니다: " + escapeHtml(e.message) + "</div>";
    }
  }

  // ---------------- 새 후킹 유형 후보 (lab.html, 관리자) ----------------
  function initLab() {
    if (location.pathname.indexOf("lab.html") < 0) return;
    (async function () {
      try {
        const u = await getUser();
        if (!u) return;
        if (!(await isAdmin(u))) return;
        const { data } = await sb.from("hook_extra_log").select("*").order("created_at", { ascending: false }).limit(50);
        const wrap = document.querySelector(".wrap") || document.querySelector("main") || document.body;
        const card = document.createElement("div");
        card.style.cssText = "background:#fff;border:1px solid var(--line,#e2e8f0);border-radius:14px;padding:16px 18px;margin-top:16px;";
        const rows = (data || []).map(function (r) {
          return "<div style='padding:9px 0;border-top:1px solid #e2e8f0;font-size:13.5px;line-height:1.6;'>" +
            "<b>" + escapeHtml(r.name || "(이름 없음)") + "</b> · <a href='https://www.youtube.com/watch?v=" + escapeHtml(r.video_id) + "' target='_blank' rel='noopener' style='color:#2563eb;'>영상 보기</a>" +
            " <span style='color:#94a3b8;font-size:12px;'>" + fmtDate(r.created_at) + "</span>" +
            (r.description ? "<br><span style='color:#475569;'>" + escapeHtml(r.description) + "</span>" : "") + "</div>";
        }).join("");
        card.innerHTML = "<div style='font-size:15.5px;font-weight:800;margin-bottom:4px;'>🆕 새 후킹 유형 후보 <span style='font-size:12px;color:#94a3b8;font-weight:600;'>AI가 10유형에 없다고 판정한 사례 — 반복되는 패턴은 11번째 유형으로 승격하세요</span></div>" +
          (rows || "<div style='padding:10px 0;color:#64748b;font-size:13.5px;'>아직 수집된 사례가 없습니다. 후킹 분석에서 '새 유형' 판정이 나오면 여기에 자동으로 쌓입니다.</div>");
        wrap.appendChild(card);
      } catch {}
    })();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initHook(); initSources(); initLab(); });
  } else {
    initHook(); initSources(); initLab();
  }
})();





/* ===== 인비남 AI 이미지 검색 — 장면 5장 + 바이두/구글 + 토탈 검색 (전체 회원용) ===== */
/* 되돌리기: IB_HIDE_OLD 를 false 로 바꾸면 기존 바이두식별/렌즈/비전 버튼이 다시 보입니다. */
;(function(){
  if (window.__ibFrames5Init) return; window.__ibFrames5Init = true;
  var IB_HIDE_OLD = true;

  function sessionToken(){
    try{
      var ks = Object.keys(localStorage).filter(function(k){ return /auth-token|sb-.*-auth/.test(k); });
      for (var i=0;i<ks.length;i++){
        try{ var o = JSON.parse(localStorage.getItem(ks[i])); if (o && o.access_token) return o.access_token; }catch(e){}
      }
    }catch(e){}
    return null;
  }

  function esc(x){ return String(x==null?"":x).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

  function toast(msg){
    try{ if (typeof window.ibToast === "function") return window.ibToast(msg); }catch(e){}
    var d = document.createElement("div");
    d.textContent = msg;
    d.style.cssText = "position:fixed;left:50%;bottom:40px;transform:translateX(-50%);background:#111;color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;z-index:99999;max-width:86%;text-align:center";
    document.body.appendChild(d);
    setTimeout(function(){ try{ d.remove(); }catch(e){} }, 3500);
  }

  function currentVideoUrl(){
    var inps = [].slice.call(document.querySelectorAll("input"));
    for (var i=0;i<inps.length;i++){
      var v = (inps[i].value || "").trim();
      if (/youtu\.?be|youtube\.com/.test(v)) return v;
    }
    var imgs = [].slice.call(document.querySelectorAll("img"));
    for (var j=0;j<imgs.length;j++){
      var m = (imgs[j].src || "").match(/\/vi\/([A-Za-z0-9_\-]{11})\//);
      if (m) return "https://www.youtube.com/watch?v=" + m[1];
    }
    return "";
  }
  function currentVid(){
    var u = currentVideoUrl();
    var m = u.match(/[?&]v=([A-Za-z0-9_\-]{11})/) || u.match(/youtu\.be\/([A-Za-z0-9_\-]{11})/) || u.match(/shorts\/([A-Za-z0-9_\-]{11})/);
    return m ? m[1] : null;
  }

  var FRAME_DEFS = [
    { key:"oardefault", fb:"hqdefault", label:"대표" },
    { key:"frame0",     fb:null,        label:"첫장면" },
    { key:"oar1",       fb:"hq1",       label:"앞부분" },
    { key:"oar2",       fb:"hq2",       label:"중간" },
    { key:"oar3",       fb:"hq3",       label:"뒷부분" }
  ];
  function probeImg(u){
    return new Promise(function(res){
      try{
        var im = new Image();
        var done = false;
        im.onload = function(){ if(!done){ done=true; res({ok:true, w:im.naturalWidth}); } };
        im.onerror = function(){ if(!done){ done=true; res({ok:false}); } };
        im.src = u;
        setTimeout(function(){ if(!done){ done=true; res({ok:false}); } }, 7000);
      }catch(e){ res({ok:false}); }
    });
  }
  var frameCache = {};
  function getFrames(vid){
    if (frameCache[vid]) return frameCache[vid];
    frameCache[vid] = Promise.all(FRAME_DEFS.map(function(d){
      var base = "https://i.ytimg.com/vi/" + vid + "/";
      return probeImg(base + d.key + ".jpg").then(function(r){
        if (r.ok && r.w >= 100) return { url: base + d.key + ".jpg", label: d.label };
        if (!d.fb) return null;
        return probeImg(base + d.fb + ".jpg").then(function(r2){
          if (r2.ok && r2.w >= 100) return { url: base + d.fb + ".jpg", label: d.label };
          return null;
        });
      });
    })).then(function(list){ return list.filter(Boolean); });
    return frameCache[vid];
  }

  // 복사 완료 후 바이두 열기 (순서 중요)
  async function copyFrameOpenBaidu(url){
    var copied = false;
    try{
      var im = await new Promise(function(res, rej){
        var i = new Image();
        i.crossOrigin = "anonymous";
        i.onload = function(){ res(i); };
        i.onerror = function(){ rej(new Error("load")); };
        i.src = url + (url.indexOf("?") < 0 ? "?cors=1" : "&cors=1");
        setTimeout(function(){ rej(new Error("timeout")); }, 8000);
      });
      var cv = document.createElement("canvas");
      cv.width = im.naturalWidth; cv.height = im.naturalHeight;
      cv.getContext("2d").drawImage(im, 0, 0);
      var bb = await new Promise(function(res){ try{ cv.toBlob(function(b){ res(b); }, "image/png"); }catch(e){ res(null); } });
      if (bb && typeof ClipboardItem !== "undefined"){
        await navigator.clipboard.write([new ClipboardItem({ "image/png": bb })]);
        copied = true;
      }
    }catch(e){}
    if (copied){
      toast("📸 장면 이미지가 복사됐어요! 열린 바이두 페이지에서 Ctrl+V(붙여넣기) 하세요.");
    } else {
      try{ await navigator.clipboard.writeText(url); }catch(e){}
      toast("이미지 주소를 복사했어요. 바이두 입력창에 붙여넣고 [识图一下]를 누르세요.");
    }
    window.open("https://graph.baidu.com/pcpage/index?tpl_from=pc", "_blank");
  }

  var NO_RESULT_MSG = "해당 영상의 원본 소스는 이곳에 없을 확률이 매우 높습니다.";

  var baiduCache = {};
  async function baiduSearch(imageUrl){
    if (baiduCache[imageUrl]) return baiduCache[imageUrl];
    var r = await fetch("/api/ai", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + sessionToken() },
      body: JSON.stringify({ feature: "source_finder", action: "baidu_sim", video_url: currentVideoUrl(), image_url: imageUrl })
    });
    var j = null; try{ j = await r.json(); }catch(e){}
    if (j && j.ok === true) baiduCache[imageUrl] = j;
    if (!j) j = { ok:false, error: "HTTP " + r.status };
    return j;
  }

  var visionCache = {};
  async function visionSearch(imageUrl){
    if (visionCache[imageUrl]) return visionCache[imageUrl];
    var r = await fetch("/api/ai", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + sessionToken() },
      body: JSON.stringify({ feature: "source_finder", action: "lens", video_url: currentVideoUrl(), image_url: imageUrl })
    });
    var j = null; try{ j = await r.json(); }catch(e){}
    if (j && j.ok === true) visionCache[imageUrl] = j;
    if (!j) j = { ok:false, error: "HTTP " + r.status };
    return j;
  }

  function platformLabel(x){
    var s = String(x.site || "");
    var u = String(x.url || "");
    function has(w){ return s.indexOf(w) >= 0 || u.indexOf(w) >= 0; }
    if (has("抖音") || has("douyin")) return "더우인";
    if (has("度小视") || has("quanmin.baidu")) return "바이두 영상";
    if (has("好看") || has("haokan.baidu")) return "하오칸 영상";
    if (has("微博") || has("weibo")) return "웨이보";
    if (has("快手") || has("kuaishou")) return "콰이쇼우";
    if (has("哔哩") || has("bilibili")) return "비리비리";
    if (has("西瓜") || has("ixigua")) return "시과 영상";
    if (has("小红书") || has("xiaohongshu")) return "샤오홍슈";
    if (has("腾讯") || has("v.qq.com")) return "텐센트 영상";
    if (has("优酷") || has("youku")) return "유큐";
    if (has("youtube") || has("youtu.be")) return "유튜브";
    if (has("baidu")) return "바이두";
    try{ return new URL(u).hostname.replace(/^www\./, ""); }catch(e){ return "출처 보기"; }
  }

  function gridCard(x, big){
    var w = big ? 110 : 82, h = big ? 195 : 146;
    var plat = esc(platformLabel(x));
    var scene = big && x.__scene ? '<div style="font-size:11px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(x.__scene) + ' 사진에서 발견</div>' : "";
    var inner = x.img
      ? '<img src="' + esc(x.img) + '" referrerpolicy="no-referrer" loading="lazy" style="width:100%;height:100%;object-fit:cover">'
      : '<div style="display:flex;align-items:center;justify-content:center;height:100%;font-size:11px;color:#94a3b8;padding:4px;text-align:center;word-break:break-all">' + plat + '</div>';
    return '<a href="' + esc(x.url || "#") + '" target="_blank" rel="noopener noreferrer" style="display:block;width:' + w + 'px;text-decoration:none;color:inherit;flex:none">'
      + '<div style="width:' + w + 'px;height:' + h + 'px;border-radius:10px;background:#f1f5f9;overflow:hidden;border:1px solid #e2e8f0">' + inner + '</div>'
      + '<div style="font-size:' + (big ? 12 : 11) + 'px;font-weight:600;color:#1e40af;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + plat + '</div>'
      + scene + '</a>';
  }

  // ── 토탈 검색 결과 화면: 바이두 동일원본 → 구글 발견 → 유사 ──
  function renderTotal(baiduGroups, googlePages, out, note){
    var seen = {}, all = [];
    baiduGroups.forEach(function(g){
      (g.items || []).forEach(function(it){
        var key = String(it.url || "").trim();
        if (!key) return;
        if (seen[key]){
          if (it.cate === "CATE_SAME" && seen[key].cate !== "CATE_SAME"){ seen[key].cate = "CATE_SAME"; seen[key].__scene = g.label; }
          return;
        }
        var c = { cate: it.cate, url: it.url, img: it.img, site: it.site, __scene: g.label };
        seen[key] = c; all.push(c);
      });
    });
    var same = all.filter(function(x){ return x.cate === "CATE_SAME"; });
    var simi = all.filter(function(x){ return x.cate !== "CATE_SAME"; });
    var gseen = {}, google = [];
    (googlePages || []).forEach(function(pg){
      var key = String(pg.url || "").trim();
      if (!key || gseen[key] || seen[key]) return;
      gseen[key] = true;
      google.push({ url: pg.url, img: pg.img, site: "", __scene: pg.__scene });
    });
    if (!all.length && !google.length){
      out.innerHTML = '<div style="padding:12px;color:#64748b;font-size:13px;background:#f8fafc;border-radius:8px">' + esc(NO_RESULT_MSG) + '</div>' + (note || "");
      return;
    }
    var html = '<div style="font-weight:700;font-size:14px;margin:6px 0 10px">🔍 인비남 AI 토탈 검색 결과 — '
      + '<span style="color:#e74c3c">동일 원본 ' + same.length + '건</span> · '
      + '<span style="color:#2563eb">구글 발견 ' + google.length + '건</span> · '
      + '<span style="color:#f39c12">유사 ' + simi.length + '건</span>'
      + ' <span style="font-size:11px;color:#94a3b8;font-weight:400">같은 주소 중복만 제거됨</span></div>';
    if (same.length){
      html += '<div style="font-size:13px;font-weight:700;color:#e74c3c;margin-bottom:6px">🔴 동일 원본 판정 (' + same.length + '건) — 사진을 누르면 해당 페이지가 열립니다</div>';
      html += '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px">' + same.map(function(x){ return gridCard(x, true); }).join("") + '</div>';
      html += '<div style="font-size:11.5px;color:#94a3b8;margin-bottom:10px">⚠ 주소에 따라 영상이 아닌 글(기사·블로그)일 수 있어요. 직접 눌러 원본 영상인지 확인하세요.</div>';
    } else {
      html += '<div style="padding:10px;color:#64748b;font-size:13px;background:#f8fafc;border-radius:8px;margin-bottom:10px">바이두 동일 원본 판정이 없습니다. ' + esc(NO_RESULT_MSG) + '</div>';
    }
    if (google.length){
      html += '<div style="font-size:13px;font-weight:700;color:#2563eb;margin:10px 0 6px">🔵 구글이 찾은 페이지 (' + google.length + '건) — 해외 원본일 가능성</div>';
      html += '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px">' + google.map(function(x){ return gridCard(x, true); }).join("") + '</div>';
    }
    if (simi.length){
      var open = same.length === 0 && google.length === 0;
      html += '<button type="button" class="ib-simi-tg" style="display:block;width:100%;padding:8px;border:1px dashed #f39c12;background:#fffbeb;color:#92400e;border-radius:8px;font-size:12.5px;cursor:pointer;font-family:inherit;margin-top:6px">🟡 유사 영상 ' + simi.length + '건 ' + (open ? "접기 ▴" : "펼쳐보기 ▾") + '</button>';
      html += '<div class="ib-simi-grid" style="display:' + (open ? "flex" : "none") + ';flex-wrap:wrap;gap:8px;margin-top:8px">' + simi.map(function(x){ return gridCard(x, false); }).join("") + '</div>';
    }
    html += (note || "") + '<div style="font-size:11px;color:#b5b5b5;margin-top:8px">바이두 식별 + 구글 비전 기술 제공 · 판정은 참고용입니다.</div>';
    out.innerHTML = html;
    var tgb = out.querySelector(".ib-simi-tg");
    if (tgb){
      tgb.addEventListener("click", function(){
        var g2 = out.querySelector(".ib-simi-grid");
        var vis = g2.style.display !== "none";
        g2.style.display = vis ? "none" : "flex";
        tgb.textContent = "🟡 유사 영상 " + simi.length + "건 " + (vis ? "펼쳐보기 ▾" : "접기 ▴");
      });
    }
  }

  // ── 토탈 검색 실행 (바이두 5장 + 구글 5장) ──
  async function totalSearch(frames, out){
    var groups = [], gpages = [], failB = 0, failG = 0;
    var total = frames.length * 2, step = 0;
    function prog(msg){ out.innerHTML = '<div style="padding:10px;color:#888;font-size:13px">🔎 ' + step + '/' + total + ' — ' + msg + ' (총 1분 정도 걸릴 수 있어요)</div>'; }
    for (var i=0;i<frames.length;i++){
      step++; prog(esc(frames[i].label) + " 사진을 바이두에서 검색 중…");
      var j = null;
      try{ j = await baiduSearch(frames[i].url); }catch(e){}
      if (!j || j.ok !== true) failB++;
      groups.push({ label: frames[i].label, items: (j && j.ok === true && j.items) ? j.items : [] });
    }
    for (var k=0;k<frames.length;k++){
      step++; prog(esc(frames[k].label) + " 사진을 구글에서 검색 중…");
      var v = null;
      try{ v = await visionSearch(frames[k].url); }catch(e){}
      if (v && v.ok === true && Array.isArray(v.pages)){
        v.pages.forEach(function(pg){ if (pg && pg.url) gpages.push({ url: pg.url, img: pg.img, __scene: frames[k].label }); });
      } else { failG++; }
    }
    var note = "";
    if (failB === frames.length && failG === frames.length){
      out.innerHTML = '<div style="padding:10px;color:#c0392b;font-size:13px">검색이 모두 실패했습니다. 잠시 후 다시 시도해 주세요.</div>';
      return;
    }
    if (failB === frames.length) note = '<div style="font-size:11.5px;color:#c0392b;margin-top:6px">⚠ 바이두 검색은 실패해서 구글 결과만 표시됩니다.</div>';
    if (failG === frames.length) note = '<div style="font-size:11.5px;color:#c0392b;margin-top:6px">⚠ 구글 검색은 실패해서 바이두 결과만 표시됩니다.</div>';
    renderTotal(groups, gpages, out, note);
  }

  // ── 통합 검색 박스 만들기 ──
  function buildTotalBox(){
    var box = document.createElement("div");
    box.className = "ib-total-box";
    box.style.cssText = "margin-top:10px;border:1px solid #c7d2fe;background:#f5f7ff;border-radius:12px;padding:12px";
    var title = document.createElement("div");
    title.textContent = "📸 인비남 AI 이미지 검색";
    title.style.cssText = "font-size:14px;font-weight:800;color:#312e81;margin-bottom:2px";
    var sub = document.createElement("div");
    sub.textContent = "사진 아래 [바이두]·[구글]을 누르면 그 장면으로 바로 검색됩니다. 바이두는 복사 후 열린 페이지에서 Ctrl+V!";
    sub.style.cssText = "font-size:11.5px;color:#64748b;margin-bottom:9px";
    var row = document.createElement("div");
    row.style.cssText = "display:flex;gap:10px;overflow-x:auto;padding:2px 0";
    var totalBtn = document.createElement("button");
    totalBtn.type = "button";
    totalBtn.textContent = "🔍 인비남 AI 토탈 검색 — 5장으로 바이두+구글 전부 찾아보기";
    totalBtn.style.cssText = "display:block;width:100%;margin-top:10px;padding:11px;border:none;background:#4338ca;color:#fff;border-radius:9px;font-size:13.5px;font-weight:700;cursor:pointer;font-family:inherit";
    var out = document.createElement("div");
    out.style.cssText = "margin-top:10px";
    box.appendChild(title); box.appendChild(sub); box.appendChild(row); box.appendChild(totalBtn); box.appendChild(out);
    var vid = currentVid();
    if (!vid){ row.innerHTML = '<div style="font-size:12.5px;color:#c0392b">영상 주소를 찾지 못했습니다.</div>'; return box; }
    row.innerHTML = '<div style="font-size:12.5px;color:#888">장면 사진을 불러오는 중…</div>';
    var framesReady = null;
    getFrames(vid).then(function(frames){
      framesReady = frames;
      row.innerHTML = "";
      if (!frames.length){ row.innerHTML = '<div style="font-size:12.5px;color:#c0392b">이 영상은 장면 사진을 제공하지 않습니다.</div>'; return; }
      frames.forEach(function(f){
        var cell = document.createElement("div");
        cell.style.cssText = "flex:none;width:86px;text-align:center";
        var im = document.createElement("img");
        im.src = f.url; im.loading = "lazy";
        im.style.cssText = "width:84px;height:149px;object-fit:cover;border-radius:9px;background:#eef2f7;border:1px solid #e2e8f0";
        var lb = document.createElement("div");
        lb.textContent = f.label;
        lb.style.cssText = "font-size:11.5px;font-weight:600;color:#334155;margin:3px 0 4px";
        var btns = document.createElement("div");
        btns.style.cssText = "display:flex;gap:4px";
        var bB = document.createElement("button");
        bB.type = "button"; bB.textContent = "바이두";
        bB.style.cssText = "flex:1;padding:5px 0;border:none;background:#2c6fbb;color:#fff;border-radius:7px;font-size:11.5px;font-weight:700;cursor:pointer;font-family:inherit";
        bB.addEventListener("click", function(){ copyFrameOpenBaidu(f.url); });
        var bG = document.createElement("button");
        bG.type = "button"; bG.textContent = "구글";
        bG.style.cssText = "flex:1;padding:5px 0;border:1px solid #cbd5e1;background:#fff;color:#1f2937;border-radius:7px;font-size:11.5px;font-weight:700;cursor:pointer;font-family:inherit";
        bG.addEventListener("click", function(){ window.open("https://lens.google.com/uploadbyurl?url=" + encodeURIComponent(f.url), "_blank"); });
        btns.appendChild(bB); btns.appendChild(bG);
        cell.appendChild(im); cell.appendChild(lb); cell.appendChild(btns);
        row.appendChild(cell);
      });
    });
    totalBtn.addEventListener("click", function(){
      if (!framesReady || !framesReady.length){ out.innerHTML = '<div style="padding:8px;color:#c0392b;font-size:12.5px">장면 사진이 아직 준비되지 않았습니다.</div>'; return; }
      totalBtn.disabled = true;
      totalSearch(framesReady, out).then(function(){ totalBtn.disabled = false; }).catch(function(){ totalBtn.disabled = false; });
    });
    return box;
  }

  // ── 부착 + 기존 버튼 숨기기 ──
  function attachAll(){
    var shitu = [].slice.call(document.querySelectorAll("button, a")).filter(function(el){
      if (!/识图/.test(el.textContent || "")) return false;
      var inner = el.querySelector("button, a");
      if (inner && /识图/.test(inner.textContent || "")) return false;
      return true;
    });
    shitu.forEach(function(el){
      if (!el.__ibF6){
        el.__ibF6 = true;
        el.insertAdjacentElement("afterend", buildTotalBox());
      }
      if (IB_HIDE_OLD) el.style.display = "none";
    });
    if (IB_HIDE_OLD){
      [].slice.call(document.querySelectorAll('a[href*="lens.google.com/uploadbyurl"]')).forEach(function(a){ a.style.display = "none"; });
      [].slice.call(document.querySelectorAll("button")).forEach(function(b){
        if (/웹 이미지 일치 후보/.test(b.textContent || "")) b.style.display = "none";
      });
    }
  }

  var pending = false;
  var mo = new MutationObserver(function(){
    if (pending) return; pending = true;
    setTimeout(function(){ pending = false; try{ attachAll(); }catch(e){} }, 200);
  });
  try{ mo.observe(document.documentElement, { childList: true, subtree: true }); }catch(e){}
  try{ attachAll(); }catch(e){}
})();
