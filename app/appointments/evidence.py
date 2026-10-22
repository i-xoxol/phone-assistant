"""Conservative spoken evidence matching; uncertainty never authorizes disclosure or a write."""
import re,unicodedata
from zoneinfo import ZoneInfo


def words(text):
    text=unicodedata.normalize('NFKC',text).casefold().replace('’',"'")
    return tuple(re.findall(r"[^\W_]+(?:['-][^\W_]+)*",text))


def contains_words(text,value):
    tokens,part=words(text),words(value)
    return bool(part) and any(tokens[i:i+len(part)]==part for i in range(len(tokens)-len(part)+1))


def affirmative_answer(text):
    text=' '.join(text.casefold().replace('’',"'").split()).strip(' .!?')
    return bool(re.fullmatch(r"yes(?: please)?|yes[,]? (?:that is|that's|it is|it's) (?:correct|right)|correct|that works|так|так[,]? правильно",text))


def speech_words(text):
    """Normalize supported transcription variants without dropping extra quantities or words."""
    text=unicodedata.normalize('NFKC',text).casefold().replace('’',"'")
    text=re.sub(r"\b([ap])\s*\.\s*m\.?",r" \1m ",text)
    numbers={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,
        'nine':9,'ten':10,'eleven':11,'twelve':12,'fifteen':15,'thirty':30,'forty-five':45,'sixty':60}
    for word,number in numbers.items():text=re.sub(r'\b'+word+r'\b',str(number),text)
    text=re.sub(r'\b(\d{1,2}):00(?=\s*[ap]m\b)',r'\1',text)
    text=re.sub(r'(?<=\d)(?=[ap]m\b)',' ',text)
    return tuple(str(int(w)) if w.isdecimal() else w for w in words(text))


def date_words(start):
    """Exact spoken date variants, confined to the date field (never other quantities)."""
    units=('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
        'fifteen sixteen seventeen eighteen nineteen').split()
    ordinals=('zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh '
        'twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth').split()
    def cardinal(n):
        return units[n] if n<20 else ('twenty' if n<30 else 'thirty')+(' '+units[n%10] if n%10 else '')
    day=start.day
    ordinal=ordinals[day] if day<20 else ('twentieth' if day==20 else 'thirtieth' if day==30
        else ('twenty' if day<30 else 'thirty')+' '+ordinals[day%10])
    suffix='th' if 10<day<14 else {1:'st',2:'nd',3:'rd'}.get(day%10,'th')
    days={str(day),str(day)+suffix,cardinal(day),ordinal,cardinal(day).replace(' ','-'),ordinal.replace(' ','-')}
    years={str(start.year)}
    # The service accepts ordinary near-term bookings. Unsupported centuries stay numeric.
    if 2000<=start.year<=2099:
        tail=start.year%100
        if tail==0:years.add('two thousand')
        elif tail<40:
            for last in {cardinal(tail),cardinal(tail).replace(' ','-')}:
                years.update({'two thousand '+last,'two thousand and '+last})
                if tail>=10:years.add('twenty '+last)
    return {speech_words(start.strftime('%B')+' '+d+' '+y) for d in days for y in years}


def complete_readback(text,value,*,include_identity=True,require_confirmation=False):
    """Require the complete queued readback, with bounded transcription variants.

    Unsupported paraphrases fail closed. Comparing the whole expression preserves every
    quantity and field boundary: a suffix of a different spoken time/duration is insufficient.
    """
    start=value.interval.start.astimezone(ZoneInfo(value.interval.zone))
    seconds=(value.interval.end-value.interval.start).total_seconds()
    if start.second or start.microsecond or seconds%60:return False
    zones=[value.interval.zone,start.strftime('%Z')]
    if value.interval.zone=='America/New_York':
        zones+=['Eastern','Eastern Time','Eastern '+('Daylight' if start.strftime('%Z')=='EDT' else 'Standard')+' Time']
    prefixes=[value.first_name+' '+value.last_name] if include_identity else [
        '', 'Confirmed', 'Calendar and private registry verified confirmed']
    if require_confirmation:prefixes=['Confirmed','Calendar and private registry verified confirmed']
    suffixes=['','Is that correct','Is that right','Is that okay','Does that work']
    actual=speech_words(text)
    tails=[]
    for prefix in prefixes:
        before=speech_words(prefix)
        if actual[:len(before)]!=before:continue
        remaining=actual[len(before):]
        for date in date_words(start):
            if remaining[:len(date)]==date:tails.append(remaining[len(date):])
    if not tails:return False
    for zone in zones:
        core=f'{start.strftime("at %I:%M %p")} {zone}, {int(seconds/60)} minutes, {value.purpose}, {value.location}'
        for suffix in suffixes:
            if speech_words(core+' '+suffix) in tails:return True
    return False


