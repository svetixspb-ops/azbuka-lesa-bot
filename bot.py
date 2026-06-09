"""Telegram-обёртка для тестирования Веры — голос-в-голос.

Логика:
1. Пользователь шлёт voice-message
2. Скачиваем OGG → Yandex STT → распознанный текст
3. Извлекаем структуру запроса через LLM → catalog.search()
4. Кормим LLM системный промпт «Вера» + историю + найденные товары → ответ-текст
5. TTS-ответ + параллельно показываем расшифровку и ответ текстом для отладки
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import brain  # noqa: E402  (общий мозг — используется и api.py)
import catalog  # noqa: E402
import speechkit  # noqa: E402

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_IDS = {int(s) for s in (os.environ.get("ADMIN_IDS", "") or "").split(",") if s.strip()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("elena-bot")

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()


def _is_admin(uid: int) -> bool:
    if not ADMIN_IDS:
        return True  # если не настроено — разрешаем всем (тестовый режим)
    return uid in ADMIN_IDS


@dp.message(CommandStart())
async def on_start(m: Message) -> None:
    if not _is_admin(m.from_user.id):
        return
    await m.answer(
        "Я — тестовая обёртка для Веры, голосового бота Азбуки Леса 🌲\n\n"
        "Запиши голосовое («Здравствуйте» / «брус 150х150х6 метров, 5 кубов» / "
        "«что есть из вагонки осины») — пришлю распознанный текст, поиск по каталогу "
        "и голосовой ответ.\n\n"
        f"Каталог обновлён: {catalog.get_yml_date()}"
    )


@dp.message(Command("reset"))
async def on_reset(m: Message) -> None:
    brain.reset(str(m.from_user.id))
    await m.answer("Контекст диалога сброшен.")


@dp.message(F.voice)
async def on_voice(m: Message) -> None:
    if not _is_admin(m.from_user.id):
        return
    uid = m.from_user.id

    # 1) Скачать OGG
    file = await bot.get_file(m.voice.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    audio = buf.getvalue()

    # 2) STT
    try:
        transcript = await speechkit.stt(audio)
    except Exception as e:
        log.exception("STT failed: %s", e)
        await m.answer("Не получилось распознать голос: " + str(e))
        return
    if not transcript.strip():
        await m.answer("STT вернул пустую строку (возможно слишком тихо).")
        return
    await m.answer(f"🎙 Распознал: «{transcript}»")

    # 3-5) Извлечение позиций → поиск по каждой → LLM-ответ
    try:
        answer = await brain.build_reply(str(uid), transcript)
    except Exception as e:
        log.exception("reply failed: %s", e)
        await m.answer("Сорри, мозг бота не ответил: " + str(e))
        return
    answer, _ = brain.split_end(answer)  # убрать служебный маркер завершения из озвучки/показа

    # 6) Параллельно: текст-ответ + голос (длинный ответ режется на сегменты)
    await m.answer(f"💬 {answer}")
    try:
        segments = await speechkit.tts(answer)
        for i, audio in enumerate(segments):
            await m.answer_voice(BufferedInputFile(audio, filename=f"elena_{i+1}.ogg"))
    except Exception as e:
        log.exception("TTS failed: %s", e)
        await m.answer("(TTS упал: " + str(e) + ")")


@dp.message(F.text)
async def on_text(m: Message) -> None:
    """Текстовая ветка — для быстрых тестов без записи голоса."""
    if not _is_admin(m.from_user.id):
        return
    uid = m.from_user.id
    transcript = m.text.strip()
    if not transcript:
        return

    try:
        answer = await brain.build_reply(str(uid), transcript)
    except Exception as e:
        log.exception("LLM error: %s", e)
        await m.answer("LLM error: " + str(e))
        return
    answer, _ = brain.split_end(answer)  # убрать служебный маркер завершения
    await m.answer(answer)


async def main() -> None:
    log.info("Bot starting, catalog date=%s", catalog.get_yml_date())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
