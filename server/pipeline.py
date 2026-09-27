"""The translation pipeline.

Two shapes, chosen in config:

  fused    audio(zh) --[one model call]--> text(en) --[tts]--> audio(en)
  cascade  audio(zh) --[stt]--> text(zh) --[mt]--> text(en) --[tts]--> audio(en)

Fused is fewer round trips (measured 1.9 s + 1.0 s TTS vs 8-10 s for a naive
cascade), so it is the default. Cascade exists for vendors that only sell one
piece, and because the intermediate text is genuinely useful to see when a
transcription goes wrong mid-call.
"""
from __future__ import annotations

import logging

from . import providers
from .providers import ProviderError

log = logging.getLogger("pipeline")


class Pipeline:
    """Wraps whichever providers the config selected behind one interface."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.fused = providers.build(cfg.fused)
        self.stt = providers.build(cfg.stt)
        self.mt = providers.build(cfg.mt)
        self.tts = providers.build(cfg.tts)

        if cfg.mode == "fused" and self.fused is None:
            raise ProviderError(
                "mode=fused needs an interpreter.fused provider "
                "(or set interpreter.mode: cascade)")
        if cfg.mode == "cascade" and (self.stt is None or self.mt is None):
            raise ProviderError(
                "mode=cascade needs interpreter.stt and interpreter.mt providers")

    # ------------------------------------------------------------------ helpers
    def _prompt(self, direction: str) -> str:
        return (self.cfg.system_prompt_zh2en if direction == "zh2en"
                else self.cfg.system_prompt_en2zh)

    def _voice(self, direction: str) -> str:
        return (self.cfg.out_voice.voice if direction == "zh2en"
                else self.cfg.in_voice.voice)

    def _translator(self):
        t = self.fused or self.mt
        if t is None:
            raise ProviderError("no translation provider configured")
        return t

    # -------------------------------------------------------------------- audio
    async def audio_to_text(self, pcm16: bytes, rate: int, direction: str) -> tuple[str, str]:
        """Audio -> translated text. Returns (source_text, translated_text).

        `source_text` is only filled in cascade mode; in fused mode the model
        returns the translation directly and the original is not available.
        """
        prompt = self._prompt(direction)
        target = "en" if direction == "zh2en" else "zh"

        if self.cfg.mode == "cascade":
            src = await self.stt.audio_to_text(
                pcm16, rate, "Transcribe this speech verbatim.", target)
            if not src.strip():
                return "", ""
            return src, await self.text_translate(src, direction)

        return "", await self.fused.audio_to_text(pcm16, rate, prompt, target)

    async def text_translate(self, text: str, direction: str) -> str:
        """Transcribe-and-translate for text input (the type-instead-of-speak path)."""
        if not text.strip():
            return ""
        prompt = self._prompt(direction)
        translator = self._translator()
        if hasattr(translator, "text_translate"):
            return await translator.text_translate(text, prompt)
        return await self._text_via_chat(text, prompt)

    # ------------------------------------------------------------------- speech
    async def to_speech(self, text: str, direction: str) -> bytes:
        """Translated text -> PCM16 @ 24 kHz in the voice configured for `direction`."""
        if not text.strip():
            return b""
        provider = self.tts or self.fused
        if provider is None or "tts" not in provider.capabilities:
            raise ProviderError(
                "no TTS provider configured (set interpreter.tts, e.g. provider: edge)")
        return await provider.text_to_speech(text, self._voice(direction))

    # ---------------------------------------------------------------- internals
    async def _text_via_chat(self, text: str, prompt: str) -> str:
        """Text-only translation for providers lacking a `text_translate`."""
        import aiohttp
        import json
        spec = self._translator().spec
        base = (spec.base_url or "https://api.openai.com/v1").rstrip("/")
        if "/v1" not in base:
            base += "/v1"
        body = {"model": spec.model,
                "messages": [{"role": "user", "content": f"{prompt}\n\n{text}"}],
                "temperature": 0.2}
        async with aiohttp.ClientSession() as s:
            async with s.post(base + "/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {spec.resolved_key()}",
                                       "Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=spec.timeout)) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise ProviderError(f"mt {r.status}: {json.dumps(data)[:200]}")
                return (data["choices"][0]["message"]["content"] or "").strip()
