// Азбука Леса — голосовой бот «Вера». Voximplant VoxEngine scenario.
// Голос — наш SpeechKit (alena / neutral / +8%) через /tts; мозг — /chat.
//
// ASR — legacy непрерывный движок ASRLanguage.RUSSIAN_RU (как до 29.05).
//   История багов (2026-05-31): профильный ASRProfileList.Yandex.ru_RU + interimResults
//   (введён 29.05 ради «не перебивай») ИСЧЕРПЫВАЛСЯ после ~3 высказываний / ~22с и молча
//   переставал слушать — звонок глох после 2-й содержательной фразы. До 29.05 на legacy-движке
//   шли длинные многоходовые звонки (4+ мин, полный заказ). Возврат к legacy снимает баг.
//   «Не перебивать» сохранено через паузу-таймер PAUSE_MS на ASREvents.Result.
require(Modules.ASR);

const API_BASE = "http://85.192.29.181:8090";
const API_KEY = "pMzoRbdEDc68pFpAFjOdDrJ7UCXPcj_-";
// Разные короткие заполнители — чередуем, чтобы не приедалось (играют только при задержке).
const FILLERS = ["Секунду.", "Минутку.", "Сейчас посмотрю.", "Так, смотрю.", "Один момент."];
let fillerIdx = 0;
function nextFiller() { const f = FILLERS[fillerIdx % FILLERS.length]; fillerIdx++; return f; }
const FILLER_DELAY_MS = 1800;        // заполнитель если мозг думает дольше — маскируем паузу пораньше
const PAUSE_MS = 800;                // тишина клиента, после которой считаем, что он договорил (было 1300 — резали паузы 2026-05-31)
const GREETING = "Здравствуйте! Компания Азбука Леса, меня зовут Вера. Как могу к вам обращаться?";

function ttsUrl(text) {
    return API_BASE + "/tts?key=" + encodeURIComponent(API_KEY) + "&text=" + encodeURIComponent(text);
}

// ФОНОВАЯ ПОДЛОЖКА ОТКЛЮЧЕНА (2026-05-31): на телефонной линии тихий офисный шум/щелчки
// клиент воспринимал как «помехи»/плохую связь и он забивал голос. Чистый канал важнее
// «живости». Вернуть позже более тихой/чистой версией после полировки диалога.

let call, asr;
let sessionId = "vox-init";

// Состояние «дослушивания»: копим сегменты речи, отвечаем только после паузы PAUSE_MS.
let buffer = "";
let pauseTimer = null;
let processing = false;

VoxEngine.addEventListener(AppEvents.CallAlerting, (e) => {
    call = e.call;
    sessionId = "vox-" + call.id();
    call.addEventListener(CallEvents.Connected, onConnected);
    call.addEventListener(CallEvents.Disconnected, () => VoxEngine.terminate());
    call.answer();
});

function onConnected() {
    // Legacy непрерывный ASR — одна сессия на весь звонок, без ограничения на число фраз.
    asr = VoxEngine.createASR(ASRLanguage.RUSSIAN_RU);
    // Result = клиент закончил очередную фразу (endpointing Яндекса). Копим и ждём паузу:
    // вдруг продолжит — тогда таймер сбросится следующим Result и склеит фразы.
    asr.addEventListener(ASREvents.Result, (e) => {
        if (processing) return;                 // пока говорит Вера — игнор (не перебиваем себя)
        const t = (e.text || "").trim();
        if (!t) return;
        buffer = buffer ? (buffer + " " + t) : t;
        if (pauseTimer) clearTimeout(pauseTimer);
        pauseTimer = setTimeout(finishTurn, PAUSE_MS);
    });
    play(GREETING, true);   // единственное приветствие; мозг повторно не здоровается
}

// Начать слушать клиента: подаём медиапоток в ASR.
function listen() {
    buffer = "";
    processing = false;
    call.sendMediaTo(asr);
}

// Клиент договорил (пауза PAUSE_MS без новой фразы) → обрабатываем накопленное.
function finishTurn() {
    pauseTimer = null;
    if (processing) return;
    const text = buffer.trim();
    buffer = "";
    if (!text) return;
    processing = true;
    call.stopMediaTo(asr);     // на время ответа Веры не слушаем (legacy это переживает)
    Logger.write("USER: " + text);
    handleUtterance(text);
}

// Проиграть текст нашим голосом.
//   hangupAfter=true → после реплики кладём трубку (прощание, мозг прислал end=true).
//   listenAfter=true → снова слушаем клиента (обычный ход диалога).
function play(text, listenAfter, hangupAfter) {
    const player = VoxEngine.createURLPlayer(ttsUrl(text));
    player.sendMediaTo(call);
    player.addEventListener(PlayerEvents.PlaybackFinished, () => {
        player.stop();
        if (hangupAfter) { call.hangup(); return; }
        if (listenAfter) listen();
    });
}

function handleUtterance(text) {
    let replyText = null;
    let endCall = false;
    let fillerPlaying = false, fillerDone = false, answered = false;

    // Заполнитель — ТОЛЬКО если мозг думает дольше FILLER_DELAY_MS (на расчётах).
    let timer = setTimeout(() => { timer = null; startFiller(); }, FILLER_DELAY_MS);

    function startFiller() {
        fillerPlaying = true;
        const filler = VoxEngine.createURLPlayer(ttsUrl(nextFiller()));
        filler.sendMediaTo(call);
        filler.addEventListener(PlayerEvents.PlaybackFinished, () => {
            filler.stop(); fillerPlaying = false; fillerDone = true; tryAnswer();
        });
    }
    function tryAnswer() {
        if (answered || replyText === null) return;
        if (fillerPlaying && !fillerDone) return;   // дождёмся конца заполнителя
        answered = true;
        play(replyText, !endCall, endCall);   // endCall → после прощания вешаем трубку
    }

    Net.httpRequestAsync(API_BASE + "/chat", {
        method: "POST",
        headers: ["Content-Type: application/json", "X-API-Key: " + API_KEY],
        postData: JSON.stringify({ session_id: sessionId, text: text })
    }).then((res) => {
        replyText = "Извините, не поняла вопрос. Повторите, пожалуйста.";
        if (res.code === 200) {
            try {
                const j = JSON.parse(res.text);
                if (j.reply) replyText = j.reply;
                if (j.end) endCall = true;   // мозг пометил прощальную реплику → завершаем звонок
            }
            catch (err) { Logger.write("parse err: " + err); }
        } else {
            Logger.write("API " + res.code + ": " + res.text);
        }
        if (timer) { clearTimeout(timer); timer = null; }
        tryAnswer();
    }).catch((err) => {
        Logger.write("HTTP err: " + err);
        replyText = "Секунду, технические неполадки. Повторите, пожалуйста.";
        if (timer) { clearTimeout(timer); timer = null; }
        tryAnswer();
    });
}
