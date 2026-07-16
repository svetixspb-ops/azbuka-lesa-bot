"""Yandex SpeechKit STT и TTS через REST API.

Auth: `Api-Key <YANDEX_API_KEY>`.
STT v1 принимает OGG/OPUS до 1 МБ за один запрос (Telegram voice = OGG, до 1 минуты ≈ 100КБ — подходит).
TTS v1 возвращает OGG/OPUS, прямо отправляем как voice в Telegram.
"""
from __future__ import annotations

import os

import httpx

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
TTS_VOICE = os.environ.get("YANDEX_TTS_VOICE", "alena")
TTS_EMOTION = os.environ.get("YANDEX_TTS_EMOTION", "neutral")  # роль голоса; "" чтобы не слать
TTS_SPEED = float(os.environ.get("YANDEX_TTS_SPEED", "1.08"))  # +8% — выбор Sveta/Артёма (вариант 11)


async def stt(audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
    """Распознать голос → текст. audio_bytes — обычно OGG/Opus из Telegram."""
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise RuntimeError("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.post(
            "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
            params={"folderId": YANDEX_FOLDER_ID, "lang": lang, "format": "oggopus"},
            headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"},
            content=audio_bytes,
        )
        r.raise_for_status()
        return r.json().get("result", "")


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


def _prepare_tts(text: str) -> str:
    """Подготовка к озвучке: сорта по буквам + дробные числа словами + ударения."""
    return _apply_stress(_spell_numbers(_spell_grades(text)))


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
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise RuntimeError("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
    parts = _split_for_tts(_prepare_tts(text))
    return [await _tts_one(p) for p in parts]


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
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise RuntimeError("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
    parts = _split_for_tts(_prepare_tts(text))
    wavs = [await _tts_one(p, container="WAV") for p in parts]
    return _concat_wav(wavs)
