# Call Interpreter · 通话翻译

给电话通话做实时口译的开源工具。你在浏览器里说话（中文），对方在电话里听到
英语；对方说英语，你在屏幕上看到中文。

它是**一个网页**，同时是软电话和翻译台：

- 浏览器直接注册到你的 SIP/WebRTC 网关（VoWiFi / Asterisk / FreeSWITCH 都行）
- 说话 → 翻译 → 合成的英语**接进这通电话**，对方只听到英语
- 按住空格（或按钮）才翻译你说的内容，松手即发送 — 不会把环境噪音和对方的
  声音翻译回去
- 翻译和语音的每一环都能换成别的厂商，改 `config.yaml` 即可，不用改代码

```
浏览器  ──SIP/WSS──▶  VoWiFi 网关 ──▶ 运营商 ──▶ 对方
   │  ▲
   │  └── 合成英语（替换通话的上行音轨）
   ▼
本服务 ──▶ 翻译 API ──▶ TTS
```

---

## 安全警告（先读这一段）

这个页面**能让任何打开它的人用你的电话线路打电话**，并且 `config.yaml` 里的
SIP 密码会发给浏览器。所以：

- **不要直接暴露到公网**。默认只监听本机时最安全。
- 需要在外面用，就加访问口令（`server.access_token`，访问 `/?k=口令`），并且
  **套一层 HTTPS 反向代理或 Cloudflare Tunnel**。浏览器只允许 HTTPS 页面开麦克风。
- 只在自己的网络上跑，或者只给信得过的人。

---

## 快速开始

```bash
sudo ./install.sh            # 装依赖、建 venv、注册 systemd 服务并启动
$EDITOR /opt/call-interpreter/config.yaml
sudo systemctl restart call-interpreter@$USER
python3 /opt/call-interpreter/check.py      # 自检：依赖、翻译、语音、网关
```

然后浏览器打开 `http://<本机IP>:8790/`。

不想装服务，先跑起来看看：

```bash
python3 -m venv .venv && .venv/bin/pip install websockets aiohttp PyYAML edge-tts
.venv/bin/python -m server.app --port 8790
```

---

## 配置

全部在 `config.yaml`，改动**重连即生效**，不用重启服务。

### 翻译：两种接法

**`mode: fused`（默认，快）** — 一次调用把语音直接变成目标语言文本。
实测中文语音 6 秒 → 英文 1.9 秒 + 语音合成 1.0 秒。

```yaml
interpreter:
  mode: fused
  fused:
    provider: gemini
    api_key: env:GOOGLE_API_KEY
    model: gemini-flash-lite-latest
```

**`mode: cascade`** — 语音识别 → 翻译 → 合成，三段各自换厂商。

```yaml
interpreter:
  mode: cascade
  stt: { provider: openai, base_url: https://api.openai.com, api_key: env:OPENAI_API_KEY, model: whisper-1 }
  mt:  { provider: gemini, api_key: env:GOOGLE_API_KEY, model: gemini-flash-lite-latest }
```

### 支持的 provider

| 名字 | 能做什么 | 说明 |
|---|---|---|
| `gemini` | stt / mt / fused | Google Generative Language API，语音理解快 |
| `openai` | stt / mt / tts / fused | 任何 OpenAI 兼容端点：OpenAI、new-api、one-api、SiliconFlow、DeepSeek、本地 vLLM… |
| `siliconflow` | 同上 | `openai` 的别名，换个 `base_url` 就行 |
| `edge` | tts | 微软 Edge 的公开接口，**免费、不用 key**，300+ 音色 |

`api_key: env:NAME` 表示从环境变量读。服务会自动加载本项目的 `.env`，也会读
`~/.hermes/.env`，所以已有的 key 可以直接引用，不用复制一份。

### 声音

```yaml
  out_voice: { provider: edge, voice: en-GB-RyanNeural }   # 说给对方的英语
  in_voice:  { provider: edge, voice: zh-CN-YunxiNeural }  # 念给你的中文（设 provider: "" 就只出文字）
```

英式男声：`en-GB-RyanNeural`（友好）、`en-GB-ThomasNeural`；
美音男声：`en-US-AndrewMultilingualNeural`。完整列表：`edge-tts --list-voices`。

### SIP 网关

```yaml
sip:
  ws_url: wss://100.85.36.110:8099/ws     # 网关的 WebRTC/WSS 端点
  uri: sip:webrtc@100.85.36.110:8099
  user: webrtc
  password: ""            # 留空则在页面上填，避免写进文件
  register: true
```

`password` 留空更安全 — 页面上的输入框优先。想知道网关的 WSS 地址：VoWiFi 网关
一般在「软电话 / SIP 信息」里给出每张卡的 WebRTC 端口。

---

## 怎么用

1. **开启麦克风** — 浏览器会要权限。
2. **连接网关** — 状态变「已注册」。
3. **拨号** — 填号码（`441733347007` 这种纯数字即可），点拨打。也可以接听：网关
   转来的来电会自动响铃到页面，接起后同理。
4. **说话** — 按住**空格**或「按住说话」按钮，说完松手。英文会自动进对方耳朵。
5. 对方说英语时，中文**自动**出现在「对话内容」里。

### 几个设计取舍

**按住说话是默认开着的。** 不开的话，房间里任何声音、对方通过话筒漏回来的声音
都会被翻译并且念给对方听 —— 一次实际测试里，笔记本麦克风的底噪被翻成了
"My head hurts and I feel dizzy." 这种凭空捏造的话，然后真的送进了通话。闸门
就是为这个存在的。取消勾选就是全程收音。

**你随时可以用真嗓子。** 没有翻译在播的时候，通话走的是你的原麦克风，所以
"Yes"、"OK"、念个数字、拼个名字都可以直接说。

**翻译一进来就抢音轨。** 检测到合成英语的瞬间，上行音轨换成合成音，说完自动换回。
对方**永远听不到你说的中文**。

**屏幕上有个「让你也听到」开关**（默认开）。打开时你也听得到合成的英语，方便
确认对方听到了什么。它只影响你本地，不进通话。

**备用：打字。** 没麦克风、或者在嘈杂环境里，直接在备用区打中文，点「送入通话」。
适合念账号、拼姓名这类必须准确的内容。

---

## 自检与排障

```bash
python3 check.py                       # 依赖 / 翻译 / 语音 / 网关 逐项检查
python3 tests/e2e_pipeline.py          # 真实 API 的双向翻译测试（会调 provider）
```

页面上「诊断」折叠区里有一行实时状态，出问题先看它：

| 现象 | 原因 |
|---|---|
| 上行一直是 0 | 闸门关着（按住说话没按住） |
| 注册=否 | `sip.*` 填错，或网关没开机、WSS 端口不对 |
| 英文出=0 | 翻译或 TTS 报错，看页面上方的红色提示 |
| 对方听不到 | 通话已建立但没换音轨；看「诊断」的「通话=是」 |
| 中文出=0 | 对方还没说话，或 `in_voice.provider` 设成了 `""` |

---

## 许可

MIT — 见 `LICENSE`。

作者：GongyiChuren
