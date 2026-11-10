// Microphone -> 16 kHz mono 16-bit WAV, entirely in the browser.
//
// Why not MediaRecorder: browsers record WebM/Ogg/MP4, not WAV. Why not
// AudioContext({sampleRate: 16000}): Firefox refuses to connect a mic stream
// to a context at a different rate. So we capture at the native rate and
// downsample here.

const TARGET_RATE = 16000;

export class MicUnavailableError extends Error {}

// End-of-speech detection: once the caller has spoken, this much quiet ends the turn.
const SILENCE_TO_STOP_S = 1.2;
const MIN_SPEECH_S = 0.25;
const NOISE_CALIBRATION_S = 0.3;

export class WavRecorder {
  constructor({ maxSeconds = 25, onLevel = () => {}, onAutoStop = () => {} } = {}) {
    this.maxSeconds = maxSeconds;
    this.onLevel = onLevel;
    this.onAutoStop = onAutoStop;     // called with "silence" or "max-length"
    this.recording = false;
  }

  // Simple energy VAD: learn the room's noise floor for 0.3 s, then call it speech when the
  // level is clearly above it. After enough speech, a run of quiet stops the recording.
  _trackSpeech(rms, seconds) {
    const v = this.vad;
    v.time += seconds;
    if (v.time < NOISE_CALIBRATION_S) {
      v.floor = Math.max(v.floor, rms);
      return;
    }
    const threshold = Math.max(0.012, v.floor * 2.5);
    if (rms > threshold) {
      v.speech += seconds;
      v.quiet = 0;
    } else if (v.speech >= MIN_SPEECH_S) {
      v.quiet += seconds;
      if (v.quiet >= SILENCE_TO_STOP_S && !v.fired) {
        v.fired = true;
        this.onAutoStop("silence");
      }
    }
  }

  async start() {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new MicUnavailableError(
        "The microphone needs a secure page (https:// or http://localhost). You can type instead.");
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      const msg = err.name === "NotAllowedError"
        ? "Microphone permission was denied. Allow it in the browser's address bar, or type instead."
        : err.name === "NotFoundError"
          ? "No microphone was found. Connect one, or type instead."
          : `The microphone could not start (${err.name}). You can type instead.`;
      throw new MicUnavailableError(msg);
    }
    this.ctx = new AudioContext();
    await this.ctx.resume();
    await this.ctx.audioWorklet.addModule("/js/pcm-worklet.js");
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "pcm-capture");
    this.chunks = [];
    this.vad = { time: 0, floor: 0, speech: 0, quiet: 0, fired: false };
    const rate = this.ctx.sampleRate;
    this.node.port.onmessage = (e) => {
      this.chunks.push(e.data.samples);
      this.onLevel(e.data.rms);
      this._trackSpeech(e.data.rms, e.data.samples.length / rate);
    };
    // A silent sink keeps the graph running in every browser without echoing the mic.
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0;
    this.source.connect(this.node).connect(this.sink).connect(this.ctx.destination);
    this.startedAt = performance.now();
    this.recording = true;
    this.timer = setTimeout(() => this.onAutoStop("max-length"), this.maxSeconds * 1000);
  }

  elapsed() {
    return this.recording ? (performance.now() - this.startedAt) / 1000 : 0;
  }

  async stop() {
    if (!this.recording) return null;
    const rate = this.ctx.sampleRate;
    this._teardown();
    const samples = merge(this.chunks);
    this.chunks = [];
    return encodeWav(downsample(samples, rate, TARGET_RATE), TARGET_RATE);
  }

  cancel() {
    if (this.recording) this._teardown();
    this.chunks = [];
  }

  _teardown() {
    clearTimeout(this.timer);
    this.recording = false;
    try { this.source.disconnect(); this.node.disconnect(); this.sink.disconnect(); } catch { /* already gone */ }
    this.stream.getTracks().forEach((t) => t.stop());
    this.ctx.close();
    this.onLevel(0);
  }
}

function merge(chunks) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const c of chunks) { out.set(c, offset); offset += c.length; }
  return out;
}

// Averaging each output bin acts as a simple low-pass filter, avoiding aliasing.
export function downsample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

export function encodeWav(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buffer);
  const text = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  text(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
  text(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  text(36, "data"); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}
