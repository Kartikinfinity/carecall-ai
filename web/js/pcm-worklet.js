// Audio-thread processor: forwards raw microphone samples and a loudness level.
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      let sum = 0;
      for (let i = 0; i < channel.length; i++) sum += channel[i] * channel[i];
      this.port.postMessage({ samples: channel.slice(0), rms: Math.sqrt(sum / channel.length) });
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
