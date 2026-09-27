#!/usr/bin/env python3
"""Preflight: is everything this app needs actually in place?

Run before opening the page:

    /opt/call-interpreter/.venv/bin/python check.py

Checks Python deps, ffmpeg, the TTS voices, the translation provider (with a
real API call), and the SIP gateway's reachability. Prints one line per check
and exits non-zero if something that would break a call is missing.
"""
from __future__ import annotations

import asyncio
import pathlib
import shutil
import socket
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from server.config import Config                     # noqa: E402
from server.pipeline import Pipeline                 # noqa: E402
from server.providers import ProviderError           # noqa: E402

OK, BAD, WARN = "  ✓", "  ✗", "  !"
failures = 0


def report(mark: str, label: str, detail: str = "") -> None:
    global failures
    if mark == BAD:
        failures += 1
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    print("Call Interpreter — preflight\n")

    # ---------------------------------------------------------------- runtime
    print("运行环境")
    py = sys.version_info
    report(OK if py >= (3, 11) else BAD, f"Python {py.major}.{py.minor}.{py.micro}",
           "" if py >= (3, 11) else "需要 3.11+")

    for mod in ("websockets", "aiohttp", "yaml", "audioop"):
        try:
            __import__(mod)
            report(OK, f"模块 {mod}")
        except ImportError as e:
            report(BAD, f"模块 {mod}", str(e))

    from server import providers as _p
    ff = _p.FFMPEG if pathlib.Path(_p.FFMPEG).is_file() else shutil.which("ffmpeg")
    report(OK if ff else BAD, "ffmpeg", str(ff) if ff else "未安装（TTS 需要它转码）")
    report(OK if pathlib.Path(_p.EDGE_TTS).is_file() else WARN, "edge-tts",
           _p.EDGE_TTS if pathlib.Path(_p.EDGE_TTS).is_file()
           else "未安装；若 interpreter.tts 用 edge 就没法出声")

    # ----------------------------------------------------------------- config
    print("\n配置")
    cfg = Config.load()
    report(OK, f"监听 {cfg.host}:{cfg.port}")
    report(OK if cfg.access_token else WARN, "访问口令",
           "已设置" if cfg.access_token else "未设置 — 任何人都能打开这个页面并用你的线路打电话")
    report(OK, f"翻译模式 {cfg.mode}")

    # -------------------------------------------------------------- providers
    print("\n翻译与语音")
    try:
        pipe = Pipeline(cfg)
        report(OK, f"翻译提供方 {cfg.fused.provider or cfg.stt.provider}",
               f"模型 {cfg.fused.model or cfg.mt.model}")
    except ProviderError as e:
        report(BAD, "翻译提供方", str(e))
        return 1

    # A real round trip: short Chinese text -> English text -> English audio.
    try:
        text = await pipe.text_translate("你好，测试一下。", "zh2en")
        report(OK if text else BAD, "翻译实测", text[:60] or "返回空")
    except Exception as e:                                  # noqa: BLE001
        report(BAD, "翻译实测", f"{type(e).__name__}: {e}"[:140])

    try:
        audio = await pipe.to_speech("This is a test.", "zh2en")
        secs = len(audio) / 2 / 24000
        report(OK if secs > 0.3 else BAD,
               f"语音实测 voice={cfg.out_voice.voice}",
               f"{secs:.1f}s 音频" if secs > 0.3 else "音频太短或为空")
    except Exception as e:                                  # noqa: BLE001
        report(BAD, "语音实测", f"{type(e).__name__}: {e}"[:140])

    # -------------------------------------------------------------------- SIP
    print("\nSIP 网关")
    if not cfg.sip_ws_url:
        report(WARN, "未配置",
               "config.yaml 的 sip.ws_url 为空 — 页面能用，但拨不了号")
    else:
        url = urllib.parse.urlparse(cfg.sip_ws_url)
        host, port = url.hostname or "", url.port or (443 if url.scheme == "wss" else 80)
        report(OK, f"目标 {url.scheme}://{host}:{port}")
        try:
            with socket.create_connection((host, port), timeout=6):
                report(OK, "端口可达")
        except OSError as e:
            report(BAD, "端口可达", f"{e}（网关没开？先启动 MDD/VoWiFi 网关）")
        report(OK if cfg.sip_user else WARN, "SIP 账号",
               cfg.sip_user or "未填（或改成在页面上填）")

    print()
    if failures:
        print(f"✗ {failures} 项有问题，先解决再打电话。")
    else:
        print("✓ 全部就绪。启动服务后在浏览器打开 http://<本机IP>:%d/" % cfg.port)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
