#!/usr/bin/env python3
"""End-to-end test of the pipeline, using the real providers (no mocks).

Feeds real Mandarin audio in, checks that English audio comes out, and reports
the latency of each stage. Run this after changing a provider or a prompt:

    /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/e2e_pipeline.py
"""
from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server.audio import rms, SpeechSegmenter          # noqa: E402
from server.config import Config                       # noqa: E402
from server.pipeline import Pipeline                   # noqa: E402

EDGE_TTS = "/home/ubuntu/.hermes/hermes-agent/venv/bin/edge-tts"
FFMPEG = "/usr/bin/ffmpeg"
TMP = pathlib.Path("/tmp/ci-e2e")
TMP.mkdir(exist_ok=True)


def synth(text: str, voice: str, rate: int = 16000) -> bytes:
    """Render text to PCM16 mono at `rate` using the free Edge voices."""
    mp3, pcm = TMP / "a.mp3", TMP / "a.pcm"
    subprocess.run([EDGE_TTS, "--voice", voice, "--text", text,
                    "--write-media", str(mp3)], check=True, capture_output=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(mp3),
                    "-ar", str(rate), "-ac", "1", "-f", "s16le", str(pcm)],
                   check=True, capture_output=True)
    return pcm.read_bytes()


def pcm_to_wav(pcm: bytes, rate: int, path: pathlib.Path) -> pathlib.Path:
    import struct
    n = len(pcm)
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", n) + pcm)
    return path


async def main() -> int:
    cfg = Config.load()
    pipe = Pipeline(cfg)
    print(f"mode={cfg.mode} fused={cfg.fused.provider}/{cfg.fused.model} "
          f"tts={cfg.tts.provider} out_voice={cfg.out_voice.voice}\n")

    failures = 0

    # ---------------------------------------------------------------- zh -> en
    print("=" * 74)
    print("方向 1 · 中文语音 -> 英文语音（对方要听到的那个方向）")
    zh_text = "你好，我想变更一下我的邮寄地址，请问需要提供什么材料？"
    zh_pcm = synth(zh_text, "zh-CN-YunxiNeural")
    print(f"  输入: {len(zh_pcm)/2/16000:.1f}s 中文语音 —「{zh_text}」")

    t0 = time.time()
    src, en = await pipe.to_text(zh_pcm, 16000, "zh2en")
    t_text = time.time() - t0
    print(f"  [{t_text:5.2f}s] 英译: {en}")
    if not en or not any(c.isascii() and c.isalpha() for c in en):
        print("  ✗ 没有拿到英文")
        failures += 1

    t0 = time.time()
    audio = await pipe.to_speech(en, "zh2en")
    t_speech = time.time() - t0
    print(f"  [{t_speech:5.2f}s] 英文语音: {len(audio)} bytes "
          f"({len(audio)/2/24000:.1f}s @24kHz)  voice={cfg.out_voice.voice}")
    if len(audio) < 8000:
        print("  ✗ 语音太短")
        failures += 1
    else:
        pcm_to_wav(audio, 24000, TMP / "out_en.wav")

    total = t_text + t_speech
    print(f"  → 端到端 {total:.2f}s "
          f"({'✓ 可接受' if total < 4 else '⚠ 偏慢'})")

    # ---------------------------------------------------------------- en -> zh
    print("=" * 74)
    print("方向 2 · 英文语音 -> 中文文本（你屏幕上看的那个方向）")
    en_text = ("Thank you for calling Lloyds Bank. To change the address on your "
               "account I will need to verify your identity first.")
    en_pcm = synth(en_text, "en-GB-RyanNeural")
    print(f"  输入: {len(en_pcm)/2/16000:.1f}s 英文语音 —「{en_text[:58]}…」")

    t0 = time.time()
    src, zh = await pipe.to_text(en_pcm, 16000, "en2zh")
    t = time.time() - t0
    print(f"  [{t:5.2f}s] 中译: {zh}")
    if not zh or not any("\u4e00" <= c <= "\u9fff" for c in zh):
        print("  ✗ 没有拿到中文")
        failures += 1

    # ------------------------------------------------------ segmenter sanity
    print("=" * 74)
    print("方向 3 · 断句器（决定一段话什么时候算说完）")
    seg = SpeechSegmenter(16000)
    # two utterances separated by 1 s of silence
    stream = zh_pcm + b"\x00" * (16000 * 2) + zh_pcm
    got, pos = [], 0
    for i in range(0, len(stream), 640):          # feed in 20 ms frames
        out = seg.push(stream[i:i + 640])
        if out:
            got.append(len(out))
            pos += 1
    print(f"  输入含 2 段话 + 中间 1s 静音 -> 切出 {len(got)} 段 {got}")
    if len(got) != 2:
        print("  ✗ 断句不正确")
        failures += 1
    else:
        print("  ✓ 断句正确")

    # ------------------------------------------------------------------ verdict
    print("=" * 74)
    if failures:
        print(f"✗ {failures} 项失败")
    else:
        print("✓ 全部通过")
        print(f"  产物: {TMP/'out_en.wav'}  <- 可直接播放，听这段英文是否自然")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
