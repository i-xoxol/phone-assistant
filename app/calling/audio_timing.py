"""Translate provider sample clocks to Twilio's clock without inventing caption coverage."""
import math
from collections import deque
from decimal import Decimal,InvalidOperation


def duration_ms(value):
    if not isinstance(value,str) or not value.endswith('s'):return None
    try:
        result=float(Decimal(value[:-1])*1000)
        return result if math.isfinite(result) and result>=0 else None
    except (InvalidOperation,ValueError):return None


class InputAudioTimeline:
    def __init__(self):
        self.audio_ms=0.0
        self.stream_end=0.0
        self.segments=deque(maxlen=1024)
        self.utterances=deque(maxlen=128)
        self.bound=None
        self.native_scale=None

    def observe(self,start_ms,size,spoken=False):
        end=self.audio_ms+size/8
        start=float(start_ms)
        # Coalesce contiguous frames; gaps/reconnects do not stretch provider time.
        offset=start-self.audio_ms
        if self.segments and abs(self.segments[-1][2]-offset)<.01:
            old=self.segments.pop();self.segments.append((old[0],end,offset))
        else:self.segments.append((self.audio_ms,end,offset))
        self.audio_ms=end;self.stream_end=start+size/8
        last=self.utterances[-1] if self.utterances else None
        if spoken:
            if not last or last['closed']:
                last={'start':end-size/8,'end':end,'closed':False,'transcribed':False,'claimed':False}
                self.utterances.append(last)
            last['end']=end
        elif last and not last['closed'] and end-last['end']>=500:last['closed']=True

    def stream_time(self,audio_ms):
        if audio_ms is None or not self.segments or audio_ms<0 or audio_ms>self.audio_ms+40:return None
        for start,end,offset in self.segments:
            if start<=audio_ms<=end+0.01:
                return max(0,round(audio_ms+offset))
        if self.audio_ms<=audio_ms<=self.audio_ms+40:return round(self.stream_end)
        return None

    def covered_time(self,audio_ms):
        # One 20 ms telephone frame of boundary quantization, never wall-clock time.
        if audio_ms is None or audio_ms<0 or audio_ms>self.audio_ms+40:return None
        return self.stream_time(min(self.audio_ms,math.ceil(audio_ms/20)*20))


    def native_span(self,start,end):
        """Validate a native timestamp unit against captured audio before using it.

        Realtime services may report their internal 16/24 kHz clock for an 8 kHz
        wire format. Calibrate only when one rate ratio matches an observed
        onset; retain normal millisecond timing when there is no raw speech.
        """
        pending=next((s for s in self.utterances if not s['claimed']),None)
        if start is None or end is None:return None,None
        scale=self.native_scale
        if pending and scale is None:
            # A caller can already be speaking on reconnect (onset near zero).
            # A closed endpoint then disambiguates otherwise identical onsets.
            options=sorted((abs(start*r-pending['start'])+
                (abs(end*r-pending['end'])*.25 if pending['closed'] else 0),r) for r in (1,2,3))
            if abs(start*options[0][1]-pending['start'])<=250 and (options[1][0]-options[0][0]>=100 or options[0][1]==1):
                scale=options[0][1]
            else:return None,None
        elif scale is None:scale=1
        mapped_start=self.stream_time(start*scale);mapped_end=self.covered_time(end*scale)
        if mapped_end is None:return mapped_start,None
        covered=[]
        for span in self.utterances:
            if start*scale-250<=span['start'] and span['closed'] and span['end']<=end*scale:
                span['claimed']=span['transcribed']=True
                covered.append(self.covered_time(span['end']))
        # Provider endpoint padding must not credit a newer, still-speaking caller.
        if self.utterances:mapped_end=max((v for v in covered if v is not None),default=None)
        if mapped_end is not None:self.native_scale=scale
        return mapped_start,mapped_end

    def transcription_span(self):
        """Bind committed timestamp-free ASR to the oldest unclaimed audio utterance.

        Never credit a newer utterance merely because a delayed caption arrived.
        Unknown timing remains pending; this fallback cannot use wall-clock time.
        """
        if self.bound is None:
            self.bound=next((span for span in self.utterances if not span['claimed']),None)
            if self.bound:self.bound['claimed']=True
        if not self.bound:return None,None
        self.bound['transcribed']=True
        self.bound['caption_end']=self.bound['end']
        return (self.stream_time(self.bound['start']),
                self.covered_time(self.bound['caption_end']) if self.bound['closed'] else None)

    def finish_transcription(self):
        self.bound=None

    def transcript_coverage(self):
        ends=[self.covered_time(span.get('caption_end',span['end'])) for span in self.utterances if span['transcribed'] and span['closed']]
        return max((end for end in ends if end is not None),default=None)


def current_caller_audio(manager,call_id,turns):
    activity=getattr(manager,'caller_activity',{}).get(call_id)
    if not activity:return True
    if activity.get('provider_timing_required'):
        end=activity.get('caption_covered_ms',-1)
    else:end=max((round(t['end_timestamp']*1000) for t in turns if t['speaker']=='callee'),default=-1)
    return end>=activity['last_speech_ms']
