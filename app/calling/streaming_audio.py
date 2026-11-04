"""Stateful G.711 μ-law / little-endian PCM16 conversion, with bounded chunks."""
import numpy as np
import soxr


def decode_sample(code):
    value=(~code)&255
    magnitude=(((value&15)<<3)+132)<<((value>>4)&7)
    return 132-magnitude if value&128 else magnitude-132

DECODE=np.array([decode_sample(i) for i in range(256)],dtype=np.int16)


def encode_pcm(samples):
    values=(np.asarray(samples,dtype=np.int32)>>2)<<2
    sign=np.where(values<0,128,0)
    magnitude=np.minimum(np.abs(values),32635)+132
    exponent=np.zeros(values.shape,dtype=np.int32)
    for bit in range(1,8): exponent=np.where(magnitude>=(256<<(bit-1)),bit,exponent)
    mantissa=(magnitude>>(exponent+3))&15
    return (~(sign|(exponent<<4)|mantissa)&255).astype(np.uint8).tobytes()


class StreamingAudio:
    def __init__(self):
        self.input=soxr.ResampleStream(8000,16000,1,dtype='float64',quality='HQ')
        self.reset_output()

    def reset_output(self):
        self.output=soxr.ResampleStream(24000,8000,1,dtype='float64',quality='HQ')
        self.partial=b''

    def from_twilio(self, payload, *, last=False):
        if len(payload)>8000: raise ValueError('Input audio chunk exceeds one second')
        pcm=self.input.resample_chunk(DECODE[np.frombuffer(payload,dtype=np.uint8)].astype(np.float64),last=last)
        return np.rint(np.clip(pcm,-32768,32767)).astype('<i2').tobytes()

    def to_twilio(self, payload, *, last=False):
        if len(payload)>480000: raise ValueError('Output audio chunk exceeds ten seconds')
        data=self.partial+payload
        self.partial=data[-1:] if len(data)%2 else b''
        if last and self.partial: raise ValueError('Incomplete PCM16 sample')
        samples=np.frombuffer(data[:len(data)//2*2],dtype='<i2').astype(np.float64)
        pcm=self.output.resample_chunk(samples,last=last)
        return encode_pcm(np.rint(np.clip(pcm,-32768,32767)).astype(np.int16))
