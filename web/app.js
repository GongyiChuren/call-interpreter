/* Call Interpreter — browser side.

   Two independent halves that share one audio graph:

     SIP half   JsSIP <-> WebRTC peer connection <-> VoWiFi gateway (MDD)
     AI half    microphone -> server -> translated speech -> call

   The important trick is the outgoing track swap. While no translation is
   playing, the call carries your real microphone (so you can still say "yes",
   "OK", a name, or read out a number — things a machine mangles). The moment
   translated audio arrives, the outgoing track is swapped to the worklet's
   output, so the other party hears only English. Swap back when it drains.
*/
(function () {
  'use strict';

  // ---------------------------------------------------------------- constants
  const CH_MIC = 0, CH_FAR = 1, CH_CALL = 2, CH_USER = 3;
  const $ = (id) => document.getElementById(id);
  // Carry the query string onto the WebSocket: the token lives there, and the
  // server rejects an unauthenticated socket with close code 4401.
  const WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://')
    + location.host + location.pathname + location.search;

  // ----------------------------------------------------------------- state
  const S = {
    cfg: null,
    ws: null,
    wsReady: false,
    ua: null,
    session: null,
    pc: null,
    registered: false,

    micCtx: null,      // AudioContext for capture (16 kHz target)
    micNode: null,
    micSrc: null,
    micStream: null,

    callCtx: null,     // AudioContext for call audio (WebRTC remote + playback)
    farNode: null,
    farSrc: null,
    playNode: null,
    playDest: null,    // MediaStreamAudioDestinationNode -> outgoing track
    playSrcNode: null, // source node to keep the play worklet pulled
    remoteStream: null,

    monitorSrc: null,  // local monitor so you hear your own translated English
    callActive: false,
    speaking: false,
    holding: false,

    // Speaker mode: no SIP anywhere. The translated English leaves through the
    // real speaker so the call that is already running — Google Voice in another
    // tab, a softphone, a mobile on the desk — picks it up with its own
    // microphone. The reverse direction rides on the same microphone: gate open
    // means you are talking, gate closed means we are listening to them.
    speakerMode: false,
    speakerReady: false,
    speakerGain: null,

    stats: { mic: 0, far: 0, en: 0, zh: 0, sent: 0, sentMic: 0, sentFar: 0,
             peak: 0, gated: 0 },
  };

  // ------------------------------------------------------------------- utils
  const say = (el, text, kind) => {
    const t = $(el);
    if (!t) return;
    t.textContent = text || '';
    t.hidden = !text;
    t.className = 'alert' + (kind === 'ok' ? ' ok' : kind === 'warn' ? ' warn' : '');
  };
  const dot = (id, cls) => { const d = $(id); if (d) d.className = 'dot ' + (cls || ''); };
  const fmt = (n) => n >= 1024 * 1024 ? (n / 1048576).toFixed(1) + 'M'
    : n >= 1024 ? (n / 1024).toFixed(0) + 'k' : String(n);

  function addCaption(kind, tag, src, dst) {
    const box = $('cap');
    const d = document.createElement('div');
    d.className = 'item ' + kind;
    if (src) { const s = document.createElement('div'); s.className = 'src'; s.textContent = src; d.appendChild(s); }
    const g = document.createElement('div'); g.className = 'tag'; g.textContent = tag; d.appendChild(g);
    if (dst) { const t = document.createElement('div'); t.className = 'dst'; t.textContent = dst; d.appendChild(t); }
    box.appendChild(d);
    box.scrollTop = box.scrollHeight;
    while (box.children.length > 60) box.removeChild(box.firstChild);
  }

  // ------------------------------------------------------------- translation link
  function connectWS() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(WS_URL);
      ws.binaryType = 'arraybuffer';
      const t = setTimeout(() => { try { ws.close(); } catch (e) {} reject(new Error('连接超时')); }, 12000);

      ws.onopen = () => { clearTimeout(t); S.wsReady = true; dot('dotAi', 'on'); resolve(ws); };
      ws.onerror = () => { clearTimeout(t); reject(new Error('无法连接翻译服务')); };
      ws.onclose = () => {
        S.wsReady = false; dot('dotAi', S.callActive ? 'bad' : '');
        if (S.callActive) say('alertAi', '翻译服务断开，正在重连…', 'warn');
        setTimeout(() => { if (!S.wsReady) connectWS().then(() => {
          S.ws = arguments.callee; say('alertAi', '', '');
        }).catch(() => {}); }, 2500);
      };
      ws.onmessage = onServerMessage;
      S.ws = ws;
    });
  }

  function onServerMessage(ev) {
    if (typeof ev.data === 'string') {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      switch (m.t) {
        case 'config':
          S.cfg = m.cfg;
          $('badgeMode').textContent = '模式 ' + (m.cfg.providers.mode);
          $('badgeVoice').textContent = m.cfg.outVoice.voice;
          break;
        case 'zh2en_text':
          addCaption('z', '你说（中文）→ 对方听到（英文）', m.src || '', m.text || '');
          break;
        case 'en2zh_text':
          addCaption('e', '对方说（英文）→ 你看到（中文）', m.src || '', m.text || '');
          break;
        case 'zh2en_audio':
          flash('已把英文送进通话 ' + fmt(m.n));
          break;
        case 'selftest_end':
          flash('自检完成');
          break;
        case 'error':
          say('alertAi', '翻译出错（' + m.where + '）：' + m.msg, 'warn');
          break;
        case 'stats': S.last = m; break;
      }
      return;
    }
    // binary: [channel][pcm16]
    const buf = new Uint8Array(ev.data);
    if (buf.length < 3) return;
    const ch = buf[0], pcm = buf.slice(1);
    if (ch === CH_CALL) { S.stats.en += pcm.length; pushPlayback(pcm, true); }
    else if (ch === CH_USER) { S.stats.zh += pcm.length; pushPlayback(pcm, false); }
    updateDiag();
  }

  function wsSend(obj) {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
  }
  function wsAudio(channel, arrayBuffer) {
    if (!S.ws || S.ws.readyState !== 1) return;
    const out = new Uint8Array(arrayBuffer.byteLength + 1);
    out[0] = channel;
    out.set(new Uint8Array(arrayBuffer), 1);
    S.ws.send(out.buffer);
    S.stats.sent += out.byteLength;
    // Counted per direction: the far-end leg is never gated, so a single total
    // cannot answer "is the talk gate actually closed?".
    if (channel === CH_MIC) S.stats.sentMic += out.byteLength;
    else if (channel === CH_FAR) S.stats.sentFar += out.byteLength;
  }

  // ------------------------------------------------------------------ audio in
  async function startCapture() {
    if (S.micNode) return;
    S.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true,
               channelCount: 1 },
    });
    S.micCtx = new (window.AudioContext || window.webkitAudioContext)();
    await CIWorklet.install(S.micCtx);
    S.micSrc = S.micCtx.createMediaStreamSource(S.micStream);
    S.micNode = new AudioWorkletNode(S.micCtx, 'capture', CIWorklet.captureOptions(16000));
    S.micNode.port.onmessage = (e) => {
      const d = e.data;
      S.stats.mic += d.pcm.byteLength;
      S.stats.peak = d.peak;
      if (S.speakerMode) {
        // Dual duty on one microphone. Holding the button = you are talking
        // (Chinese, to be translated). Button released = listen: the phone /
        // other tab is talking into our speaker, and this mic hears it.
        // While our own English is still playing we stay deaf, or we would
        // transcribe ourselves and answer our own sentence.
        if (S.holding && !S.speakerReady) wsAudio(CH_MIC, d.pcm);
        else if (!S.holding && !S.speakerReady) wsAudio(CH_FAR, d.pcm);
        if (d.frames % 15 === 0) { setMeter('meterMe', d.peak); updateDiag(); }
        return;
      }
      // Push-to-talk: while the gate is closed we keep the track live (so the
      // call still carries your real voice for "yes"/"no") but we do NOT feed
      // the translator. Without this, room noise and the other party's own
      // voice bleeding into the mic get translated and spoken back at them.
      if (gateOpen()) wsAudio(CH_MIC, d.pcm);
      else S.stats.gated++;
      if (d.frames % 15 === 0) { setMeter('meterMe', d.peak); updateDiag(); }
    };
    S.micSrc.connect(S.micNode);
    // Worklet output must be pulled by the graph, but we do not want to hear it.
    const sink = S.micCtx.createGain(); sink.gain.value = 0;
    S.micNode.connect(sink).connect(S.micCtx.destination);
  }

  function stopCapture() {
    try { S.micStream && S.micStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { S.micSrc && S.micSrc.disconnect(); } catch (e) {}
    try { S.micNode && S.micNode.disconnect(); } catch (e) {}
    try { S.micCtx && S.micCtx.close(); } catch (e) {}
    S.micStream = S.micSrc = S.micNode = S.micCtx = null;
  }

  async function startFarCapture(stream) {
    S.remoteStream = stream;
    S.callCtx = new (window.AudioContext || window.webkitAudioContext)();
    await CIWorklet.install(S.callCtx);

    // --- far-end -> server (what the other party says) ---
    S.farSrc = S.callCtx.createMediaStreamSource(stream);
    S.farNode = new AudioWorkletNode(S.callCtx, 'capture', CIWorklet.captureOptions(16000));
    S.farNode.port.onmessage = (e) => {
      const d = e.data;
      S.stats.far += d.pcm.byteLength;
      wsAudio(CH_FAR, d.pcm);
      if (d.frames % 15 === 0) setMeter('meterOther', d.peak);
    };
    S.farSrc.connect(S.farNode);
    const sink = S.callCtx.createGain(); sink.gain.value = 0;
    S.farNode.connect(sink).connect(S.callCtx.destination);

    // --- server -> call (translated English) ---
    // TTS arrives at 24 kHz; the worklet resamples to the device rate, because
    // an AudioWorkletNode does not do it for you (dropping that made the far end
    // hear 1.84x-fast chipmunk English).
    S.playNode = new AudioWorkletNode(S.callCtx, 'play',
                                      CIWorklet.playOptions(CIWorklet.TTS_RATE));
    S.playDest = S.callCtx.createMediaStreamDestination();
    S.playNode.connect(S.playDest);
    // Keep the graph pulling even when nothing is queued.
    S.playSrcNode = S.callCtx.createConstantSource();
    S.playSrcNode.offset.value = 0;
    S.playSrcNode.connect(S.playNode);
    S.playSrcNode.start();

    // --- local monitor: let the user hear what the other party hears ---
    setMonitor($('monitor').checked);

    // --- local playback of the Chinese translation ---
    S.userSink = S.callCtx.createGain();
    S.userSink.gain.value = 1;
    S.userSink.connect(S.callCtx.destination);
  }

  function setMonitor(on) {
    if (!S.playNode || !S.callCtx) return;
    try { S.monitorSrc && S.monitorSrc.disconnect(); } catch (e) {}
    if (on) { S.monitorSrc = S.callCtx.createGain(); S.playNode.connect(S.monitorSrc).connect(S.callCtx.destination); }
    else if (S.userSink) { S.playNode.connect(S.userSink); }
  }

  /* ---------------------------------------------------------- speaker mode
   *
   * For calls the browser cannot dial itself. Google Voice has no SIP and no
   * API, and the MDD gateway only registers its own in-page softphone — so the
   * one route that always works is the oldest one: hold the phone (or the other
   * tab) next to the speaker and let its microphone hear us.
   *
   *   you speak Chinese -> server -> English -> this laptop's SPEAKER
   *   the call's microphone picks it up (acoustic, no software in between)
   *   the call's audio comes back through THIS microphone when the gate is shut
   *
   * Everything runs on one AudioContext: capture from the mic, play to the
   * device's real destination. No SIP registration, no peer connection.
   */
  async function startSpeakerMode() {
    if (S.speakerMode) return;
    await startCapture();                       // shared mic capture path
    if (!S.callCtx) {
      S.callCtx = new (window.AudioContext || window.webkitAudioContext)();
      await CIWorklet.install(S.callCtx);
    }
    // Playback worklet -> the actual speakers (not a MediaStream destination).
    S.playNode = new AudioWorkletNode(S.callCtx, 'play',
                                      CIWorklet.playOptions(CIWorklet.TTS_RATE));
    S.speakerGain = S.callCtx.createGain();
    S.speakerGain.gain.value = 1;
    S.playNode.connect(S.speakerGain).connect(S.callCtx.destination);
    S.playNode.port.onmessage = (e) => {
      // English finished: reopen the mic path so the reply can be transcribed.
      if (e.data === 'drained') {
        S.speakerReady = false;
        if (S.speakerMode) {
          $('whoHears').textContent = '✱ 你在说话（中文）';
          $('whoHears').className = 'badge';
        }
      }
    };
    S.speakerMode = true;
    $('panelCall').style.display = 'none';      // SIP panel is irrelevant here
    $('speakerPanel').style.display = '';
    $('callState').textContent = '外放模式';
    say('alertAi', '外放模式已开：把手机/另一个窗口的通话对着这台电脑的音箱。'
      + '按住说话时麦克风只收你；松手后麦克风自动去收对方的英文。', 'ok');
    updateDiag();
  }

  function stopSpeakerMode() {
    S.speakerMode = false;
    S.speakerReady = false;
    try { S.playNode && S.playNode.port.postMessage('stop'); } catch (e) {}
    try { S.playNode && S.playNode.disconnect(); } catch (e) {}
    try { S.speakerGain && S.speakerGain.disconnect(); } catch (e) {}
    S.playNode = null; S.speakerGain = null;
    try { S.callCtx && S.callCtx.close(); } catch (e) {}
    S.callCtx = null;
    stopCapture();
    $('panelCall').style.display = '';
    $('speakerPanel').style.display = 'none';
    $('callState').textContent = '空闲';
    say('alertAi', '', '');
    updateDiag();
  }

  function pushPlayback(pcmBytes, toCall) {
    if (!S.callCtx || !S.playNode) return;
    const view = new Int16Array(pcmBytes.buffer, pcmBytes.byteOffset,
                                Math.floor(pcmBytes.byteLength / 2));
    const f32 = new Float32Array(view.length);
    for (let i = 0; i < view.length; i++) f32[i] = view[i] / 32768;
    // In speaker mode there is no peer connection to swap a track on: the same
    // worklet feeds the speaker instead, and `drained` flips the gate back.
    if (toCall && S.speakerMode) {
      if (!S.speakerReady) { S.speakerReady = true; $('whoHears').textContent = '✱ 正在对麦克风播放英文…'; $('whoHears').className = 'badge ok'; }
      S.playNode.port.postMessage(f32.buffer, [f32.buffer]);
      return;
    }
    if (toCall && S.speaking !== true) { S.speaking = true; takeOverCall(); }
    S.playNode.port.postMessage(f32.buffer, [f32.buffer]);
  }

  /* Swap the outgoing RTP track to the translated-English feed. */
  function takeOverCall() {
    if (!S.session || !S.playDest || !S.pc) return;
    const track = S.playDest.stream.getAudioTracks()[0];
    if (!track) return;
    const sender = S.pc.getSenders().find((s) => s.track && s.track.kind === 'audio')
      || S.pc.getSenders().find((s) => s.kind === 'audio');
    if (!sender) return;
    try { sender.replaceTrack(track); } catch (e) { return; }
    $('whoHears').textContent = '对方正在听：英文合成声';
    $('whoHears').className = 'badge ok';
  }

  function releaseCall() {
    if (!S.session || !S.pc || !S.micStream) return;
    const track = S.micStream.getAudioTracks()[0];
    if (!track) return;
    const sender = S.pc.getSenders().find((s) => s.track && s.track.kind === 'audio')
      || S.pc.getSenders().find((s) => s.kind === 'audio');
    if (!sender) return;
    try { sender.replaceTrack(track); } catch (e) { return; }
    S.speaking = false;
    $('whoHears').textContent = '对方正在听：你的原声';
    $('whoHears').className = 'badge';
  }

  function setMeter(id, peak) {
    const el = $(id); if (!el) return;
    const pct = Math.min(100, Math.round(peak * 140));
    el.style.width = pct + '%';
  }

  /* Brief status line so the user can see the pipeline working without reading stats. */
  let flashTimer = null;
  function flash(msg) {
    const el = $('flash');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => el.classList.remove('show'), 2600);
  }

  // ------------------------------------------------------------------ talk gate
  /* Push-to-talk. `pttOn()` false means always-open (hands-free mode). */
  function pttOn() { const el = $('ptt'); return !el || el.checked; }
  function gateOpen() { return !pttOn() || S.holding; }

  function setHolding(on) {
    if (S.holding === on) return;
    S.holding = on;
    const b = $('btnTalk');
    if (b) {
      b.classList.toggle('active', on);
      b.textContent = on ? '正在说…（松开发送）' : '按住说话';
    }
    $('whoHears').textContent = on ? '正在收音…' : (S.callActive ? '对方正在听：你的原声' : '');
    if (on) { wsSend({ t: 'flush' }); flash('开始收音，说完松手'); }
    else { wsSend({ t: 'flush' }); }   // close the utterance at once
  }

  /* Speaker mode reuses the same button, but the gate means something else:
   * pressing it declares "this is me talking"; releasing it hands the
   * microphone over to the call's audio coming out of the speaker. */
  function bindSpeakerGate() {
    const b = $('btnTalkSpk');
    if (!b) return;
    ['mousedown', 'touchstart'].forEach((e) =>
      b.addEventListener(e, (ev) => {
        ev.preventDefault();
        S.holding = true;
        b.classList.add('active');
        b.textContent = '正在说中文…（松开发送）';
        $('whoHears').textContent = '✱ 你在说话（中文）';
        $('whoHears').className = 'badge';
        wsSend({ t: 'flush' });
      }, { passive: false }));
    ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach((e) =>
      b.addEventListener(e, () => {
        if (!S.holding) return;
        S.holding = false;
        b.classList.remove('active');
        b.textContent = '按住说话（说中文）';
        wsSend({ t: 'flush' });
        flash('松手了，现在听对方说');
      }));
    window.addEventListener('blur', () => {
      if (S.holding) { S.holding = false; b.classList.remove('active'); b.textContent = '按住说话（说中文）'; }
    });
  }

  function bindTalkGate() {
    const b = $('btnTalk');
    if (!b) return;
    ['mousedown', 'touchstart'].forEach((e) =>
      b.addEventListener(e, (ev) => { ev.preventDefault(); setHolding(true); }, { passive: false }));
    ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach((e) =>
      b.addEventListener(e, () => setHolding(false)));

    // Space bar, but never while typing into one of the text fields.
    document.addEventListener('keydown', (e) => {
      if (e.code !== 'Space' || e.repeat) return;
      const t = e.target, tag = (t && t.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      e.preventDefault();
      setHolding(true);
    });
    document.addEventListener('keyup', (e) => {
      if (e.code !== 'Space') return;
      const t = e.target, tag = (t && t.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      setHolding(false);
    });
    // Releasing outside the window would otherwise leave the gate stuck open.
    window.addEventListener('blur', () => setHolding(false));
  }

  function updateDiag() {
    const s = S.stats;
    $('diag').textContent =
      '麦克风帧 ' + fmt(s.mic) + ' · 对方音频 ' + fmt(s.far) + ' · 英文出 ' + fmt(s.en) +
      ' · 中文出 ' + fmt(s.zh) + '\n' +
      '上行 ' + fmt(s.sent) + ' · 峰值 ' + s.peak.toFixed(3) +
      ' · 已挡住 ' + s.gated + ' 帧' +
      ' · WS=' + (S.wsReady ? '开' : '关') +
      ' · 注册=' + (S.registered ? '是' : '否') +
      ' · 通话=' + (S.callActive ? '是' : '否') +
      ' · 模式=' + (S.speakerMode ? '外放' : '网关') +
      ' · 闸门=' + (gateOpen() ? '开' : '关');
  }

  // -------------------------------------------------------------------- SIP
  function buildUA() {
    const c = S.cfg.sip;
    if (!c.wsUrl || !c.uri) throw new Error('SIP 未配置（config.yaml 的 sip 段）');
    const socket = new JsSIP.WebSocketInterface(c.wsUrl);
    const ua = new JsSIP.UA({
      sockets: [socket],
      uri: c.uri,
      // The value typed into the page wins, so a password can be kept out of
      // config.yaml entirely and pasted in per session.
      password: $('sipPass').value || c.password || undefined,
      display_name: c.displayName || 'Interpreter',
      register: !!c.register,
      session_timers: false,
    });

    ua.on('registered', () => { S.registered = true; dot('dotSip', 'on');
      $('sipState').textContent = '已注册'; updateDiag(); });
    ua.on('unregistered', () => { S.registered = false; dot('dotSip', 'mid');
      $('sipState').textContent = '未注册'; updateDiag(); });
    ua.on('registrationFailed', (e) => { S.registered = false; dot('dotSip', 'bad');
      $('sipState').textContent = '注册失败 ' + (e.cause || '');
      say('alertSip', 'SIP 注册失败：' + (e.cause || '未知原因') +
        '（检查 config.yaml 的 sip.user / sip_password，或网关是否在线）', 'warn'); });

    ua.on('newRTCSession', (e) => {
      if (S.session) { try { e.session.terminate(); } catch (err) {} return; }
      setupSession(e.session, e.originator === 'remote');
    });

    ua.on('connected', () => dot('dotSip', 'on'));
    ua.on('disconnected', () => dot('dotSip', 'mid'));
    return ua;
  }

  /* Attach to the peer connection's audio as soon as it exists.
   *
   * The `peerconnection` event can fire before our listener is registered (it is
   * emitted synchronously inside session.connect(), which runs in the same tick
   * as our setup for outgoing calls). So: try immediately, then poll briefly,
   * and also keep the listener for the incoming case. Whichever fires first wins
   * — the guard makes it idempotent.
   */
  function attachPeer(session) {
    if (S.peerAttached) return;
    const pc = session.connection;
    if (!pc) return;
    S.peerAttached = true;
    S.pc = pc;

    pc.addEventListener('track', (ev) => {
      if (ev.track.kind !== 'audio') return;
      const stream = (ev.streams && ev.streams[0]) || new MediaStream([ev.track]);
      attachRemoteAudio(stream);
      startFarCapture(stream)
        .then(() => flash('已接入对方音频'))
        .catch((err) => say('alertAi', '对方音频分析未启动：' + err.message, 'warn'));
    });

    // Some browsers only surface the remote track after negotiation settles;
    // re-check on the connection state changes rather than polling forever.
    pc.addEventListener('connectionstatechange', () => {
      if (pc.connectionState === 'connected' && !S.remoteStream) {
        const streams = (pc.getReceivers() || [])
          .map((r) => r.track).filter((t) => t && t.kind === 'audio');
        if (streams.length) {
          const st = new MediaStream(streams);
          attachRemoteAudio(st);
          startFarCapture(st).catch(() => {});
        }
      }
    });
  }

  function watchPeer(session) {
    let tries = 0;
    const tick = () => {
      attachPeer(session);
      if (!S.peerAttached && tries++ < 60) setTimeout(tick, 100);
    };
    tick();
  }

  function setupSession(session, incoming) {
    S.session = session;
    S.peerAttached = false;
    watchPeer(session);

    session.on('accepted', () => { S.callActive = true; setCallUI(true); watchPeer(session); });
    session.on('confirmed', () => { S.callActive = true; setCallUI(true); watchPeer(session); });
    session.on('ended', endCall);
    session.on('failed', (e) => { say('alertSip', '通话失败：' + (e.cause || ''), 'warn'); endCall(); });

    if (incoming) {
      session.answer({ mediaConstraints: { audio: true, video: false },
                       pcConfig: { iceServers: [] } });
      watchPeer(session);
    }
  }

  function attachRemoteAudio(stream) {
    const a = $('remoteAudio');
    a.srcObject = stream;
    a.play().catch(() => {});
  }

  function setCallUI(on) {
    $('btnCall').disabled = on;
    $('btnHangup').disabled = !on;
    $('callState').textContent = on ? '通话中' : '空闲';
    $('panelCall').style.display = '';
    $('whoHears').textContent = on ? '对方正在听：你的原声' : '';
    if (!on) { $('whoHears').className = 'badge'; }
  }

  function endCall() {
    S.callActive = false;
    S.session = null;
    S.speaking = false;
    S.peerAttached = false;
    S.remoteStream = null;
    setCallUI(false);
    try { S.playNode && S.playNode.port.postMessage('stop'); } catch (e) {}
    setTimeout(() => { stopCapture(); }, 200);
  }

  // ------------------------------------------------------------------- wiring
  async function boot() {
    try {
      await connectWS();
      say('alertAi', '', '');
    } catch (e) {
      say('alertAi', '翻译服务未连接：' + e.message, 'warn');
    }

    $('btnStart').onclick = async () => {
      $('btnStart').disabled = true;
      try {
        await startCapture();
        say('alertAi', '麦克风已就绪，可以拨号或接听。', 'ok');
        $('micState').textContent = '就绪';
        dot('dotMic', 'on');
      } catch (e) {
        say('alertAi', '拿不到麦克风：' + e.message, 'warn');
        $('btnStart').disabled = false;
      }
    };

    $('btnSip').onclick = () => {
      try {
        if (S.ua) { S.ua.stop(); S.ua = null; }
        S.ua = buildUA();
        S.ua.start();
        dot('dotSip', 'mid');
        $('sipState').textContent = '连接中…';
        say('alertSip', '', '');
      } catch (e) { say('alertSip', e.message, 'warn'); }
    };

    $('btnCall').onclick = async () => {
      const target = $('target').value.trim();
      if (!target) { say('alertSip', '填一个号码再拨。', 'warn'); return; }
      if (!S.ua) { say('alertSip', '先点「连接网关」。', 'warn'); return; }
      if (!S.micStream) { try { await startCapture(); } catch (e) {} }
      try {
        S.ua.call('sip:' + target.replace(/^sip:/, '') + '@' + hostOf(S.cfg.sip), {
          mediaConstraints: { audio: true, video: false },
          pcConfig: { iceServers: [] },
        });
        setCallUI(true);
      } catch (e) { say('alertSip', '拨号失败：' + e.message, 'warn'); }
    };

    $('btnHangup').onclick = () => { try { S.session && S.session.terminate(); } catch (e) {} endCall(); };

    $('monitor').onchange = (e) => setMonitor(e.target.checked);
    bindTalkGate();
    bindSpeakerGate();

    $('btnSpk').onclick = async () => {
      $('btnSpk').disabled = true;
      try {
        if (S.speakerMode) { stopSpeakerMode(); $('btnSpk').textContent = '开启外放模式'; }
        else { await startSpeakerMode(); $('btnSpk').textContent = '关闭外放模式'; }
      } catch (e) {
        say('alertAi', '外放模式启动失败：' + e.message, 'warn');
      }
      $('btnSpk').disabled = false;
    };

    $('spkVol').oninput = (e) => {
      if (S.speakerGain) S.speakerGain.gain.value = parseFloat(e.target.value);
      $('spkVolVal').textContent = Math.round(parseFloat(e.target.value) * 100) + '%';
    };

    $('btnSay').onclick = () => {
      const text = $('typed').value.trim();
      if (!text) return;
      wsSend({ t: 'say', text, direction: $('typedDir').value });
      $('typed').value = '';
    };

    $('btnTest').onclick = () => {
      const text = $('testEn').value.trim();
      // Renders an English voice server-side, then pushes it back through the
      // en->zh path exactly as a live call would — no dialling required.
      wsSend({ t: 'selftest', text });
      addCaption('e', '自检 · 模拟对方说话', text || '（默认银行开场白）', '正在合成并翻译…');
    };

    setInterval(() => { wsSend({ t: 'stats' }); updateDiag(); }, 2000);
    updateDiag();
  }

  function hostOf(sip) {
    const u = (sip && (sip.uri || '')) || '';
    const m = u.match(/@([^;>]+)/);
    return m ? m[1] : (sip.wsUrl || '').replace(/^wss?:\/\//, '').replace(/\/.*$/, '');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  // Exposed for automated testing and for reading live state in the console.
  window.CIState = S;
})();
