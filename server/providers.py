"""Pluggable translation providers.

Every provider speaks the same tiny contract so the pipeline never cares which
vendor is behind it:

    async def audio_to_text(pcm16, rate, prompt, target_lang_hint) -> str
    async def text_to_speech(text, voice) -> bytes   # PCM16 @ 24 kHz

A provider only implements what it can do; the pipeline checks `capabilities`
and falls back to the cascade when a fused provider is not configured.

Adding a vendor = one class + one registry entry. No pipeline changes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile

import aiohttp

from .audio import pcm_to_wav

log = logging.getLogger("providers")


def _find_tool(name: str, *fallbacks: str) -> str:
    """Locate an external binary, preferring this app's own venv.

    The old code hardcoded the Hermes venv path, which works on the dev machine
    and breaks the moment the app is installed anywhere else. Look next to our
    own interpreter first, then PATH, then a few known locations.
    """
    here = pathlib.Path(sys.executable).parent
    for cand in (here / name, *map(pathlib.Path, fallbacks)):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    found = shutil.which(name)
    return found or name


def _resolve(name: str, *fallbacks: str) -> str:
    return _find_tool(name, *fallbacks)


EDGE_TTS = _resolve("edge-tts", "/usr/local/bin/edge-tts", "/usr/bin/edge-tts")
FFMPEG = _resolve("ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg")


class ProviderError(RuntimeError):
    """Raised for any upstream failure; the session reports it to the browser."""


# --------------------------------------------------------------------------- base
class BaseProvider:
    """capabilities: subset of {'stt', 'mt', 'tts', 'fused'}."""

    capabilities: set[str] = set()

    def __init__(self, spec):
        self.spec = spec

    @property
    def name(self) -> str:
        return self.spec.provider

    async def audio_to_text(self, pcm16: bytes, rate: int, prompt: str,
                            target: str = "") -> str:
        raise NotImplementedError

    async def text_translate(self, text: str, prompt: str) -> str:
        """Translate already-transcribed text (used by the type-instead-of-speak path)."""
        raise NotImplementedError

    async def text_to_speech(self, text: str, voice: str) -> bytes:
        raise NotImplementedError


async def with_retry(fn, *, attempts: int = 3, base_delay: float = 0.6,
                     what: str = "provider"):
    """Retry a flaky upstream call.

    Measured: gemini-flash-lite-latest answers in ~1.1 s when it answers at all
    and times out roughly one request in two. Dropping every other sentence
    mid-call is not survivable — the other party simply never hears it — so a
    transient failure is retried a couple of times before it is reported.
    Only transport/5xx/429 style failures are retried; a 4xx that means "your
    request is wrong" is not going to fix itself.
    """
    last = None
    for k in range(max(1, attempts)):
        try:
            return await fn()
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last = e
        except ProviderError as e:
            msg = str(e)
            # 4xx that is not 408/429 = our fault, retrying will not help.
            if any(f" {c}" in msg for c in (400, 401, 403, 404, 422)):
                raise
            last = e
        if k < attempts - 1:
            await asyncio.sleep(base_delay * (2 ** k))
    log.warning("%s failed after %d attempts: %s", what, attempts, last)
    raise last if last else ProviderError(f"{what}: unknown failure")


# ------------------------------------------------------------------------ OpenAI
class OpenAIProvider(BaseProvider):
    """Any OpenAI-compatible /chat/completions + /audio/speech endpoint.

    Covers OpenAI itself, new-api / one-api gateways, SiliconFlow, DeepSeek,
    local vLLM — anything that speaks the same wire format.
    """

    capabilities = {"stt", "mt", "tts", "fused"}

    def _url(self, path: str) -> str:
        base = (self.spec.base_url or "https://api.openai.com/v1").rstrip("/")
        if not base.endswith("/v1") and "/v1" not in base:
            base += "/v1"
        return base + path

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.spec.resolved_key()}",
                "Content-Type": "application/json"}

    async def audio_to_text(self, pcm16: bytes, rate: int, prompt: str,
                            target: str = "") -> str:
        """One shot: audio in, translated text out (fused mode)."""
        wav = pcm_to_wav(pcm16, rate)
        b64 = base64.b64encode(wav).decode()
        body = {
            "model": self.spec.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio",
                     "input_audio": {"data": b64, "format": "wav"}},
                ],
            }],
            "temperature": 0.2,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(self._url("/chat/completions"), json=body,
                              headers=self._headers(),
                              timeout=aiohttp.ClientTimeout(total=self.spec.timeout)) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise ProviderError(
                        f"{self.name} {r.status}: {json.dumps(data)[:200]}")
                return (data["choices"][0]["message"]["content"] or "").strip()

    async def text_translate(self, text: str, prompt: str) -> str:
        async def call() -> str:
            body = {
                "model": self.spec.model,
                "messages": [{"role": "user", "content": f"{prompt}\n\n{text}"}],
                "temperature": 0.2,
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(self._url("/chat/completions"), json=body,
                                  headers=self._headers(),
                                  timeout=aiohttp.ClientTimeout(total=self.spec.timeout)) as r:
                    data = await r.json(content_type=None)
                    if r.status != 200:
                        raise ProviderError(
                            f"{self.name} {r.status}: {json.dumps(data)[:200]}")
                    return (data["choices"][0]["message"]["content"] or "").strip()
        return await with_retry(call, what=f"{self.name} mt")

    async def text_to_speech(self, text: str, voice: str) -> bytes:
        async def call() -> bytes:
            body = {"model": self.spec.model or "tts-1", "input": text,
                    "voice": voice or "alloy", "response_format": "pcm"}
            async with aiohttp.ClientSession() as s:
                async with s.post(self._url("/audio/speech"), json=body,
                                  headers=self._headers(),
                                  timeout=aiohttp.ClientTimeout(total=self.spec.timeout)) as r:
                    if r.status != 200:
                        txt = await r.text()
                        raise ProviderError(f"{self.name} tts {r.status}: {txt[:200]}")
                    return await r.read()
        return await with_retry(call, what=f"{self.name} tts")


# ------------------------------------------------------------------------ Gemini
class GeminiProvider(BaseProvider):
    """Google Generative Language API (audio understanding + text)."""

    capabilities = {"stt", "mt", "fused"}
    DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"

    def _url(self, model: str) -> str:
        base = (self.spec.base_url or self.DEFAULT_BASE).rstrip("/")
        return f"{base}/models/{model}:generateContent"

    async def _generate(self, parts: list[dict]) -> str:
        key = self.spec.resolved_key()
        if not key:
            raise ProviderError("gemini: missing api_key")

        async def call() -> str:
            # A fresh session per attempt: a pooled connection that just timed
            # out is exactly the thing we are retrying away from.
            async with aiohttp.ClientSession() as s:
                async with s.post(self._url(self.spec.model),
                                  json={"contents": [{"parts": parts}]},
                                  headers={"x-goog-api-key": key,
                                           "Content-Type": "application/json"},
                                  timeout=aiohttp.ClientTimeout(total=self.spec.timeout)) as r:
                    data = await r.json(content_type=None)
                    if r.status != 200:
                        raise ProviderError(f"gemini {r.status}: {json.dumps(data)[:200]}")
                    try:
                        return (data["candidates"][0]["content"]["parts"][0]["text"] or "").strip()
                    except (KeyError, IndexError):
                        raise ProviderError(f"gemini: unexpected payload {json.dumps(data)[:200]}")

        return await with_retry(call, what="gemini")

    async def audio_to_text(self, pcm16: bytes, rate: int, prompt: str,
                            target: str = "") -> str:
        b64 = base64.b64encode(pcm16).decode()
        return await self._generate([
            {"text": prompt},
            {"inline_data": {"mime_type": f"audio/l16;rate={rate}", "data": b64}},
        ])

    async def text_translate(self, text: str, prompt: str) -> str:
        return await self._generate([{"text": f"{prompt}\n\n{text}"}])

    async def text_only(self, prompt: str) -> str:
        return await self._generate([{"text": prompt}])


# -------------------------------------------------------------------- Edge (free)
class EdgeProvider(BaseProvider):
    """Microsoft Edge's public TTS endpoint via the `edge-tts` CLI.

    Free, no key, hundreds of voices, and the only stage that reliably gives us
    the British male voice a UK bank expects to hear. Shelled out rather than
    reimplemented because edge-tts tracks the upstream token dance for us.
    """

    capabilities = {"tts"}

    async def text_to_speech(self, text: str, voice: str) -> bytes:
        if not text.strip():
            return b""
        voice = voice or "en-GB-RyanNeural"

        async def call() -> bytes:
            with tempfile.TemporaryDirectory() as td:
                mp3 = pathlib.Path(td) / "out.mp3"
                pcm = pathlib.Path(td) / "out.pcm"
                p1 = await asyncio.create_subprocess_exec(
                    EDGE_TTS, "--voice", voice, "--text", text,
                    "--write-media", str(mp3),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE)
                _, err = await p1.communicate()
                if p1.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
                    raise ProviderError(
                        f"edge-tts failed: {err.decode(errors='replace')[:200]}")
                # 24 kHz mono PCM16: what the browser plays into the call.
                p2 = await asyncio.create_subprocess_exec(
                    FFMPEG, "-y", "-loglevel", "error", "-i", str(mp3),
                    "-ar", "24000", "-ac", "1", "-f", "s16le", str(pcm),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE)
                _, err = await p2.communicate()
                if p2.returncode != 0:
                    raise ProviderError(
                        f"ffmpeg failed: {err.decode(errors='replace')[:200]}")
                return pcm.read_bytes()

        # The public endpoint drops a request now and then (observed: no output
        # file at all, exit 0). One retry turns a dropped sentence into a
        # slightly late one, which is the better failure for a live call.
        return await with_retry(call, attempts=3, base_delay=0.4, what="edge-tts")


# ------------------------------------------------------------------- registry
_REGISTRY: dict[str, type[BaseProvider]] = {
    "openai": OpenAIProvider,
    "openai-compatible": OpenAIProvider,
    "siliconflow": OpenAIProvider,   # same wire format, different base_url
    "deepseek": OpenAIProvider,
    "newapi": OpenAIProvider,
    "gemini": GeminiProvider,
    "google": GeminiProvider,
    "edge": EdgeProvider,
    "edge-tts": EdgeProvider,
}


def build(spec) -> BaseProvider | None:
    """Instantiate the provider named in the config, or None if unset/unknown."""
    if not spec or not spec.provider:
        return None
    cls = _REGISTRY.get(spec.provider.lower())
    if cls is None:
        raise ProviderError(
            f"unknown provider '{spec.provider}' "
            f"(known: {', '.join(sorted(_REGISTRY))})")
    return cls(spec)


def known_providers() -> list[str]:
    return sorted(_REGISTRY)
