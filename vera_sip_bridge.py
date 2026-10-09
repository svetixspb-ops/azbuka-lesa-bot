"""Вера — мост между SIP-звонком (Asterisk AudioSocket) и мозгом Веры (api.py).

Схема звонка:

    Mango Office --SIP--> Asterisk --AudioSocket(TCP)--> ЭТОТ МОСТ --HTTP--> :8090
                                                              |
                                        VAD: когда клиент договорил
                                        /voice?fmt=lpcm  → распознать + ответ
                                        /tts?text=...    → голос Веры (alena)

Поведение один в один перенесено из обкатанного сценария Voximplant
(`voximplant_scenario.js`), включая выстраданные решения:

  * приветствие произносит ТЕЛЕФОНИЯ, не мозг (в prompts.py прописано, что
    звонок уже открыт приветствием — иначе Вера здоровается дважды);
  * пока говорит Вера — вход игнорируем, себя не перебиваем;
  * заполнитель («Секунду.») играет ТОЛЬКО если мозг думает дольше 1.8 с;
  * пауза 800 мс тишины = клиент договорил (1300 мс резало живые паузы);
  * фоновая подложка офиса ОТКЛЮЧЕНА — на телефонной линии клиенты
    принимали её за помехи (решение 31.05.2026, не возвращать без полировки);
  * `end=true` от мозга → доигрываем прощание и кладём трубку.

Важно про устройство: сокет вычитывается НЕПРЕРЫВНО, озвучка всегда идёт
отдельной задачей. Если ждать конца реплики Веры внутри цикла чтения,
кадры от Asterisk копятся в буфере ядра и диалог начинает разъезжаться
со временем — звонок отвечает на то, что было сказано полминуты назад.

Запуск: systemd-юнит vera-sip-bridge.service (venv/bin/python vera_sip_bridge.py)
"""
from __future__ import annotations

