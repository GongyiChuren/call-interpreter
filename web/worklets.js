/* AudioWorklet processors, created from an inline source string so the page stays
   a two-file app and a worklet can never be served stale from a cache.

   Three jobs:
     capture  mic / far-end -> 16 kHz PCM16 -> main thread -> server
     play     server PCM16 24 kHz -> Float32 -> an outgoing MediaStream track

   `play` is what makes the other party hear English: its output feeds a
   MediaStreamAudioDestinationNode whose track replaces the microphone track on
   the live peer connection. Nothing but translated speech reaches the call.

   Loaded as a classic script (no ES module) so it also works when opened from
   file:// during local testing. Exposes window.CIWorklet.
*/
(function () {
  'use strict';

  const WORKLET_SRC = `
// ---- shared downsample helper (linear-decimated, adequate for speech) ----
function makeResampler(inRate, outRate) {
  const ratio = inRate / outRate;
  let acc = 0, accN = 0, phase = 0;
  return function push(input) {
    const out = [];
    for (let i = 0; i < input.length; i++) {
      acc += input[i]; accN++;
      phase += 1;
      if (phase >= ratio) {
        out.push(acc / accN);
        acc = 0; accN = 0; phase -= ratio;
      }
    }
    return out;
  };
}

function toInt16(f32) {
  const buf = new ArrayBuffer(f32.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < f32.length; i++) {
    let s = Math.max(-1, Math.min(1, f32[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buf;
}

// ---- capture: mic or far-end ----
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.down = makeResampler(sampleRate, o.targetRate || 16000);
    this.step = Math.round((o.targetRate || 16000) * 0.02);   // 20 ms frames
    this.pending = [];
    this.meter = 0;
    this.frames = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !ch.length) return true;
    const ds = this.down(ch);
    for (let i = 0; i < ds.length; i++) this.pending.push(ds[i]);

    let peak = 0;
    for (let i = 0; i < ch.length; i++) {
      const v = ch[i] < 0 ? -ch[i] : ch[i];
      if (v > peak) peak = v;
    }
    if (peak > this.meter) this.meter = peak;

    while (this.pending.length >= this.step) {
      const chunk = this.pending.splice(0, this.step);
      const buf = toInt16(Float32Array.from(chunk));
      this.port.postMessage({ pcm: buf, peak: this.meter, frames: ++this.frames }, [buf]);
    }
    this.meter *= 0.92;
    return true;
  }
}
registerProcessor('capture', CaptureProcessor);

// ---- playback ----
class PlayProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.wasPlaying = false;
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d === 'stop') { this.queue = []; this.wasPlaying = false; return; }
      this.queue.push(new Float32Array(d));
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    let need = out.length, i = 0;
    while (need > 0 && this.queue.length) {
      const head = this.queue[0];
      const take = need < head.length ? need : head.length;
      out.set(head.subarray(0, take), i);
      i += take; need -= take;
      if (take === head.length) this.queue.shift();
      else this.queue[0] = head.subarray(take);
    }
    for (; i < out.length; i++) out[i] = 0;   // underrun -> silence, never loop
    if (this.queue.length) this.wasPlaying = true;
    else if (this.wasPlaying) { this.wasPlaying = false; this.port.postMessage('drained'); }
    return true;
  }
}
registerProcessor('play', PlayProcessor);
`;

  const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));

  window.CIWorklet = {
    url,
    async install(ctx) { await ctx.audioWorklet.addModule(url); },
    captureOptions(targetRate) { return { processorOptions: { targetRate } }; },
  };
})();
