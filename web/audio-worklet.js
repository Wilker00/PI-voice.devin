// AudioWorkletProcessor: accumulate Float32 mic samples, convert to
// Int16 PCM, post ~85 ms chunks (4096 samples @48k) to the main thread.
class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunk = 4096;
    this._acc = new Float32Array(this._chunk);
    this._pos = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) {
      let i = 0;
      while (i < ch.length) {
        const n = Math.min(ch.length - i, this._chunk - this._pos);
        this._acc.set(ch.subarray(i, i + n), this._pos);
        this._pos += n;
        i += n;
        if (this._pos === this._chunk) {
          const pcm = new Int16Array(this._chunk);
          for (let j = 0; j < this._chunk; j++) {
            const s = Math.max(-1, Math.min(1, this._acc[j]));
            pcm[j] = s < 0 ? s * 32768 : s * 32767;
          }
          this.port.postMessage(pcm.buffer, [pcm.buffer]);
          this._pos = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('pcm-processor', PCMProcessor);

// Playback of converted audio from the Pi: a jitter buffer + smooth gate.
// Frames arrive every ~85 ms over Wi-Fi with jitter; scheduling each one
// as its own buffer leaves a gap whenever one is late. Here they queue in
// a ring buffer that starts playing once the cushion is buffered, so a
// late frame drains the cushion instead of going silent. The cushion
// adapts: when playback runs dry and audio resumes within 350 ms (a late
// frame), it grows (up to 300 ms); longer pauses are the server going
// quiet on purpose (enrolling, switching target) and don't count. After
// 10 s without a gap it shrinks back toward 120 ms. Running dry fades the
// last sample out; a backlog well past the cushion is trimmed so latency
// can't creep up.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    const sr = sampleRate;
    this._cap = Math.round(sr * 2);
    this._ring = new Float32Array(this._cap);
    this._r = 0;
    this._w = 0;
    this._count = 0;
    this._minTarget = Math.round(sr * 0.12); // jitter cushion (adaptive)
    this._maxTarget = Math.round(sr * 0.3);
    this._target = this._minTarget;
    this._step = Math.round(sr * 0.04);
    this._calm = 0;                          // samples since the last gap
    this._clock = 0;                         // samples rendered
    this._dryAt = -1;                        // when playback last ran dry
    this._lateGap = Math.round(sr * 0.35);   // shorter = jitter, longer = pause
    this._pauses = 0;
    this._playing = false;
    this._fade = 0;                         // 0..1 ramp around gaps
    this._fadeStep = 1 / (sr * 0.005);      // 5 ms
    this._last = 0;                         // decays out on underrun
    this._decay = Math.exp(-1 / (sr * 0.002));
    // Gate: per-sample RMS envelope (~10 ms smoothing), hold, slow
    // release — keeps soft syllables and word tails instead of muting
    // 85 ms blocks. RMS rather than peak: white room noise has peaks
    // far above its RMS, and a peak envelope would rest above the
    // threshold on nothing but the noise floor.
    this._env2 = 0;                         // smoothed power ~ RMS^2
    this._envK = 1 - Math.exp(-1 / (sr * 0.01));
    this._gateThr2 = 0.0045 * 0.0045;       // ~-48 dBFS RMS, squared
    this._holdLen = Math.round(sr * 0.12);
    this._hold = 0;
    this._g = 0;
    this._gAtk = 1 - Math.exp(-1 / (sr * 0.003));
    this._gRel = 1 - Math.exp(-1 / (sr * 0.12));
    this._underruns = 0;
    this._trimmed = 0;
    this._report = 0;
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === 'reset') this._reset();
      else this._push(e.data);
    };
  }

  _reset() {
    this._r = this._w = this._count = 0;
    this._playing = false;
    this._fade = 0;
    this._dryAt = -1;
    this._env2 = this._hold = this._g = this._last = 0;
  }

  _push(f32) {
    for (let i = 0; i < f32.length; i++) {
      this._ring[this._w] = f32[i];
      this._w = (this._w + 1) % this._cap;
      if (this._count < this._cap) this._count++;
      else this._r = (this._r + 1) % this._cap;   // full: drop oldest
    }
    if (this._count > this._target + Math.round(sampleRate * 0.15)) {
      const drop = this._count - this._target;
      this._r = (this._r + drop) % this._cap;
      this._count -= drop;
      this._trimmed++;
      this._fade = 0;                        // fade back in over the jump
    }
  }

  process(inputs, outputs) {
    const out = outputs[0][0];
    if (!this._playing && this._count >= this._target) {
      this._playing = true;
      if (this._dryAt >= 0) {                // resuming after running dry
        if (this._clock - this._dryAt < this._lateGap) {
          this._underruns++;                 // a late frame: widen cushion
          this._target = Math.min(this._maxTarget, this._target + this._step);
          this._calm = 0;
        } else {
          this._pauses++;                    // deliberate pause: ignore
        }
        this._dryAt = -1;
      }
    }
    for (let i = 0; i < out.length; i++) {
      let s;
      if (this._playing && this._count > 0) {
        s = this._ring[this._r];
        this._r = (this._r + 1) % this._cap;
        this._count--;
        this._fade = Math.min(1, this._fade + this._fadeStep);
        s *= this._fade;
        this._last = s;
      } else {
        if (this._playing) {                 // ran dry mid-stream
          this._playing = false;
          this._dryAt = this._clock + i;
        }
        this._fade = 0;
        this._last *= this._decay;           // no click: tail off
        s = this._last;
      }
      this._env2 += (s * s - this._env2) * this._envK;
      if (this._env2 > this._gateThr2) this._hold = this._holdLen;
      else if (this._hold > 0) this._hold--;
      const tgt = this._hold > 0 ? 1 : 0;
      this._g += (tgt - this._g) * (tgt > this._g ? this._gAtk : this._gRel);
      out[i] = s * this._g;
    }
    this._clock += out.length;
    this._calm += out.length;
    if (this._calm >= sampleRate * 10 && this._target > this._minTarget) {
      this._target = Math.max(this._minTarget, this._target - this._step / 4);
      this._calm = 0;
    }
    this._report += out.length;
    if (this._report >= sampleRate) {        // ~1/s stats for the HUD
      this._report = 0;
      this.port.postMessage({
        bufferedMs: Math.round(this._count / sampleRate * 1000),
        cushionMs: Math.round(this._target / sampleRate * 1000),
        underruns: this._underruns,
        pauses: this._pauses,
        trimmed: this._trimmed,
      });
    }
    return true;
  }
}

registerProcessor('playback-processor', PlaybackProcessor);
