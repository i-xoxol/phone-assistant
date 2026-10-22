"""Conservative spoken evidence matching; uncertainty never authorizes disclosure or a write."""
import re,unicodedata
from zoneinfo import ZoneInfo


def words(text):
    text=unicodedata.normalize('NFKC',text).casefold().replace('’',"'")
    return tuple(re.findall(r"[^\W_]+(?:['-][^\W_]+)*",text))


def contains_words(text,value):
    tokens,part=words(text),words(value)
    return bool(part) and any(tokens[i:i+len(part)]==part for i in range(len(tokens)-len(part)+1))


def complete_readback(text,value,*,include_identity=True):
    start=value.interval.start.astimezone(ZoneInfo(value.interval.zone))
    if start.second or start.microsecond:return False
    # Keep numeric boundaries: day 26 is not the year 2026, nor 10 AM part of 11:10 AM.
    said=text.casefold()
    numbers={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,
        'nine':9,'ten':10,'eleven':11,'twelve':12,'fifteen':15,'thirty':30,'forty-five':45,'sixty':60}
    for word,number in numbers.items():said=re.sub(r'\b'+word+r'\b',str(number),said)
    if re.search(r"\b(?:not|instead|actually|rather|correction|isn't|aren't|wasn't|won't|don't|cannot|can't)\b",said):return False
    months='january february march april may june july august september october november december'.split()
    month_pattern=r'\b('+'|'.join(months)+r')\b'
    dates=re.findall(month_pattern+r'\s+(\d{1,2})(?:st|nd|rd|th)?[ ,]+(\d{4})\b',said)
    expected=(start.strftime('%B').casefold(),start.day,start.year)
    if not dates or any((m,int(d),int(y))!=expected for m,d,y in dates):return False
    if len(re.findall(month_pattern,said))!=len(dates):return False
    times=list(re.finditer(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b\.?',said))
    if not times:return False
    for match in times:
        hour,minute,period=match.groups();hour,minute=int(hour),int(minute or 0)
        if not 1<=hour<=12 or minute>59:return False
        if (hour%12+(12 if period=='p' else 0),minute)!=(start.hour,start.minute):return False
    # An additional unqualified clock time is ambiguous, even beside a valid one.
    if any(not any(t.start()<=m.start()<t.end() for t in times)
            for m in re.finditer(r'\b\d{1,2}:\d{2}\b',said)):return False
    durations=re.findall(r'\b(\d+)\s*(?:minutes?|mins?)\b',said)
    seconds=(value.interval.end-value.interval.start).total_seconds()
    if seconds%60 or not durations or any(int(n)!=seconds/60 for n in durations):return False
    zones=(value.interval.zone,start.strftime('%Z'),'Eastern' if value.interval.zone=='America/New_York' else value.interval.zone)
    if not any(contains_words(text,z) for z in zones):return False
    # Reject a contradictory explicit meridian/zone as well as requiring the right one.
    known_zones=re.findall(r'\b(?:EST|EDT|CST|CDT|MST|MDT|PST|PDT|UTC|GMT|Eastern|Central|Mountain|Pacific)\b',text,re.I)
    if any(z.casefold() not in {v.casefold() for v in zones} for z in known_zones):return False
    if any(('EST' if z.casefold()=='standard' else 'EDT')!=start.strftime('%Z')
            for z in re.findall(r'\bEastern\s+(Standard|Daylight)\b',text,re.I)):return False
    parts=[value.location]
    if include_identity:parts.extend([value.first_name+' '+value.last_name,value.purpose])
    return all(contains_words(text,part) for part in parts)


def utterances(turns):
    """Reassemble adjacent transcript deltas, preserving speaker, pauses and sentence boundaries."""
    result=[]
    for turn in turns:
        start=turn.get('timestamp',turn.get('start_ms',0)/1000)
        end=turn.get('end_timestamp',turn.get('end_ms',start*1000)/1000)
        if (result and result[-1]['speaker']==turn['speaker'] and start<=result[-1]['end']+.75
                and not re.search(r'[.!?]\s*$',result[-1]['text'])):
            result[-1]['text']+=turn['text'];result[-1]['end']=max(end,result[-1]['end'])
        else:result.append({'speaker':turn['speaker'],'text':turn['text'],'end':end})
    return result


def caller_identified(turns,first,last):
    """Only the current full claim or two explicitly elicited components may identify the caller."""
    expected_first,expected_last=words(first),words(last)
    full=expected_first+expected_last;given=surname=False;question=None
    fillers=r'(?:(?:hello|hi|yes|actually|sorry|no|correction)[,\s]+)*'
    intro=fillers+r"(?:my name(?: is|'s)|this is|i am|i'm|it's|it is|call me|i go by)\b\s*(.*)"
    component=fillers+r"(?:my|the) (first name|given name|last name|surname|family name)(?: is|'s)\b\s*(.*)"
    for turn in utterances(turns):
        text=turn['text'].casefold().replace('’',"'").strip()
        if turn['speaker']=='assistant':
            if re.search(r'full name|first and last name|your name|who am i speaking',text):question='full';given=surname=False
            elif re.search(r'last name|surname|family name',text):question='last';surname=False
            elif re.search(r'first name|given name',text):question='first';given=surname=False
            else:question=None
            continue
        if turn['speaker']!='callee':continue
        claim=re.fullmatch(intro,text)
        part=re.fullmatch(component,text)
        if part:
            field,answer=part.groups();field='last' if field in ('last name','surname','family name') else 'first'
        elif claim:field,answer='full',claim[1]
        elif question:field,answer=question,re.sub(fillers+r"(?:it's|it is)\s+",'',text)
        elif re.search(r"\b(?:not|isn't)\b",text) and (contains_words(text,first) or contains_words(text,last) or 'name' in text):
            given=surname=False;question=None;continue
        elif re.match(r'(?:actually|no|correction)\b',text):
            given=surname=False;question=None;continue
        else:continue
        answer=words(answer)
        if field=='full':given=answer in (full,expected_first);surname=answer==full
        elif field=='first':given=answer==expected_first;surname=False
        else:surname=answer==expected_last
        question=None
    return bool(given and surname)
