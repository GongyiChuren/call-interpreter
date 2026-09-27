#!/usr/bin/env python3
"""Call Interpreter — server.

One WebSocket carries everything between browser and server:

    browser -> server   JSON control frames + raw PCM16 binary frames
    server  -> browser   JSON events + raw PCM16 binary frames

Binary frames use a 1-byte channel prefix:

    0x00  microphone capture (the Chinese speaker, 16 kHz)
    0x01  far-end capture     (the English speaker,  16 kHz)
    0x02  playback for the call (translated English, 24 kHz) -> browser feeds RTP
    0x03  playback for the user (translated Chinese, 24 kHz) -> browser speakers

The browser is the softphone (a SIP/WebRTC endpoint); the server never touches
RTP. That keeps the SIP stack in one place and lets the same page work against
any WebRTC SIP gateway, not just the one this was built for.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server.audio import SpeechSegmenter, rms          # noqa: E402
from server.config import Config                       # noqa: E402
from server.pipeline import Pipeline                   # noqa: E402
from server.providers import ProviderError             # noqa: E402

log = logging.getLogger("call-interp")
HERE = pathlib.Path(__file__).resolve().parent
WEB = HERE.parent / "web"

CH_MIC, CH_FAR, CH_CALL, CH_USER = 0, 1, 2, 3
MAX_UTTERANCE_BYTES = 16000 * 2 * 20        # hard stop at 20 s of buffered speech


# --------------------------------------------------------------------- helpers
def public_config(cfg: Config) -> dict:
    d = cfg.as_public_dict()
    d["providers"] = {
        "mode": cfg.mode,
        "fused": cfg.fused.provider,
        "stt": cfg.stt.provider,
        "mt": cfg.mt.provider,
        "tts": cfg.tts.provider,
    }
    return d


# ------------------------------------------------------------------- http layer
async def read_http(connection, request):
    """Serve the single-page app; let WebSocket upgrades through untouched."""
    path = request.path.split("?", 1)[0]
    headers = {k.lower(): v for k, v in request.headers.items()}
    if "websocket" in headers.get("upgrade", "").lower():
        return None

    if path in ("/health", "/healthz"):
        return Response(200, "OK",
                        Headers({"Content-Type": "text/plain; charset=utf-8"}),
                        b"ok\n")

    name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
    target = (WEB / name).resolve()
    # Never serve anything outside web/ (path traversal guard).
    if WEB.resolve() not in target.parents and target != WEB.resolve():
        return Response(404, "Not Found",
                        Headers({"Content-Type": "text/plain"}), b"not found\n")
    if not target.is_file():
        return Response(404, "Not Found",
                        Headers({"Content-Type": "text/plain"}), b"not found\n")

    ctype = {".html": "text/html; charset=utf-8",
             ".js": "application/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8",
             ".json": "application/json; charset=utf-8"}.get(target.suffix, "application/octet-stream")
    return Response(200, "OK", Headers({
        "Content-Type": ctype,
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    }), target.read_bytes())


# ----------------------------------------------------------------- session
class Session:
    """One browser = one call-interpreting session."""

    def __init__(self, ws, cfg: Config):
        self.ws = ws
        self.cfg = cfg
        self.pipeline = Pipeline(cfg)
        self.stats = {"mic_bytes": 0, "far_bytes": 0, "en_bytes": 0, "zh_bytes": 0,
                      "zh2en_ok": 0, "en2zh_ok": 0, "errors": 0}
        # Two independent segmenters: each leg decides on its own when the
        # speaker stopped, so neither direction can block the other.
        self.seg_zh = SpeechSegmenter(16000)
        self.seg_en = SpeechSegmenter(16000)
        self._zh_busy = asyncio.Lock()
        self._en_busy = asyncio.Lock()

    # -------------------------------------------------------------- transport
    async def send_json(self, obj: dict) -> None:
        try:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    async def send_audio(self, channel: int, pcm: bytes) -> None:
        if not pcm:
            return
        try:
            await self.ws.send(bytes([channel]) + pcm)
        except Exception:
            pass

    async def error(self, where: str, exc: Exception) -> None:
        self.stats["errors"] += 1
        msg = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("[%s] %s", where, msg)
        await self.send_json({"t": "error", "where": where, "msg": msg})

    # ------------------------------------------------------------ directions
    async def zh2en(self, pcm: bytes) -> None:
        """Chinese speech -> English speech, played into the call."""
        async with self._zh_busy:
            try:
                src, text = await self.pipeline.audio_to_text(pcm, 16000, "zh2en")
                if not text:
                    return
                self.stats["zh2en_ok"] += 1
                await self.send_json({"t": "zh2en_text", "src": src, "text": text})
                audio = await self.pipeline.to_speech(text, "zh2en")
                if audio:
                    self.stats["en_bytes"] += len(audio)
                    await self.send_audio(CH_CALL, audio)
                    await self.send_json({"t": "zh2en_audio", "n": len(audio)})
            except ProviderError as e:
                await self.error("zh2en", e)
            except Exception as e:                       # noqa: BLE001
                await self.error("zh2en", e)

    async def en2zh(self, pcm: bytes) -> None:
        """English speech -> Chinese text (+ optional Chinese audio for the user)."""
        async with self._en_busy:
            try:
                src, text = await self.pipeline.audio_to_text(pcm, 16000, "en2zh")
                if not text:
                    return
                self.stats["en2zh_ok"] += 1
                await self.send_json({"t": "en2zh_text", "src": src, "text": text})
                if self.cfg.in_voice.provider:
                    try:
                        audio = await self.pipeline.to_speech(text, "en2zh")
                        if audio:
                            self.stats["zh_bytes"] += len(audio)
                            await self.send_audio(CH_USER, audio)
                    except ProviderError as e:
                        # Reading the reply aloud is a convenience; failing it
                        # must never hide the text we already delivered.
                        log.info("en2zh tts skipped: %s", e)
            except ProviderError as e:
                await self.error("en2zh", e)
            except Exception as e:                       # noqa: BLE001
                await self.error("en2zh", e)

    # ---------------------------------------------------------------- inbound
    async def handle_binary(self, msg: bytes) -> None:
        if not msg:
            return
        ch, pcm = msg[0], msg[1:]
        if ch == CH_MIC:
            self.stats["mic_bytes"] += len(pcm)
            seg = self.seg_zh.push(pcm)
            if seg:
                asyncio.create_task(self.zh2en(seg))
        elif ch == CH_FAR:
            self.stats["far_bytes"] += len(pcm)
            seg = self.seg_en.push(pcm)
            if seg:
                asyncio.create_task(self.en2zh(seg))

    async def handle_json(self, cmd: dict) -> None:
        t = cmd.get("t")
        if t == "ping":
            await self.send_json({"t": "pong"})
        elif t == "stats":
            await self.send_json({"t": "stats", **self.stats})
        elif t == "level":
            # Live mic meter so the user can see the capture path works before
            # they are mid-call with a bank.
            await self.send_json({"t": "level", "rms": cmd.get("rms", 0)})
        elif t == "flush":
            for seg in (self.seg_zh, self.seg_en):
                tail = seg.flush()
                if tail:
                    asyncio.create_task(
                        self.zh2en(tail) if seg is self.seg_zh else self.en2zh(tail))
        elif t == "say":
            # Type instead of speak: render text to the call in the target voice.
            text = (cmd.get("text") or "").strip()
            direction = cmd.get("direction", "zh2en")
            if text:
                asyncio.create_task(self._say(text, direction))
        elif t == "selftest":
            # Synthesize an English utterance, then push it back through the
            # en->zh path exactly as if the other party had said it. Proves the
            # whole chain (audio in -> translate -> caption) without a call.
            text = (cmd.get("text") or "").strip() or (
                "Good afternoon, you are through to Lloyds Bank. "
                "How can I help you today?")
            asyncio.create_task(self.selftest(text))
        elif t == "probe":
            log.info("PROBE %s", json.dumps(cmd, ensure_ascii=False)[:400])

    async def selftest(self, text: str) -> None:
        try:
            await self.send_json({"t": "selftest_begin", "text": text})
            # 1. render the far end's voice
            audio = await self.pipeline.to_speech(text, "zh2en")
            if not audio:
                raise ProviderError("selftest: TTS produced nothing")
            # 2. downsample 24k -> 16k and feed the far-end path
            import audioop
            pcm16k, _ = audioop.ratecv(audio, 2, 1, 24000, 16000, None)
            await self.en2zh(pcm16k)
            await self.send_json({"t": "selftest_end"})
        except Exception as e:                            # noqa: BLE001
            await self.error("selftest", e)

    async def _say(self, text: str, direction: str) -> None:
        """Typed text -> translated speech into the call (or to the user)."""
        try:
            translated, audio = await self._text_pipeline(text, direction)
            if translated:
                await self.send_json({"t": f"{direction}_text", "src": text,
                                      "text": translated})
            if audio:
                ch = CH_CALL if direction == "zh2en" else CH_USER
                self.stats["en_bytes" if ch == CH_CALL else "zh_bytes"] += len(audio)
                await self.send_audio(ch, audio)
                await self.send_json({"t": f"{direction}_audio", "n": len(audio)})
        except Exception as e:                            # noqa: BLE001
            await self.error("say", e)

    async def _text_pipeline(self, text: str, direction: str) -> tuple[str, bytes]:
        translated = await self.pipeline.text_translate(text, direction)
        if not translated:
            return "", b""
        audio = await self.pipeline.to_speech(translated, direction)
        return translated, audio


# ------------------------------------------------------------------ ws handler
CONFIG_PATH: str = ""       # set by main(); reloaded per connection so edits apply live


def token_ok(ws, cfg: Config) -> bool:
    if not cfg.access_token:
        return True
    req = getattr(ws, "request", None)
    path = getattr(req, "path", "") if req else ""
    return f"k={cfg.access_token}" in path


async def handler(ws):
    cfg = Config.load(CONFIG_PATH or None)   # reload: config edits apply on reconnect
    if not token_ok(ws, cfg):
        await ws.close(code=4401, reason="unauthorized")
        return
    peer = ws.remote_address
    log.info("browser connected %s", peer)
    sess = Session(ws, cfg)
    await sess.send_json({"t": "config", "cfg": public_config(cfg)})
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                await sess.handle_binary(msg)
            else:
                try:
                    await sess.handle_json(json.loads(msg))
                except json.JSONDecodeError:
                    continue
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        log.info("browser gone; stats=%s", sess.stats)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="")
    ap.add_argument("--config", default="")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    global CONFIG_PATH
    CONFIG_PATH = a.config or ""
    cfg = Config.load(a.config or None)
    host = a.host or cfg.host
    port = a.port or cfg.port

    try:
        Pipeline(cfg)            # fail fast on a bad provider config
    except ProviderError as e:
        raise SystemExit(f"configuration error: {e}")

    async with serve(handler, host, port, process_request=read_http,
                     max_size=None, ping_interval=20, ping_timeout=20):
        log.info("call-interpreter listening on http://%s:%d/", host, port)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
