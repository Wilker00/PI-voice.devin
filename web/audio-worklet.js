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
