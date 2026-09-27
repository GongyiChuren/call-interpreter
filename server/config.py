"""Configuration: one YAML file, reloaded on every call so edits need no restart."""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.yaml"


def load_dotenv(paths: list[pathlib.Path] | None = None) -> None:
    """Read KEY=VALUE files into the environment without overriding real env vars.

    Checked in order: the project's own .env first, then Hermes' .env so an
    existing key (GOOGLE_API_KEY, SILICONFLOW_API_KEY, ...) can be reused by
    reference — `api_key: env:GOOGLE_API_KEY` — instead of copied around.
    """
    if paths is None:
        paths = [ROOT / ".env", pathlib.Path.home() / ".hermes" / ".env"]
    for p in paths:
        try:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        except OSError:
            continue



@dataclass
class VoiceSpec:
    """Which TTS voice renders a language, and where it comes from."""
    provider: str = "edge"          # edge | openai
    voice: str = "en-GB-RyanNeural"
    model: str = ""                 # provider-specific (e.g. an OpenAI TTS model)


@dataclass
class ProviderSpec:
    """One pluggable API endpoint: STT / MT / TTS all use this same shape."""
    provider: str = ""              # gemini | openai | edge | passthrough
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 30.0

    def resolved_key(self) -> str:
        """`api_key: env:NAME` reads NAME from the environment at call time."""
        if self.api_key.startswith("env:"):
            return os.environ.get(self.api_key[4:], "").strip()
        return self.api_key.strip()


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8790
    access_token: str = ""

    # --- translation pipelines -------------------------------------------
    # "fused"    : one model call turns audio directly into the target text
    # "cascade"  : STT -> MT -> TTS, each stage independently swappable
    mode: str = "fused"
    fused: ProviderSpec = field(default_factory=ProviderSpec)
    stt: ProviderSpec = field(default_factory=ProviderSpec)
    mt: ProviderSpec = field(default_factory=ProviderSpec)
    tts: ProviderSpec = field(default_factory=ProviderSpec)

    # Voice rendered when we speak English to the other party (must be male
    # and ideally British — a UK bank agent reacts badly to a mismatched voice).
    out_voice: VoiceSpec = field(default_factory=lambda: VoiceSpec(
        provider="edge", voice="en-GB-RyanNeural"))
    # Voice rendered when we read the other party back in Chinese (optional).
    in_voice: VoiceSpec = field(default_factory=lambda: VoiceSpec(
        provider="edge", voice="zh-CN-YunxiNeural"))

    # --- SIP / softphone --------------------------------------------------
    sip_ws_url: str = ""            # e.g. wss://100.85.36.110:8099/ws
    sip_uri: str = ""               # e.g. sip:webrtc@100.85.36.110:8099
    sip_user: str = ""
    sip_password: str = ""
    sip_display_name: str = "Interpreter"
    sip_register: bool = True

    system_prompt_zh2en: str = (
        "You are interpreting a live telephone call. The caller speaks Mandarin "
        "Chinese; render it as natural spoken English for the other party. "
        "Output only the English, with no quotes and no commentary.")
    system_prompt_en2zh: str = (
        "You are interpreting a live telephone call. The other party speaks "
        "English; render it as natural spoken Mandarin Chinese. "
        "Use Simplified Chinese characters only (not Traditional). "
        "Output only the Chinese, with no quotes and no commentary.")

    _path: pathlib.Path = field(default=DEFAULT_PATH, repr=False)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | pathlib.Path | None = None) -> "Config":
        load_dotenv()
        p = pathlib.Path(path) if path else DEFAULT_PATH
        raw = {}
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        def spec(node: dict | None, **dflt) -> ProviderSpec:
            node = node or {}
            return ProviderSpec(
                provider=node.get("provider", dflt.get("provider", "")),
                base_url=node.get("base_url", dflt.get("base_url", "")),
                api_key=node.get("api_key", dflt.get("api_key", "")),
                model=node.get("model", dflt.get("model", "")),
                timeout=float(node.get("timeout", dflt.get("timeout", 30.0))),
            )

        def voice(node: dict | None, dflt: VoiceSpec) -> VoiceSpec:
            node = node or {}
            return VoiceSpec(provider=node.get("provider", dflt.provider),
                             voice=node.get("voice", dflt.voice),
                             model=node.get("model", dflt.model))

        sr = raw.get("server", {}) or {}
        interp = raw.get("interpreter", {}) or {}
        sip = raw.get("sip", {}) or {}
        prompts = raw.get("prompts", {}) or {}

        cfg = cls(
            host=sr.get("host", cls.host),
            port=int(sr.get("port", cls.port)),
            access_token=str(sr.get("access_token", "") or ""),
            mode=interp.get("mode", cls.mode),
            fused=spec(interp.get("fused"), provider="gemini"),
            stt=spec(interp.get("stt"), provider="gemini"),
            mt=spec(interp.get("mt"), provider="gemini"),
            tts=spec(interp.get("tts"), provider="edge"),
            sip_ws_url=sip.get("ws_url", ""),
            sip_uri=sip.get("uri", ""),
            sip_user=sip.get("user", ""),
            sip_password=sip.get("password", ""),
            sip_display_name=sip.get("display_name", cls.sip_display_name),
            sip_register=bool(sip.get("register", True)),
            system_prompt_zh2en=prompts.get("zh2en", cls.system_prompt_zh2en),
            system_prompt_en2zh=prompts.get("en2zh", cls.system_prompt_en2zh),
            _path=p,
        )
        cfg.out_voice = voice(interp.get("out_voice"), cls().out_voice)
        cfg.in_voice = voice(interp.get("in_voice"), cls().in_voice)
        return cfg

    def as_public_dict(self) -> dict:
        """Everything the browser needs — never includes API keys."""
        return {
            "mode": self.mode,
            "outVoice": {"provider": self.out_voice.provider,
                         "voice": self.out_voice.voice},
            "inVoice": {"provider": self.in_voice.provider,
                        "voice": self.in_voice.voice},
            "sip": {
                "wsUrl": self.sip_ws_url,
                "uri": self.sip_uri or self.sip_user,
                "user": self.sip_user,
                "displayName": self.sip_display_name,
                "register": self.sip_register,
                # Served to the page over the same WebSocket the audio flows on.
                # Only do this when the whole app is behind access_token or bound
                # to loopback — see the README's security section.
                "password": self.sip_password,
            },
        }
