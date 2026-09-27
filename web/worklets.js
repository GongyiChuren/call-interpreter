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
// TTS audio arrives at TTS_RATE (24 kHz). The graph runs at sampleRate (usually
// 44.1 or 48 kHz), and AudioWorkletNode does NOT resample: pushing 24 k samples
// into a 44.1 kHz stream plays them in 0.54 s, i.e. 1.84x too fast and a
// semitone-and-a-half sharp. Every playback therefore goes through a fractional
// resampler here, so "fast" is impossible by construction rather than by luck.
//
// Linear interpolation is plenty for speech, and unlike a fixed 24->48 doubling
// it stays correct on a 44.1 kHz device.
const TTS_RATE = 24000;
class PlayProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    // 1 for the browser (same rate), TTS_RATE/deviceRate for the speaker path.
    this.inRate = o.inRate || sampleRate;
    this.ratio = this.inRate / sampleRate;   // input samples consumed per output
    // 0 = passthrough, 1 = resample. Kept explicit so a same-rate device costs
    // nothing and the fast path stays honest.
    this.resample = Math.abs(this.ratio - 1) > 1e-6;
    this.pos = 0;            // fractional read cursor into the current block
    this.cur = null;         // block being consumed
    this.next = null;        // look-ahead block, needed for interpolation
    this.queue = [];
    this.wasPlaying = false;
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d === 'stop') {
        this.queue = []; this.cur = null; this.next = null;
        this.pos = 0; this.wasPlaying = false; return;
      }
      this.queue.push(new Float32Array(d));
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    out.fill(0);
    if (!this.resample) return this.copy(out);
    return this.resampleTo(out);
  }

  /* Same-rate path: straight copy, no cursor bookkeeping. */
  copy(out) {
    let need = out.length, i = 0;
    while (need > 0 && this.queue.length) {
      const head = this.queue[0];
      const take = need < head.length ? need : head.length;
      out.set(head.subarray(0, take), i);
      i += take; need -= take;
      if (take === head.length) this.queue.shift();
      else this.queue[0] = head.subarray(take);
    }
    this.markDrained(i);
    return true;
  }

  /* Fractional-rate path: linear interpolation between neighbouring samples. */
  resampleTo(out) {
    let i = 0;
    while (i < out.length) {
      if (!this.cur) {
        this.cur = this.queue.shift() || null;
        this.pos = 0;
        if (!this.cur) break;
      }
      if (!this.next && this.queue.length) this.next = this.queue[0];

      const p = Math.floor(this.pos);
      if (this.next && p + 1 >= this.cur.length) {
        // Straddles a block boundary: finish this block, interpolate with the
        // first sample of the next one, then roll forward.
        const a = this.cur[this.cur.length - 1];
        const b = this.next[0];
        out[i++] = a + (b - a) * (this.pos - Math.floor(this.pos));
        const overshoot = this.pos - (this.cur.length - 1);
        this.cur = this.next; this.next = null;
        this.pos = overshoot;
        continue;
      }
      if (p >= this.cur.length) { this.cur = null; continue; }

      const a = this.cur[p];
      const b = p + 1 < this.cur.length ? this.cur[p + 1]
              : (this.next ? this.next[0] : a);
      out[i++] = a + (b - a) * (this.pos - p);
      this.pos += this.ratio;
    }
    this.markDrained(i);
    return true;
  }

  markDrained(written) {
    if (this.queue.length || this.cur) this.wasPlaying = true;
    else if (this.wasPlaying) { this.wasPlaying = false; this.port.postMessage('drained'); }
    // Silence any tail we could not fill (underrun): never loop, never hold.
    void written;
  }
}
registerProcessor('play', PlayProcessor);
`;

  const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));

  window.CIWorklet = {
    url,
    TTS_RATE: 24000,     // what server/providers.py renders every TTS clip at
    async install(ctx) { await ctx.audioWorklet.addModule(url); },
    captureOptions(targetRate) { return { processorOptions: { targetRate } }; },
    // Playback must be told the rate of the samples being pushed in; the
    // worklet resamples to the device rate from there.
    playOptions(inRate) {
      return { numberOfOutputs: 1, outputChannelCount: [1],
               processorOptions: { inRate: inRate || 24000 } };
    },
  };
})();
