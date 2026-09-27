"""Audio primitives: resampling, RMS gating, PCM helpers.

Sample rates used across the project:
  browser mic / far-end capture : PCM16 mono @ 16 kHz
  translated playback           : PCM16 mono @ 24 kHz
  SIP / RTP leg (G.711)         : mu-law @ 8 kHz   (handled by the browser's WebRTC
                                    stack, so the server never touches mu-law itself)
"""
from __future__ import annotations

import audioop
import struct

PCM16K_FRAME = 640      # 20 ms @ 16 kHz
PCM24K_FRAME = 960      # 20 ms @ 24 kHz

# Below this RMS a 20 ms frame counts as silence. Chosen against real speech
# recorded from a laptop mic: quiet speech sits around 400-900, room tone < 150.
VOICE_RMS_GATE = 200


class RateConverter:
    """Stateful resampler (keeps the filter state across calls, unlike a one-shot)."""

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._state = None

    def convert(self, pcm16: bytes) -> bytes:
        if self.src_rate == self.dst_rate:
            return pcm16
        out, self._state = audioop.ratecv(
            pcm16, 2, 1, self.src_rate, self.dst_rate, self._state)
        return out


def rms(pcm16: bytes) -> int:
    if len(pcm16) < 2:
        return 0
    return audioop.rms(pcm16, 2)


def is_voice(pcm16: bytes, gate: int = VOICE_RMS_GATE) -> bool:
    return rms(pcm16) >= gate


def pcm_to_wav(pcm16: bytes, rate: int = 16000) -> bytes:
    """Wrap raw PCM16 mono in a WAV container (what most STT HTTP APIs want)."""
    n = len(pcm16)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", n) + pcm16)


class SpeechSegmenter:
    """Splits a continuous stream into utterances.

    Used on the far-end (English) leg, where the audio arrives without any
    client-side gating: we must decide for ourselves when the other party
    stopped talking so the segment can be transcribed and translated.
    """

    def __init__(self, sample_rate: int = 16000, gate: int = VOICE_RMS_GATE,
                 head_ms: int = 200, tail_ms: int = 700, min_ms: int = 300,
                 max_ms: int = 15000):
        self.sr = sample_rate
        self.gate = gate
        self.head_bytes = int(sample_rate * head_ms / 1000) * 2
        self.tail_bytes = int(sample_rate * tail_ms / 1000) * 2
        self.min_bytes = int(sample_rate * min_ms / 1000) * 2
        self.max_bytes = int(sample_rate * max_ms / 1000) * 2
        self._voiced = False
        self._head = bytearray()   # held pre-roll silence
        self._body = bytearray()   # the utterance itself
        self._tail = bytearray()   # trailing silence, pending more speech

    def push(self, pcm: bytes) -> bytes | None:
        """Feed one chunk. Returns the finished utterance, or None."""
        voiced = is_voice(pcm, self.gate)

        if voiced:
            if not self._voiced:
                self._voiced = True
                self._body = bytearray(self._head[-self.head_bytes:])
                self._head.clear()
            self._tail.clear()
            self._body.extend(pcm)
            if len(self._body) >= self.max_bytes:
                return self._cut()
            return None

        if not self._voiced:
            self._head.extend(pcm)
            if len(self._head) > self.head_bytes * 4:
                del self._head[: len(self._head) - self.head_bytes * 4]
            return None

        # trailing silence while an utterance is open
        self._tail.extend(pcm)
        if len(self._tail) >= self.tail_bytes:
            self._body.extend(self._tail)
            return self._cut()
        return None

    def _cut(self) -> bytes | None:
        body = bytes(self._body)
        self._body = bytearray()
        self._tail.clear()
        self._voiced = False
        return body if len(body) >= self.min_bytes else None

    def flush(self) -> bytes | None:
        if len(self._body) >= self.min_bytes:
            return self._cut()
        self._body = bytearray()
        self._tail.clear()
        self._voiced = False
        return None


class RmsGate:
    """Keeps a short tail so words are not clipped, drops dead air.

    Applied to synthesized playback: the TTS/text pipeline inserts silence
    between utterances and we do not want to ship that silence into a live
    phone call, where it would just be dead air on the other end.
    """

    def __init__(self, sample_rate: int = 24000, gate: int = VOICE_RMS_GATE,
                 keep_ms: int = 300):
        self.gate = gate
        self.keep = int(sample_rate * keep_ms / 1000) * 2 // PCM16K_FRAME + 1
        self._tail = 0

    def accept(self, pcm: bytes) -> bool:
        if is_voice(pcm, self.gate):
            self._tail = self.keep
            return True
        if self._tail > 0:
            self._tail -= 1
            return True
        return False
