/*
 * Тёс — встраиваемый виджет ИИ-консультанта «Азбука Леса».
 * Подключается одной строкой на любом сайте:
 *   <script src="https://<host>/static/widget.js" defer></script>
 *
 * Сам определяет свой origin из src этого скрипта → туда же шлёт /chat, /stt.
 * Вся вёрстка и стили заскоуплены под #tyos-root, чтобы не задеть сайт-хозяин.
 * Имя в кнопке/шапке = «ИИ-консультант» (ФЗ-53), «Тёс» — самопредставление в диалоге.
 */
(function () {
  if (window.__tyosWidgetLoaded) return;
  window.__tyosWidgetLoaded = true;

  // origin, с которого загрузился widget.js → база для API и статики
  var me = document.currentScript || (function () {
    var s = document.getElementsByTagName("script");
    return s[s.length - 1];
  })();
  var API = new URL(".", me.src).href.replace(/\/+$/, "");   // .../static → база
  API = API.replace(/\/static$/, "");                         // отрезаем /static → корень бота
  var STATIC = API + "/static";
  var AVATAR = STATIC + "/tyos-avatar.png";

  var GREETING = "Здравствуйте! Я Бука, ИИ-консультант «Азбуки Леса». Подберу материал, посчитаю объём и цену. Чем помочь?";
  var START_CHIPS = ["Подобрать под задачу", "Знаю, что нужно", "Рассчитать объём", "Доставка и оплата"];

  var CSS = `
#tyos-root{--paper:#FAF7F1;--paper2:#F1EADD;--ink:#1F1B16;--mute:#6B6258;--line:#E7E0D5;
 --green:#2F5A3A;--green-d:#1E3D27;--wood:#B98B5E;--wood-d:#8A6238;--clay:#C2502F;
 font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
#tyos-root *{box-sizing:border-box;margin:0;padding:0}
#tyos-root .fab{position:fixed;right:26px;bottom:26px;display:inline-flex;align-items:center;gap:10px;background:var(--green);color:#fff;font-size:14px;font-weight:500;padding:13px 18px;border-radius:30px;box-shadow:0 8px 24px rgba(31,27,22,.18);cursor:pointer;z-index:2147483640;border:none}
#tyos-root .fab svg{width:20px;height:20px}
#tyos-root .fab:hover{background:var(--green-d)}
#tyos-root .fab-teaser{position:fixed;right:30px;bottom:92px;max-width:262px;background:var(--green-d);color:#fff;border-radius:14px;padding:12px 40px 12px 13px;box-shadow:0 14px 34px rgba(31,27,22,.3);font-size:13.5px;font-weight:500;line-height:1.4;z-index:2147483641;cursor:pointer;opacity:0;transform:translateY(8px);pointer-events:none;transition:opacity .35s ease,transform .35s ease;display:flex;align-items:center;gap:10px}
#tyos-root .fab-teaser .tic{width:34px;height:34px;border-radius:50%;flex-shrink:0;overflow:hidden;background:var(--paper2)}
#tyos-root .fab-teaser .tic img{width:100%;height:100%;object-fit:cover}
#tyos-root .fab-teaser.show{opacity:1;transform:translateY(0);pointer-events:auto}
#tyos-root .fab-teaser::after{content:"";position:absolute;right:30px;bottom:-6px;width:13px;height:13px;background:var(--green-d);transform:rotate(45deg);border-radius:0 0 3px 0}
#tyos-root .fab-teaser .tx{position:absolute;top:8px;right:9px;width:22px;height:22px;border:none;background:rgba(255,255,255,.15);color:#fff;font-size:13px;line-height:1;cursor:pointer;border-radius:50%;display:flex;align-items:center;justify-content:center}
#tyos-root .fab-teaser .tx:hover{background:rgba(255,255,255,.28)}
#tyos-root .chatwin{position:fixed;right:26px;bottom:90px;width:360px;max-width:calc(100vw - 36px);background:#fff;border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:0 16px 44px rgba(31,27,22,.22);z-index:2147483639;display:none;flex-direction:column}
#tyos-root .chatwin.open{display:flex}
#tyos-root .chathead{background:var(--green);color:#fff;padding:14px 16px;display:flex;align-items:center;gap:11px}
#tyos-root .ava{width:36px;height:36px;border-radius:50%;background:var(--paper2);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;overflow:hidden}
#tyos-root .ava img{width:100%;height:100%;object-fit:cover}
#tyos-root .chathead b{font-size:14px;font-weight:600;display:block}
#tyos-root .chathead small{font-size:11px;opacity:.85}
#tyos-root .chathead .x{margin-left:auto;cursor:pointer;opacity:.85;font-size:18px;line-height:1;background:none;border:none;color:#fff}
#tyos-root .chatbody{padding:14px;background:var(--paper);height:380px;max-height:60vh;overflow-y:auto;display:flex;flex-direction:column}
#tyos-root .msg{font-size:13.5px;padding:9px 13px;border-radius:14px;max-width:86%;margin-bottom:10px;white-space:pre-wrap;word-wrap:break-word}
#tyos-root .msg.in{background:#fff;border:1px solid var(--line);border-bottom-left-radius:4px;align-self:flex-start;color:var(--ink)}
#tyos-root .msg.out{background:var(--green);color:#fff;border-bottom-right-radius:4px;align-self:flex-end}
#tyos-root .msg strong{font-weight:700}
#tyos-root .typing{align-self:flex-start;color:var(--mute);font-size:13px;margin-bottom:10px;font-style:italic}
#tyos-root .chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:10px}
#tyos-root .qchip{font-size:12px;background:#fff;border:1px solid var(--green);color:var(--green);padding:6px 12px;border-radius:18px;cursor:pointer}
#tyos-root .qchip:hover{background:var(--green);color:#fff}
#tyos-root .acts{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
#tyos-root .act{font-size:13.5px;font-weight:500;padding:11px 14px;border-radius:11px;cursor:pointer;text-align:center;border:1px solid var(--green);transition:all .2s}
#tyos-root .act.primary{background:var(--green);color:#fff}
#tyos-root .act.primary:hover{background:var(--green-d)}
#tyos-root .act.ghost{background:#fff;color:var(--green-d)}
#tyos-root .act.ghost:hover{background:var(--paper2)}
#tyos-root .chatfoot{display:flex;align-items:center;gap:9px;padding:11px 14px;border-top:1px solid var(--line);background:#fff}
#tyos-root .chatfoot input{flex:1;font-size:13.5px;border:none;outline:none;color:var(--ink);background:transparent}
#tyos-root .chatfoot .snd{width:34px;height:34px;border-radius:50%;background:var(--green);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;border:none;flex-shrink:0}
#tyos-root .chatfoot .snd svg{width:16px;height:16px}
#tyos-root .chatfoot .snd:disabled{opacity:.5}
#tyos-root .chatfoot .mic{display:none;width:34px;height:34px;border-radius:50%;background:#fff;border:1px solid var(--green);color:var(--green);align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
#tyos-root .chatfoot .mic svg{width:17px;height:17px}
#tyos-root .chatfoot .mic:disabled{opacity:.5}
#tyos-root.has-mic .chatfoot .mic{display:flex}
#tyos-root .chatfoot .mic.rec{background:var(--clay);border-color:var(--clay);color:#fff;animation:tyos-micpulse 1.1s ease-in-out infinite}
@keyframes tyos-micpulse{0%,100%{box-shadow:0 0 0 0 rgba(194,80,47,.5)}50%{box-shadow:0 0 0 7px rgba(194,80,47,0)}}
@media(max-width:560px){#tyos-root .chatwin{right:8px;bottom:80px}#tyos-root .fab{right:12px;bottom:12px}#tyos-root .fab-teaser{right:14px;bottom:78px;max-width:215px}#tyos-root .fab-teaser::after{right:26px}}
`;

  var ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a8 8 0 0 1-11.5 7.2L3 21l1.8-6.5A8 8 0 1 1 21 12z"></path></svg>';
  var ICON_MIC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8"></path></svg>';
  var ICON_SEND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 19V5M5 12l7-7 7 7"></path></svg>';

  var HTML =
    '<div class="fab-teaser" id="tyosTeaser">' +
      '<span class="tic"><img src="' + AVATAR + '" alt="Бука"></span>' +
      '<span>Подберу материал и посчитаю цену — спросите</span>' +
      '<button class="tx" id="tyosTeaserX" aria-label="Закрыть">✕</button>' +
    '</div>' +
    '<button class="fab" id="tyosFab">' + ICON_CHAT + ' Консультант 24/7</button>' +
    '<div class="chatwin" id="tyosWin">' +
      '<div class="chathead">' +
        '<div class="ava"><img src="' + AVATAR + '" alt="Консультант"></div>' +
        '<div><b>Консультант 24/7</b><small>ИИ-консультант · отвечает за пару секунд</small></div>' +
        '<button class="x" id="tyosClose">✕</button>' +
      '</div>' +
      '<div class="chatbody" id="tyosBody"></div>' +
      '<div class="chatfoot">' +
        '<input id="tyosInput" placeholder="Напишите сообщение…" autocomplete="off">' +
        '<button class="mic" id="tyosMic" aria-label="Голосовой ввод">' + ICON_MIC + '</button>' +
        '<button class="snd" id="tyosSnd">' + ICON_SEND + '</button>' +
      '</div>' +
    '</div>';

  function boot() {
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    var root = document.createElement("div");
    root.id = "tyos-root";
    root.innerHTML = HTML;
    document.body.appendChild(root);

    var sid = "web-" + Math.random().toString(36).slice(2) + "-" + ((window.performance ? performance.now() : Date.now()) | 0);
    var started = false, busy = false;
    var recorder = null, chunks = [], recording = false;

    var $ = function (id) { return document.getElementById(id); };
    var bodyEl = $("tyosBody"), winEl = $("tyosWin"), inputEl = $("tyosInput");

    function scroll() { bodyEl.scrollTop = bodyEl.scrollHeight; }

    function mdToHtml(text) {
      var s = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      return s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    }
    function addMsg(text, who) {
      var d = document.createElement("div");
      d.className = "msg " + (who === "out" ? "out" : "in");
      if (who === "out") d.textContent = text; else d.innerHTML = mdToHtml(text);
      bodyEl.appendChild(d); scroll();
    }
    function addChips(list) {
      if (!list || !list.length) return;
      var row = document.createElement("div");
      row.className = "chips";
      list.forEach(function (c) {
        var b = document.createElement("span");
        b.className = "qchip"; b.textContent = c;
        b.onclick = function () { row.remove(); sendText(c); };
        row.appendChild(b);
      });
      bodyEl.appendChild(row); scroll();
    }
    function addActions(list) {
      if (!list || !list.length) return;
      var row = document.createElement("div");
      row.className = "acts";
      list.forEach(function (a) {
        var b = document.createElement("div");
        b.className = "act " + (a.type === "max" ? "primary" : "ghost");
        b.textContent = a.label;
        b.onclick = function () {
          row.remove();
          if (a.type === "max") {
            if (a.url) { window.open(a.url, "_blank"); addMsg("Открываю MAX — ваш расчёт сохранён, продолжайте там 👍", "in"); }
            else { addMsg("Расчёт сохранён ✓ MAX-бот «Азбуки Леса» скоро подключится — там можно будет продолжить и передать заказ менеджеру.", "in"); }
          } else { sendText("Оставить телефон"); }
        };
        row.appendChild(b);
      });
      bodyEl.appendChild(row); scroll();
    }
    function typing(on) {
      var t = $("tyosTyping");
      if (on && !t) { t = document.createElement("div"); t.id = "tyosTyping"; t.className = "typing"; t.textContent = "Бука печатает…"; bodyEl.appendChild(t); scroll(); }
      if (!on && t) t.remove();
    }
    function start() {
      if (started) return; started = true;
      addMsg(GREETING, "in"); addChips(START_CHIPS);
    }

    function sendText(text) {
      if (busy || !text.trim()) return;
      busy = true; $("tyosSnd").disabled = true;
      addMsg(text, "out"); typing(true);
      var ctrl = new AbortController();
      var to = setTimeout(function () { ctrl.abort(); }, 26000);
      fetch(API + "/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sid, text: text }), signal: ctrl.signal
      }).then(function (r) { return r.json(); }).then(function (data) {
        typing(false);
        addMsg(data.reply || "…", "in");
        if (data.chips) addChips(data.chips);
        if (data.actions) addActions(data.actions);
      }).catch(function () {
        typing(false);
        addMsg("Что-то подвисло. Попробуйте повторить вопрос — или оставьте телефон, и менеджер свяжется.", "in");
      }).then(function () {
        clearTimeout(to); busy = false; $("tyosSnd").disabled = false; inputEl.focus();
      });
    }
    function send() {
      // во время записи «отправить» = закончить запись и отправить (логика «я договорила»)
      if (recording) { stopRec(); return; }
      var t = inputEl.value; inputEl.value = ""; sendText(t);
    }

    // --- голос (только сенсорные устройства + secure context) ---
    function micSupported() {
      return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
    }
    function isPhone() {
      return micSupported() && window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
    }
    function startRec() {
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        chunks = [];
        recorder = new MediaRecorder(stream);
        recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
        recorder.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          var blob = new Blob(chunks, { type: chunks[0] ? chunks[0].type : "audio/webm" });
          if (blob.size) transcribe(blob);
        };
        recorder.start(); recording = true;
        var btn = $("tyosMic"); btn.classList.add("rec"); btn.setAttribute("aria-label", "Остановить запись");
        inputEl.placeholder = "Говорите… нажмите ещё раз";
      }).catch(function () {
        addMsg("Не получилось включить микрофон. Разрешите доступ в браузере — или просто напишите текстом.", "in");
      });
    }
    function stopRec() {
      recording = false;
      var btn = $("tyosMic"); btn.classList.remove("rec"); btn.setAttribute("aria-label", "Голосовой ввод");
      inputEl.placeholder = "Напишите сообщение…";
      if (recorder && recorder.state !== "inactive") recorder.stop();
    }
    function transcribe(blob) {
      var btn = $("tyosMic"); btn.disabled = true; typing(true);
      fetch(API + "/stt", { method: "POST", headers: { "Content-Type": blob.type || "application/octet-stream" }, body: blob })
        .then(function (r) { return r.json(); }).then(function (data) {
          typing(false);
          var text = (data.text || "").trim();
          if (text) sendText(text);
          else addMsg("Не расслышал 🙉 Попробуйте ещё раз поближе к микрофону — или напишите текстом.", "in");
        }).catch(function () {
          typing(false);
          addMsg("Не получилось распознать голос. Попробуйте ещё раз или напишите текстом.", "in");
        }).then(function () { btn.disabled = false; inputEl.focus(); });
    }
    function mic() { if (busy) return; if (recording) stopRec(); else startRec(); }

    function hideTeaser() { var t = $("tyosTeaser"); if (t) t.classList.remove("show"); }
    function dismissTeaser() { hideTeaser(); try { localStorage.setItem("tyos_teaser_dismissed", "1"); } catch (e) {} }
    function openFromTeaser() { hideTeaser(); if (!winEl.classList.contains("open")) toggle(); }
    function maybeShowTeaser() {
      var dismissed = false;
      try { dismissed = localStorage.getItem("tyos_teaser_dismissed") === "1"; } catch (e) {}
      if (dismissed) return;
      setTimeout(function () {
        if (!winEl.classList.contains("open")) { var t = $("tyosTeaser"); if (t) t.classList.add("show"); }
      }, 4000);
    }
    function toggle() {
      winEl.classList.toggle("open");
      if (winEl.classList.contains("open")) { hideTeaser(); start(); inputEl.focus(); }
    }

    // привязки
    $("tyosFab").addEventListener("click", toggle);
    $("tyosClose").addEventListener("click", toggle);
    $("tyosSnd").addEventListener("click", send);
    $("tyosMic").addEventListener("click", mic);
    $("tyosTeaser").addEventListener("click", openFromTeaser);
    $("tyosTeaserX").addEventListener("click", function (e) { e.stopPropagation(); dismissTeaser(); });
    inputEl.addEventListener("keydown", function (e) { if (e.key === "Enter") send(); });

    maybeShowTeaser();
    if (isPhone()) root.classList.add("has-mic");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