import asyncio
import audioop
import io
import itertools
import logging
import os
import struct
import time
import wave
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("vera-sip")
# httpx на INFO печатает полный URL — а в нём ключ API. В журнал это не надо.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Параметры связи ──────────────────────────────────────────────────────────
BIND_HOST = os.environ.get("VERA_BRIDGE_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("VERA_BRIDGE_PORT", "9092"))
API_BASE = os.environ.get("VERA_API_BASE", "http://127.0.0.1:8090")
API_KEY = (os.environ.get("VOX_API_KEY") or "").strip()

# AudioSocket всегда отдаёт slin: 8 кГц, 16 бит LE, моно, кадр 20 мс.
RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000          # 160
FRAME_BYTES = FRAME_SAMPLES * 2                  # 320

# ── Тайминги диалога (перенесены из voximplant_scenario.js) ─────────────────
PAUSE_MS = 800            # тишина, после которой считаем, что клиент договорил
SPEECH_START_FRAMES = 3   # 60 мс речи — начало высказывания
MIN_SPEECH_FRAMES = 15    # короче 300 мс — щелчок/шум, не реплика
MAX_UTTERANCE_S = 30      # предохранитель: лимит Yandex STT v1 — 1 МБ (~65 с на 8 кГц)
FILLER_DELAY_S = 1.8      # заполнитель только если мозг думает дольше
ECHO_GUARD_MS = 250       # после своей реплики не слушаем — гасим эхо линии
NO_INPUT_PROMPT_S = 15    # молчит после приветствия — переспросить
NO_INPUT_HANGUP_S = 15    # молчит и после переспроса — попрощаться

GREETING = "Здравствуйте! Компания Азбука Леса, меня зовут Вера. Как могу к вам обращаться?"
FILLERS = ["Секунду.", "Минутку.", "Сейчас посмотрю.", "Так, смотрю.", "Один момент."]
NO_INPUT_PROMPT = "Алло, вы меня слышите?"
NO_INPUT_BYE = "Похоже, связь пропала. Перезвоните нам, пожалуйста, будем рады помочь. До свидания!"
TECH_ERROR = "Секунду, технические неполадки. Повторите, пожалуйста."
NOT_UNDERSTOOD = "Извините, не поняла вопрос. Повторите, пожалуйста."

_fillers = itertools.cycle(FILLERS)

# ── Протокол AudioSocket: [1 байт тип][2 байта длина BE][полезная нагрузка] ─
KIND_TERMINATE = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x02
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF


def _vad_factory():
    """VAD от WebRTC, если доступен; иначе — по энергии сигнала.

    Идём в `_webrtcvad` напрямую: питонья обёртка webrtcvad.py падает на
    `import pkg_resources`, которого нет в setuptools 84+. Нам нужен только
    C-движок, обёртка там чисто косметическая.
    """
    try:
        import _webrtcvad

        handle = _webrtcvad.create()
        _webrtcvad.init(handle)
        _webrtcvad.set_mode(handle, 2)  # 0 самый мягкий … 3 самый строгий

        def is_speech(frame: bytes) -> bool:
            return bool(_webrtcvad.process(handle, RATE, frame, FRAME_SAMPLES))

        return is_speech, "webrtc"
    except Exception as e:  # pragma: no cover — страховка, не основной путь
        log.warning("WebRTC VAD недоступен (%s) — падаю на VAD по энергии", e)

        def is_speech(frame: bytes) -> bool:
            return audioop.rms(frame, 2) > 500

        return is_speech, "energy"


def _wav_to_slin8k(data: bytes) -> bytes:
    """WAV от SpeechKit (обычно 22 кГц) → slin 8 кГц моно для телефонной линии."""
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
        width = 2
    if channels > 1:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if rate != RATE:
        pcm, _ = audioop.ratecv(pcm, width, 1, rate, RATE, None)
    return pcm


class Call:
    """Один звонок: непрерывно читает кадры, ловит конец реплики, отвечает голосом."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 http: httpx.AsyncClient):
        self.reader = reader
        self.writer = writer
        self.http = http
        self.uuid = "?"
        self.session_id = "sip-?"

        self.is_speech, self.vad_kind = _vad_factory()
        self.closing = False
        self.turns = 0
        self.prompted_no_input = False

        self._busy: asyncio.Task | None = None   # идёт реплика Веры — вход глушим
        self._mute_until = 0.0                   # гасим эхо сразу после своей реплики
        self._chunks: list[bytes] = []            # накопленное высказывание
        self._voiced = 0                          # кадров речи в нём
        self._silence = 0                         # подряд кадров тишины после начала речи
        self._in_speech = False
        self._last_voice_ts = time.monotonic()

    # ── нижний уровень: кадры AudioSocket ──────────────────────────────────
    async def _read_frame(self) -> tuple[int, bytes]:
        head = await self.reader.readexactly(3)
        kind, length = head[0], struct.unpack("!H", head[1:3])[0]
        payload = await self.reader.readexactly(length) if length else b""
        return kind, payload

    async def _send_audio(self, frame: bytes) -> None:
        self.writer.write(bytes([KIND_AUDIO]) + struct.pack("!H", len(frame)) + frame)
        await self.writer.drain()

    async def _hangup(self) -> None:
        self.closing = True
        try:
            self.writer.write(bytes([KIND_TERMINATE]) + struct.pack("!H", 0))
            await self.writer.drain()
        except Exception:
            pass

    # ── воспроизведение ────────────────────────────────────────────────────
    async def _play_pcm(self, pcm: bytes) -> None:
        """Отдать звук в звонок ровно в реальном времени (кадр раз в 20 мс).

        Без выдержки темпа Asterisk получит ответ одной пачкой, захлебнётся
        буфером, и трубка начнёт реагировать с задержкой.
        """
        deadline = time.monotonic()
        for off in range(0, len(pcm), FRAME_BYTES):
            if self.closing:
                return
            frame = pcm[off:off + FRAME_BYTES]
            if len(frame) < FRAME_BYTES:
                frame += b"\x00" * (FRAME_BYTES - len(frame))
            await self._send_audio(frame)
            deadline += FRAME_MS / 1000
            delay = deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

    async def _play_text(self, text: str) -> None:
        try:
            r = await self.http.get(f"{API_BASE}/tts", params={"text": text, "key": API_KEY},
                                    timeout=30)
            r.raise_for_status()
            await self._play_pcm(_wav_to_slin8k(r.content))
        except Exception as e:
            log.error("[%s] озвучка не удалась (%r): %s", self.session_id, text[:40], e)

    # ── единственная «занятость» звонка: Вера говорит ──────────────────────
    def _start(self, coro) -> None:
        """Запустить реплику Веры отдельной задачей.

        Цикл чтения при этом продолжает вычитывать сокет и выбрасывать кадры —
        иначе они копятся в буфере и диалог разъезжается со временем.
        """
        if self._busy is not None or self.closing:
            coro.close()
            return
        self._busy = asyncio.create_task(coro)

        def done(task: asyncio.Task) -> None:
            self._busy = None
            self._mute_until = time.monotonic() + ECHO_GUARD_MS / 1000
            self._last_voice_ts = time.monotonic()
            self._reset_utterance()
            if not task.cancelled() and task.exception():
                log.error("[%s] реплика Веры упала: %s", self.session_id, task.exception())

        self._busy.add_done_callback(done)

    # ── один ход диалога ───────────────────────────────────────────────────
    async def _handle_turn(self, pcm: bytes) -> None:
        self.turns += 1
        log.info("[%s] ход %d: реплика клиента %.1f с",
                 self.session_id, self.turns, len(pcm) / (RATE * 2))

        filler_playing = asyncio.Event()

        async def filler() -> None:
            await asyncio.sleep(FILLER_DELAY_S)
            filler_playing.set()
            await self._play_text(next(_fillers))

        filler_task = asyncio.create_task(filler())

        async def drop_filler() -> None:
            # Не начался — снимаем. Уже звучит — даём договорить, не рубим на полуслове.
            if not filler_playing.is_set():
                filler_task.cancel()
            await asyncio.gather(filler_task, return_exceptions=True)

        reply, end = NOT_UNDERSTOOD, False
        try:
            r = await self.http.post(
                f"{API_BASE}/voice",
                params={"session_id": self.session_id, "tts": "0", "fmt": "lpcm", "rate": RATE},
                headers={"X-API-Key": API_KEY, "Content-Type": "application/octet-stream"},
                content=pcm,
                timeout=60,
            )
            if r.status_code == 200:
                j = r.json()
                transcript = (j.get("transcript") or "").strip()
                log.info("[%s] КЛИЕНТ: %s", self.session_id, transcript or "(пусто)")
                if not transcript:
                    # Распознавание ничего не дало — молча слушаем дальше, чтобы
                    # не отвечать «повторите» на кашель или шум линии.
                    await drop_filler()
                    self.prompted_no_input = False
                    return
                if j.get("reply"):
                    reply = j["reply"]
                end = bool(j.get("end"))
            else:
                log.error("[%s] API %s: %s", self.session_id, r.status_code, r.text[:300])
                reply = TECH_ERROR
        except Exception as e:
            log.error("[%s] запрос к мозгу не удался: %s", self.session_id, e)
            reply = TECH_ERROR

        await drop_filler()
        log.info("[%s] ВЕРА: %s%s", self.session_id, reply[:200], " [конец звонка]" if end else "")
        await self._play_text(reply)
        self.prompted_no_input = False
        if end:
            await self._hangup()

    async def _greet(self) -> None:
        # Единственное приветствие: в prompts.py прописано, что мозг
        # здороваться повторно не должен.
        await self._play_text(GREETING)

    async def _no_input_prompt(self) -> None:
        await self._play_text(NO_INPUT_PROMPT)

    async def _no_input_bye(self) -> None:
        log.info("[%s] клиент молчит — завершаю звонок", self.session_id)
        await self._play_text(NO_INPUT_BYE)
        await self._hangup()

    def _reset_utterance(self) -> None:
        self._chunks.clear()
        self._voiced = 0
        self._silence = 0
        self._in_speech = False

    # ── накопление речи клиента ────────────────────────────────────────────
    def _feed(self, frame: bytes) -> bytes | None:
        """Кадр речи клиента → накопленное высказывание, когда он договорил."""
        if self.is_speech(frame):
            self._last_voice_ts = time.monotonic()
            self._voiced += 1
            self._silence = 0
            if not self._in_speech and self._voiced >= SPEECH_START_FRAMES:
                self._in_speech = True
            self._chunks.append(frame)
        elif self._in_speech:
            self._silence += 1
            self._chunks.append(frame)  # хвост тишины помогает распознаванию
            if self._silence * FRAME_MS >= PAUSE_MS:
                pcm = b"".join(self._chunks)
                enough = self._voiced >= MIN_SPEECH_FRAMES
                self._reset_utterance()
                return pcm if enough else None
        else:
            self._voiced = 0  # одиночные щелчки не копим

        if self._in_speech and len(self._chunks) * FRAME_MS >= MAX_UTTERANCE_S * 1000:
            pcm = b"".join(self._chunks)
            self._reset_utterance()
            return pcm
        return None

    def _check_no_input(self) -> None:
        """Клиент молчит: сначала переспросить, потом вежливо попрощаться."""
        quiet = time.monotonic() - self._last_voice_ts
        if not self.prompted_no_input and quiet >= NO_INPUT_PROMPT_S:
            self.prompted_no_input = True
            self._start(self._no_input_prompt())
        elif self.prompted_no_input and quiet >= NO_INPUT_PROMPT_S + NO_INPUT_HANGUP_S:
            self._start(self._no_input_bye())

    # ── главный цикл звонка ────────────────────────────────────────────────
    async def run(self) -> None:
        started = time.monotonic()
        try:
            while not self.closing:
                try:
                    kind, payload = await self._read_frame()
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break

                if kind == KIND_AUDIO:
                    # Вера говорит (или только что договорила) — кадр вычитан и выброшен.
                    if self._busy is not None or time.monotonic() < self._mute_until:
                        continue
                    if len(payload) != FRAME_BYTES:
                        continue
                    pcm = self._feed(payload)
                    if pcm:
                        self._start(self._handle_turn(pcm))
                    else:
                        self._check_no_input()

                elif kind == KIND_UUID:
                    if len(payload) == 16:
                        h = payload.hex()
                        self.uuid = f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
                    else:
                        self.uuid = payload.decode(errors="replace")
                    self.session_id = f"sip-{self.uuid}"
                    log.info("[%s] звонок принят, VAD=%s", self.session_id, self.vad_kind)
                    self._last_voice_ts = time.monotonic()
                    self._start(self._greet())

                elif kind == KIND_DTMF:
                    log.info("[%s] DTMF: %s", self.session_id, payload.decode(errors="replace"))

                elif kind == KIND_TERMINATE:
                    log.info("[%s] Asterisk завершил звонок", self.session_id)
                    break

                elif kind == KIND_ERROR:
                    log.error("[%s] AudioSocket сообщил об ошибке: %s", self.session_id, payload.hex())
                    break
        finally:
            self.closing = True
            if self._busy:
                self._busy.cancel()
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            # Контекст звонка в мозге больше не нужен — освобождаем.
            try:
                await self.http.post(f"{API_BASE}/reset", json={"session_id": self.session_id},
                                     headers={"X-API-Key": API_KEY}, timeout=10)
            except Exception:
                pass
            log.info("[%s] звонок закрыт: %.0f с, ходов %d",
                     self.session_id, time.monotonic() - started, self.turns)


async def _prewarm(http: httpx.AsyncClient) -> None:
    """Прогреть кэш озвучки: приветствие и заполнители должны звучать мгновенно."""
    for text in [GREETING, *FILLERS, NO_INPUT_PROMPT]:
        try:
            r = await http.get(f"{API_BASE}/tts", params={"text": text, "key": API_KEY}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            log.warning("прогрев озвучки не удался (%r): %s", text[:30], e)
            return
    log.info("озвучка прогрета: приветствие + %d заполнителей", len(FILLERS))


async def main() -> None:
    http = httpx.AsyncClient()

    async def on_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            await Call(reader, writer, http).run()
        except Exception:
            log.exception("звонок с %s упал", peer)

    server = await asyncio.start_server(on_call, BIND_HOST, BIND_PORT)
    log.info("мост Веры слушает %s:%d, мозг на %s", BIND_HOST, BIND_PORT, API_BASE)
    await _prewarm(http)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
