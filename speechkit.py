"""Речь для Веры: STT и TTS. Основной провайдер — Yandex SpeechKit, запасной — ElevenLabs + OpenAI.

Auth Yandex: `Api-Key <YANDEX_API_KEY>`.
STT v1 принимает OGG/OPUS до 1 МБ за один запрос (Telegram voice = OGG, до 1 минуты ≈ 100КБ — подходит).
TTS v1 возвращает OGG/OPUS, прямо отправляем как voice в Telegram.

ЗАПАСНОЙ ПРОВАЙДЕР (`SPEECH_PROVIDER=fallback`) — добавлен 11.10.2026.
В этот день в Yandex Cloud пропало питание в зоне ru-central1-a (04:00 МСК, 84 сервиса,
среди них SpeechKit и YandexGPT; зона ru-central1-b лежала четвёртые сутки после аварии
08.10). SpeechKit отдавал 500 на синтез И на распознавание, по обоим эндпоинтам v1 и v3,
и починить это со своей стороны было нечем: Вера молчала в трубку, а мост при сбое озвучки
просто проглатывал исключение.
  Озвучка  → ElevenLabs, голос «Alisa Natural Russian» (выбор Светы 11.10), turbo v2.5:
             0,27 с против 1,0–1,8 с у OpenAI на замерах того дня.
  Распознавание → OpenAI gpt-4o-mini-transcribe: 0,55–0,84 с и точнее Яндекса; локальный
             faster-whisper base пробовали — 1,6 с и ошибается («самовы возбесплатны»).
Переключение — одной строкой в .env, Яндекс остаётся значением по умолчанию.

КЕШ ОЗВУЧКИ работает при ЛЮБОМ провайдере. Вера дословно повторяет приветствие, «что-то
ещё?», адрес склада, прощание: по журналу настоящих звонков это ~30% символов уже на
четырёх звонках и ~60% на длинной дистанции. Кеш убирает и повторную оплату, и задержку —
постоянные фразы играют мгновенно.
"""
from __future__ import annotations

import os

import httpx

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
TTS_VOICE = os.environ.get("YANDEX_TTS_VOICE", "alena")
TTS_EMOTION = os.environ.get("YANDEX_TTS_EMOTION", "neutral")  # роль голоса; "" чтобы не слать
TTS_SPEED = float(os.environ.get("YANDEX_TTS_SPEED", "1.08"))  # +8% — выбор Sveta/Артёма (вариант 11)

# «yandex» (по умолчанию) или «fallback». Менять ТОЛЬКО на время аварии SpeechKit.
SPEECH_PROVIDER = os.environ.get("SPEECH_PROVIDER", "yandex").strip().lower()

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
# «Alisa Natural Russian» — родной русский голос, а не англичанка с акцентом.
ELEVEN_VOICE_ID = os.environ.get("ELEVEN_VOICE_ID", "t6lBrEl93uCiLR1Lgm8v")
ELEVEN_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_turbo_v2_5")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")


def using_fallback() -> bool:
    return SPEECH_PROVIDER == "fallback"