def utterances(turns,*,join_sentences=False):
    """Reassemble adjacent transcript deltas, preserving speaker, pauses and sentence boundaries."""
    result=[]
    for turn in turns:
        start=turn.get('timestamp',turn.get('start_ms',0)/1000)
        end=turn.get('end_timestamp',turn.get('end_ms',start*1000)/1000)
        if (result and result[-1]['speaker']==turn['speaker'] and start<=result[-1]['end']+.75
                and (join_sentences or not re.search(r'[.!?]\s*$',result[-1]['text']))):
            separator=' ' if re.search(r'[.!?]$',result[-1]['text']) and not turn['text'][:1].isspace() else ''
            result[-1]['text']+=separator+turn['text'];result[-1]['end']=max(end,result[-1]['end'])
        else:result.append({'speaker':turn['speaker'],'text':turn['text'],'end':end})
    return result


def caller_identified(turns,first,last):
    """Only the current full claim or two explicitly elicited components may identify the caller."""
    expected_first,expected_last=words(first),words(last)
    full=expected_first+expected_last;given=surname=False;question=None
    fillers=r'(?:(?:hello|hi|yes|actually|sorry|no|correction)[,\s]+)*'
    explicit=r"(?:my name(?: is|'s)|call me|i go by)"
    intro=fillers+r"(?:"+explicit+r"|this is|i am|i'm|it's|it is)\b\s*(.*)"
    component=fillers+r"(?:my|the) (first name|given name|last name|surname|family name)(?: is|'s)\b\s*(.*)"
    sentences=[]
    for turn in utterances(turns,join_sentences=True):
        if turn['speaker']!='callee':sentences.append(turn);continue
        # A period inside a complete supplied name (for example St. John) is not a sentence boundary.
        text=turn['text'].casefold().replace('’',"'");spans=[]
        for name in (first,last,first+' '+last):
            pattern=r'(?<!\w)'+r'[\W_]+'.join(re.escape(w) for w in words(name))+r'(?!\w)'
            if words(name):spans.extend((m.start(),m.end()) for m in re.finditer(pattern,text))
        offset=0
        for boundary in re.finditer(r'[.!?]+(?:\s+|$)',text):
            if any(a<=boundary.start()<b for a,b in spans):continue
            sentences.append({**turn,'text':text[offset:boundary.start()]});offset=boundary.end()
        if text[offset:].strip():sentences.append({**turn,'text':text[offset:]})
    for turn in sentences:
        text=turn['text'].casefold().replace('’',"'").strip()
        text=re.sub(r'\b(first|last|family|given)[- ]?name\b',r'\1 name',text)
        if turn['speaker']=='assistant':
            if re.search(r'\byour (?:full name|first and last name|name)\b|\bwho am i speaking (?:with|to)\b',text):question='full';given=surname=False
            elif re.search(r'\byour (?:last name|surname|family name)\b',text):question='last';surname=False
            elif re.search(r'\byour (?:first name|given name)\b',text):question='first';given=surname=False
            else:question=None
            continue
        if turn['speaker']!='callee':continue
        claim=re.fullmatch(intro,text)
        part=re.fullmatch(component,text)
        name_cue=explicit+r"|\b(?:this is|i am|i'm|it's|it is|first name|last name|surname|family name|name)\b"
        followup=r"(?:(?:i am|i'm) (?:calling|phoning|asking|inquiring|checking) (?:about|regarding|for|on|to confirm|to check|to discuss)|(?:it's|it is) (?:about|regarding)) (?:my|the|an) (?:(?:existing|upcoming|scheduled) )?appointment(?: please| thank you)?"
        ordinary=re.fullmatch(followup,' '.join(words(text)))
        if ordinary and question is None:continue
        if part:
            field,answer=part.groups();field='last' if field in ('last name','surname','family name') else 'first'
        elif claim:field,answer='full',claim[1]
        elif question:field,answer=question,re.sub(fillers+r"(?:it's|it is)\s+",'',text)
        elif re.search(r"\b(?:not|isn't)\b",text) and (contains_words(text,first) or contains_words(text,last) or 'name' in text):
            given=surname=False;question=None;continue
        elif re.match(r'(?:actually|no|correction)\b',text):
            given=surname=False;question=None;continue
        elif re.search(name_cue,text):
            given=surname=False;question=None;continue
        else:continue
        answer=words(answer)
        if field=='full':given=answer in (full,expected_first);surname=answer==full
        elif field=='first':given=answer==expected_first;surname=False
        else:surname=answer==expected_last
        question=None
    return bool(given and surname)
