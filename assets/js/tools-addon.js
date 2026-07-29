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
        var row = a.closest(".intl-row"); var q = "";
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
      b.style.cssText = "border:none;cursor:pointer;font-family:inherit;margin-top:6px";
      b.textContent = "🅑 바이두 이미지 역검색 (주소 복사)";
      b.addEventListener("click", function () {
        try { navigator.clipboard.writeText(thumb); } catch (_) {}
        ibToast("이미지 주소를 복사했어요. 바이두 이미지검색에서 카메라 아이콘 → 붙여넣기 하세요");
        window.open("https://image.baidu.com/", "_blank");
      });
      lens.appendChild(b);
    }
  }
  function ibEnrichPreview(j){
    var cards = (j && j.candidates) || [];
    var host = document.getElementById("src-cards");
    if (!host) return;
    var oldSum = document.getElementById("ib-prev-sum");
    if (oldSum) oldSum.remove();
    if (!document.getElementById("ib-prev-style")) {
      var st = document.createElement("style");
      st.id = "ib-prev-style";
      st.textContent = ".ib-badges{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}.ib-b{font-size:11px;padding:2px 7px;border-radius:999px;background:var(--line,#eee);color:var(--sub,#555);white-space:nowrap}.ib-short{background:#ffe8d6;color:#b5480a}.ib-long{background:#e6ecff;color:#274bd1}.ib-embed{background:#e4f7e8;color:#1c8a3b}.ib-sum{font-size:12px;color:var(--sub,#666);margin:2px 0 8px}";
      document.head.appendChild(st);
    }
    var nodes = host.querySelectorAll(".src-card");
    var nShort = 0, nLong = 0, nUnknown = 0;
    for (var i = 0; i < nodes.length && i < cards.length; i++) {
      var c = cards[i], node = nodes[i];
      if (!node || node.querySelector(".ib-badges")) continue;
      var badges = [];
      var hasDur = typeof c.duration_seconds === "number" && c.duration_seconds > 0;
      if (hasDur) badges.push('<span class="ib-b">⏱ ' + (c.duration_label || "") + '</span>');
      if (typeof c.view_count === "number" && c.view_count > 0) badges.push('<span class="ib-b">👁 ' + fmtV(c.view_count) + '</span>');
      if (c.is_short_candidate) { badges.push('<span class="ib-b ib-short">쇼츠 후보' + (c.short_confidence ? (' · 확신 ' + c.short_confidence) : '') + '</span>'); nShort++; }
      else if (hasDur) { badges.push('<span class="ib-b ib-long">긴 원본 후보</span>'); nLong++; }
      else { nUnknown++; }
      if (c.found_by) { var fbq = String(c.found_by).replace(/[<>&"\']/g, "").slice(0, 24); badges.push('<span class="ib-b">🔎 「' + fbq + '」로 발견</span>'); }
      if (c.embeddable) badges.push('<span class="ib-b ib-embed">인비랩 재생 가능</span>');
      if (badges.length) {
        var d = document.createElement("div");
        d.className = "ib-badges";
        d.innerHTML = badges.join(" ");
        var info = node.querySelector(".sc-info") || node;
        info.appendChild(d);
      }
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
        try { ibSmartLinks(j); } catch (e) {} } catch {}
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
          qs.map(function (q) {
            return "<div class='intl-row'><span class='iq'>「" + escapeHtml(q.q) + "」</span><br>" +
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