async def stt(
    audio_bytes: bytes,
    *,
    lang: str = "ru-RU",
    fmt: str = "oggopus",
    sample_rate: int | None = None,
) -> str:
    """Распознать голос → текст.

    fmt="oggopus"  — голосовые из Telegram (по умолчанию).
    fmt="lpcm"     — сырой PCM 16-бит LE моно прямо из звонка (SIP/AudioSocket);
                     тогда обязателен sample_rate (на телефонной линии 8000).
    Лимит Yandex STT v1 — 1 МБ за запрос (8 кГц LPCM ≈ 65 с).
    """
    if using_fallback():
        return await _stt_openai(audio_bytes, lang=lang, fmt=fmt, sample_rate=sample_rate)
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise RuntimeError("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
    params: dict[str, object] = {"folderId": YANDEX_FOLDER_ID, "lang": lang, "format": fmt}
    if fmt == "lpcm":
        if not sample_rate:
            raise ValueError("для fmt=lpcm нужен sample_rate (8000 на телефонной линии)")
        params["sampleRateHertz"] = int(sample_rate)
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.post(
            "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
            params=params,
            headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"},
            content=audio_bytes,
        )
        r.raise_for_status()
        return r.json().get("result", "")


def _pcm_to_wav_bytes(pcm: bytes, rate: int, *, channels: int = 1, width: int = 2) -> bytes:
    """Сырой LPCM → WAV в памяти. Нужен и для OpenAI STT (он не принимает PCM без
    контейнера), и для отдачи озвучки мосту, который ждёт именно WAV."""
    import io as _io2
    import wave as _wave2
    buf = _io2.BytesIO()
    with _wave2.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


import re as _re0

# Подсказка распознаванию. OpenAI без неё пишет сорта ЛАТИНИЦЕЙ («сорт BC», «сорт B-C»),
# а весь разбор сортов ниже по стеку ждёт кириллицу (_GRADE_MULTI_RE) — названный клиентом
# сорт просто терялся. Яндекс такого не делал, это разница провайдеров, а не наша.
# Заодно закрепляет цифры в размерах и словарь товаров.
_OPENAI_STT_PROMPT = (
    "Телефонный заказ пиломатериалов. Размеры и количество — цифрами. "
    "Сорта пиши русскими буквами: сорт АВ, сорт ВС, сорт Б/Ц, сорт БС, сорт БСК. "
    "Товары: доска, брус, брусок, рейка, планкен, вагонка, штакетник, имитация бруса."
)


def _is_prompt_echo(text: str) -> bool:
    """Whisper вернул нашу подсказку вместо речи? Тогда в трубке была тишина.

    OpenAI принимает prompt для смещения распознавания (сорта кириллицей), и на пустом
    фрагменте отдаёт его дословно — целиком или с любого предложения. Нормализуем обе
    строки и проверяем вхождение: короткая реплика клиента так совпасть не может.
    """
    def norm(s: str) -> str:
        return _re0.sub(r"[^a-zа-яё0-9]+", " ", s.lower()).strip()

    t = norm(text)
    if len(t) < 25:
        return False
    return t in norm(_OPENAI_STT_PROMPT)


async def _stt_openai(
    audio_bytes: bytes, *, lang: str = "ru-RU", fmt: str = "oggopus",
    sample_rate: int | None = None,
) -> str:
    """Распознавание через OpenAI — запасной путь на время аварии SpeechKit.

    Сырой PCM со звонка OpenAI не принимает (нужен контейнер), поэтому заворачиваем
    в WAV прямо здесь. Голосовые из Telegram приходят уже в OGG — их шлём как есть.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан — запасное распознавание недоступно")
    if fmt == "lpcm":
        if not sample_rate:
            raise ValueError("для fmt=lpcm нужен sample_rate (8000 на телефонной линии)")
        payload, filename = _pcm_to_wav_bytes(audio_bytes, int(sample_rate)), "audio.wav"
    else:
        payload, filename = audio_bytes, "audio.ogg"
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (filename, payload)},
            data={"model": OPENAI_STT_MODEL, "language": lang.split("-")[0],
                  "prompt": _OPENAI_STT_PROMPT},
        )
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
        if _is_prompt_echo(text):
            # Whisper на тишине/шуме возвращает нашу же подсказку как «речь клиента».
            # Звонок 14.10.2026 14:42: такое эхо Вера приняла за ответ на «Всё верно?»
            # и отправила заявку в сметный отдел — клиент подтверждения не давал.
            _log.warning("STT: распознана подсказка вместо речи — считаю тишиной: %r", text[:80])
            return ""
        return text


import re as _re

# Yandex SpeechKit v3 unary имеет лимит ~250 символов на один синтез.
# Длинные ответы режем по границам предложений и шлём несколькими файлами.
_TTS_MAX_CHARS = 240


def _split_for_tts(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= _TTS_MAX_CHARS:
        return [text]
    # Сначала по абзацам, потом по предложениям внутри абзаца
    parts: list[str] = []
    for para in _re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= _TTS_MAX_CHARS:
            parts.append(para)
            continue
        # Режем по концам предложений (. ! ?)
        buf = ""
        for sent in _re.findall(r"[^.!?]+[.!?]?", para):
            sent = sent.strip()
            if not sent:
                continue
            candidate = (buf + " " + sent).strip() if buf else sent
            if len(candidate) <= _TTS_MAX_CHARS:
                buf = candidate
            else:
                if buf:
                    parts.append(buf)
                if len(sent) <= _TTS_MAX_CHARS:
                    buf = sent
                else:
                    # хард-сплит по длине
                    for i in range(0, len(sent), _TTS_MAX_CHARS):
                        parts.append(sent[i:i + _TTS_MAX_CHARS])
                    buf = ""
        if buf:
            parts.append(buf)
    return parts


import asyncio as _asyncio
import logging as _logging

_log = _logging.getLogger(__name__)

# --- Произношение сортов по буквам (TTS иначе читает «АБ» как слог «аб») ---
_GRADE_LETTER = {"А": "А", "Б": "БЭ", "В": "ВЭ", "С": "ЭС", "К": "КА", "Ц": "ЦЭ"}
# Спец-коды сортов — латиница, записанная кириллицей-двойником (правило Sveta 2026-05-31):
#   «АВ» = латинское AB (А + B) → «А БЭ»;  «ВС» = латинское BC (B + C) → «БЭ ЦЭ».
# Здесь «В»=Latin B=«бэ», «С»=Latin C=«цэ», поэтому посимвольное правило не годится — задаём явно.
_GRADE_CODE_SPELL = {"АВ": "А БЭ", "ВС": "БЭ ЦЭ"}
# многобуквенные коды сортов (длинные — первыми), вокруг не должно быть кириллицы
_GRADE_MULTI_RE = _re.compile(r"(?<![А-Яа-яЁё])(БСК|Б/Ц|АБ|АВ|ВС|БС)(?![А-Яа-яЁё])")
# одиночные сорта Б/В в кавычках «Б» или после слова «сорт»
_GRADE_SINGLE_RE = _re.compile(r"(«|\bсорт[аеуы]?\s+)([БВ])(?![А-Яа-яЁё])")


def _spell_code(code: str) -> str:
    if code in _GRADE_CODE_SPELL:
        return _GRADE_CODE_SPELL[code]
    return " ".join(_GRADE_LETTER.get(ch, ch) for ch in code if ch != "/")


def _spell_grades(text: str) -> str:
    """Заменить буквенные коды сортов на их произношение по буквам — для озвучки."""
    text = _GRADE_MULTI_RE.sub(lambda m: _spell_code(m.group(1)), text)
    text = _GRADE_SINGLE_RE.sub(lambda m: m.group(1) + _GRADE_LETTER[m.group(2)], text)
    return text


# Дробные числа: TTS иначе читает «12,5» криво. «12,5»→«12 с половиной», «5,7»→«5 целых 7».
_DEC_HALF_RE = _re.compile(r"(\d+)[.,]5(?!\d)")
_DEC_OTHER_RE = _re.compile(r"(\d+)[.,](\d)(?!\d)")


def _spell_numbers(text: str) -> str:
    text = _DEC_HALF_RE.sub(r"\1 с половиной", text)
    text = _DEC_OTHER_RE.sub(r"\1 целых \2", text)
    return text


# --- Ударения для TTS (Yandex понимает «+» перед ударной гласной) ---
# Приём Елены (Sasha AI): у пиломатериальных терминов alena часто ставит
# неверное ударение и звучит роботом. Задаём правильные ударения детерминированно.
# Ключ — точная словоформа в нижнем регистре, значение — с «+» перед ударной гласной.
_STRESS_MAP = {
    "строгаем": "стро+гаем", "строгает": "стро+гает", "строгать": "строг+ать",
    "строганый": "стро+ганый", "строганая": "стро+ганая", "строганые": "стро+ганые",
    "строганой": "стро+ганой", "строганую": "стро+ганую",
    "сортов": "сорт+ов", "сучки": "сучк+и", "сучков": "сучк+ов",
    "бруски": "бруск+и", "брусков": "бруск+ов", "брусок": "брус+ок",
    "погонаж": "погон+аж", "погонажа": "погон+ажа",
    "досок": "д+осок", "роквул": "р+оквул", "скандик": "ск+андик",
}
_STRESS_RE = _re.compile(
    r"(?<![А-Яа-яЁё+])(" + "|".join(_re.escape(w) for w in _STRESS_MAP) + r")(?![А-Яа-яЁё])",
    _re.IGNORECASE,
)


def _apply_stress(text: str) -> str:
    """Проставить правильные ударения пиломатериальным терминам для озвучки."""
    def repl(m: "_re.Match") -> str:
        word = m.group(1)
        stressed = _STRESS_MAP[word.lower()]
        # сохранить заглавную первую букву исходного слова
        if word[:1].isupper():
            stressed = stressed[:1].upper() + stressed[1:]
        return stressed
    return _STRESS_RE.sub(repl, text)


# Ударения вида «стро+ганая» понимает ТОЛЬКО Yandex. ElevenLabs прочитает плюс вслух
# или испортит слово, поэтому для запасного провайдера метки снимаем.
# Условие намеренно широкое — «плюс сразу после кириллической буквы»: в _STRESS_MAP
# метка стоит то перед ударной гласной («строг+ать»), то после неё («стро+ганый»),
# и узкое правило «перед гласной» половину слов не вычищало. Телефон «+7 999» не
# задевается: перед его плюсом кириллицы нет.
_STRESS_MARK_RE = _re.compile(r"(?<=[А-Яа-яЁё])\+")


def _prepare_tts(text: str) -> str:
    """Подготовка к озвучке: сорта по буквам + дробные числа словами + ударения."""
    out = _apply_stress(_spell_numbers(_spell_grades(text)))
    if using_fallback():
        out = _STRESS_MARK_RE.sub("", out)
    return out


# ── Кеш озвучки ───────────────────────────────────────────────────────────────
# Вера дословно повторяет приветствие, «что-то ещё?», адрес склада, прощание. По журналу
# настоящих звонков это ~30% символов уже на четырёх звонках и ~60% на длинной дистанции.
# Кеш убирает повторную оплату синтеза И задержку: постоянная фраза играет мгновенно.
# Ключ включает провайдера и голос — при смене того или другого кеш не переиспользуется.
import hashlib as _hashlib
import pathlib as _pathlib

_CACHE_DIR = _pathlib.Path(__file__).resolve().parent / "tts_cache"
# Сервер тесный (решение про размер — см. CLAUDE.md), поэтому кеш ограничен и
# подрезается по времени последнего обращения. Постоянных фраз у Веры десятки, не тысячи.
_CACHE_MAX_FILES = int(os.environ.get("TTS_CACHE_MAX_FILES", "300"))
_CACHE_MAX_MB = int(os.environ.get("TTS_CACHE_MAX_MB", "150"))


def _cache_key(text: str, container: str) -> str:
    voice = ELEVEN_VOICE_ID if using_fallback() else f"{TTS_VOICE}/{TTS_EMOTION}/{TTS_SPEED}"
    raw = f"{SPEECH_PROVIDER}|{voice}|{container}|{text}"
    return _hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache_get(key: str) -> bytes | None:
    p = _CACHE_DIR / key
    try:
        data = p.read_bytes()
        os.utime(p, None)          # отметить обращение — по нему же и вытесняем
        return data or None
    except OSError:
        return None


def _cache_put(key: str, data: bytes) -> None:
    if not data:
        return
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        tmp = _CACHE_DIR / f".{key}.tmp"
        tmp.write_bytes(data)
        tmp.replace(_CACHE_DIR / key)   # атомарно: файл читают параллельные звонки
        _cache_trim()
    except OSError as e:
        _log.warning("кеш озвучки недоступен: %s", e)


def _cache_trim() -> None:
    """Выбросить самые давно не используемые, если вышли за лимит файлов или мегабайт."""
    try:
        files = [(p.stat().st_atime, p.stat().st_size, p)
                 for p in _CACHE_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return
    total = sum(s for _, s, _ in files)
    if len(files) <= _CACHE_MAX_FILES and total <= _CACHE_MAX_MB * 1024 * 1024:
        return
    files.sort()                        # самые старые по обращению — первыми
    for _, size, p in files:
        if len(files) <= _CACHE_MAX_FILES and total <= _CACHE_MAX_MB * 1024 * 1024:
            break
        try:
            p.unlink()
            total -= size
            files = files[1:]
        except OSError:
            break


async def _tts_one_eleven(text: str, *, container: str = "OGG_OPUS") -> bytes:
    """Один кусок текста → аудио через ElevenLabs (запасной провайдер).

    Для телефонии просим сразу pcm_8000 — ровно частота линии, пересэмплировать нечего
    (мост всё равно прогонит через _wav_to_slin8k, но там это станет пустой операцией).
    Для Telegram — opus_48000_32, это уже готовый OGG/Opus.
    """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY не задан — запасная озвучка недоступна")
    import json as _json2
    telephony = container == "WAV"
    fmt = "pcm_8000" if telephony else "opus_48000_32"
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}",
            params={"output_format": fmt},
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            content=_json2.dumps({"text": text, "model_id": ELEVEN_MODEL},
                                 ensure_ascii=False).encode("utf-8"),
        )
        if r.status_code >= 400:
            _log.error("ElevenLabs %s; text=%r body=%s", r.status_code, text[:80], r.text[:300])
            r.raise_for_status()
        audio = r.content
    # pcm_8000 приходит БЕЗ контейнера — заворачиваем в WAV, мост ждёт именно его.
    return _pcm_to_wav_bytes(audio, 8000) if telephony else audio


async def _tts_cached(text: str, *, container: str) -> bytes:
    """Синтез одного куска с кешем. Провайдер выбирается здесь же."""
    key = _cache_key(text, container)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    if using_fallback():
        data = await _tts_one_eleven(text, container=container)
    else:
        data = await _tts_one(text, container=container)
    _cache_put(key, data)
    return data


async def _tts_one(text: str, *, container: str = "OGG_OPUS", _attempt: int = 1) -> bytes:
    """Один кусок ≤240 символов → аудио (OGG_OPUS для Telegram / WAV для телефонии).

    Голос фиксирован: alena, role=neutral, speed=1.08 (выбор Sveta/Артёма). Один
    ретрай при 4xx/5xx с логированием тела.
    """
    import base64
    import json as _json
    hints: list[dict] = [{"voice": TTS_VOICE}, {"speed": TTS_SPEED}]
    if TTS_EMOTION:
        hints.append({"role": TTS_EMOTION})
    body = {
        "text": text,
        "outputAudioSpec": {"containerAudio": {"containerAudioType": container}},
        "hints": hints,
    }
    chunks: list[bytes] = []
    async with httpx.AsyncClient(timeout=30) as cx:
        async with cx.stream(
            "POST",
            "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis",
            headers={
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "x-folder-id": YANDEX_FOLDER_ID,
                "Content-Type": "application/json; charset=utf-8",
            },
            content=_json.dumps(body, ensure_ascii=False).encode("utf-8"),
        ) as r:
            if r.status_code >= 400:
                err_body = (await r.aread()).decode("utf-8", errors="replace")
                _log.error(
                    "TTS %s on attempt %d; text=%r body=%s",
                    r.status_code, _attempt, text[:80], err_body[:500],
                )
                if _attempt == 1 and r.status_code in (429, 500, 502, 503, 504, 400):
                    await _asyncio.sleep(0.7)
                    return await _tts_one(text, container=container, _attempt=2)
                r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                data_b64 = (obj.get("result") or {}).get("audioChunk", {}).get("data")
                if data_b64:
                    chunks.append(base64.b64decode(data_b64))
    return b"".join(chunks)


async def tts(text: str) -> list[bytes]:
    """Озвучить длинный текст → список OGG/Opus сегментов ≤240 симв. каждый.

    Yandex переключил v1 REST на gRPC (form-data → 415), v3 unary имеет лимит
    ~250 симв. на запрос. Длинные ответы режем по границам предложений,
    шлём отдельными voice-сообщениями в Telegram.
    """
    if not using_fallback() and not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise RuntimeError("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
    parts = _split_for_tts(_prepare_tts(text))
    return [await _tts_cached(p, container="OGG_OPUS") for p in parts]


import io as _io
import wave as _wave


def _concat_wav(wavs: list[bytes]) -> bytes:
    """Склеить несколько WAV (одинаковый формат) в один — для проигрывания одним файлом."""
    if len(wavs) == 1:
        return wavs[0]
    params = None
    frames: list[bytes] = []
    for w in wavs:
        with _wave.open(_io.BytesIO(w), "rb") as wf:
            if params is None:
                params = wf.getparams()
            frames.append(wf.readframes(wf.getnframes()))
    out = _io.BytesIO()
    with _wave.open(out, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(b"".join(frames))
    return out.getvalue()


async def tts_wav(text: str) -> bytes:
    """Озвучить текст в ОДИН WAV-файл — для телефонии (Voximplant createURLPlayer).

    Тот же голос alena/neutral/+8%, что и в Telegram. Длинный текст режем по
    предложениям (лимит v3 ~250 симв.) и склеиваем обратно в один WAV.
    """
    if not using_fallback() and not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise RuntimeError("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
    parts = _split_for_tts(_prepare_tts(text))
    wavs = [await _tts_cached(p, container="WAV") for p in parts]
    return _concat_wav(wavs)
