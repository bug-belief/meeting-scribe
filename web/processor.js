// 采集 → PCM16/16k → 主线程。每 100 ms 交一包，服务端再按 600 ms 聚合。
// channels=1：普通单声道（麦克风 / 系统声音）
// channels=2：双路分轨，左=麦克风、右=系统声音，交给服务端按声道判人
// 采样率不强行要求，按实际输入重采样（iOS 常给 48k、系统声音常给 44.1/48k）。

class PCM16Worklet extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.target = opts.targetRate || 16000;
    this.packet = opts.packetSamples || 1600; // 每声道 100 ms @16k
    this.channels = Math.min(2, Math.max(1, opts.channels || 1));
    this.pos = new Float64Array(this.channels);
    this.sum = new Float64Array(this.channels);
    this.count = new Float64Array(this.channels);
    this.buf = new Int16Array(this.packet * this.channels);
    this.fill = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length || !input[0]) return true;
    const frames = input[0].length;
    const available = input.length;

    for (let i = 0; i < frames; i++) {
      let emitted = false;
      for (let c = 0; c < this.channels; c++) {
        const data = available > c ? input[c] : input[available - 1];
        const v = data ? data[i] : 0;
        this.sum[c] += v;
        this.count[c]++;
        this.pos[c] += this.target;
        if (this.pos[c] >= sampleRate) {
          this.pos[c] -= sampleRate;
          let s = this.count[c] ? this.sum[c] / this.count[c] : 0;
          this.sum[c] = 0;
          this.count[c] = 0;
          s = s < -1 ? -1 : s > 1 ? 1 : s;
          this.buf[this.fill * this.channels + c] = s < 0 ? s * 0x8000 : s * 0x7fff;
          emitted = true;
        }
      }
      if (emitted) {
        this.fill++;
        if (this.fill >= this.packet) this.emit();
      }
    }
    return true;
  }

  emit() {
    if (!this.fill) return;
    const out = this.buf.slice(0, this.fill * this.channels);
    this.buf = new Int16Array(this.packet * this.channels);
    this.fill = 0;
    this.port.postMessage(out.buffer, [out.buffer]);
  }
}

registerProcessor('pcm16-worklet', PCM16Worklet);
