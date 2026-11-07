/* Continuous mono microphone -> 8 kHz G.711 mu-law, in 20 ms packets. */
'use strict';
function encodeMuLaw(value) {
  let sample=Math.max(-32635,Math.min(32635,Math.round(value*32768)));
  const sign=sample<0?128:0;sample=Math.abs(sample)+132;
  let exponent=7;
  for(let mask=16384;exponent>0 && !(sample&mask);exponent--,mask>>=1) {}
  return (~(sign | (exponent<<4) | ((sample>>(exponent+3))&15)))&255;
}
class BrowserCallMicrophone extends AudioWorkletProcessor {
  constructor() {
    super();this.phase=0;this.sum=0;this.count=0;this.packet=new Uint8Array(160);this.index=0;
  }
  process(inputs) {
    const input=inputs[0]?.[0];
    if(!input) return true;
    // Integrate each input interval instead of decimating at the browser's rate.
    for(const value of input) {
      this.sum+=value;this.count++;this.phase+=8000;
      if(this.phase>=sampleRate) {
        this.phase-=sampleRate;this.packet[this.index++]=encodeMuLaw(this.sum/this.count);this.sum=0;this.count=0;
        if(this.index===160) {
          this.port.postMessage(this.packet,[this.packet.buffer]);this.packet=new Uint8Array(160);this.index=0;
        }
      }
    }
    return true;
  }
}
registerProcessor('browser-call-microphone',BrowserCallMicrophone);
