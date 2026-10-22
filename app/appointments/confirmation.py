"""Brief server-issued confirmations, bound to the complete private appointment draft."""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo
from app.appointments.evidence import words,speech_words,date_words,caller_identified,utterances,affirmative_answer,READBACK_PREFIXES

SHORT='Read this appointment time exactly once, without a preface, then wait:\n'
COMPACT='Read these appointment details exactly once, without a preface, then wait:\n'


def eligible(value,now,zone,turns=()):
    seconds=(value.interval.end-value.interval.start).total_seconds()
    caller=' '.join(t['text'] for t in utterances(turns,join_sentences=True) if t['speaker']=='callee').casefold()
    foreign_zone=re.search(r'\b(?:pacific|central|mountain|utc|gmt|pst|pdt|cst|cdt|mst|mdt|bst|cet|cest|my time)\b'
        r'|\b(?:europe|asia|africa|australia|pacific)/|\bamerica/(?!new_york\b)\w+',caller)
    # One possible upcoming month/day, in the established office time zone.
    # Explicit other-zone claims require speaking the full date/zone to resolve intent.
    return (not foreign_zone and value.interval.zone==zone and timedelta(0)<value.interval.start-now<timedelta(days=31)
        and not (value.interval.start.second or value.interval.start.microsecond or seconds%60))


def time_text(value):
    start=value.interval.start.astimezone(ZoneInfo(value.interval.zone));day=start.day
    suffix='th' if 10<day<14 else {1:'st',2:'nd',3:'rd'}.get(day%10,'th')
    return f'{start.strftime("%B")} {day}{suffix}, {start.strftime("%I:%M %p").lstrip("0")}'


def details_text(value):
    minutes=int((value.interval.end-value.interval.start).total_seconds()/60)
    return f'{value.first_name} {value.last_name}, {time_text(value)}, {minutes} minutes, {value.purpose}, {value.location}'


def room_words(text):
    return speech_words(re.sub(r'(?<=[A-Za-z])[- ]?(?=\d)',' ',text))


def established_details(turns,value):
    """Omit only details already supplied clearly in this conversation, never model claims."""
    # "Yes, it is correct" agrees to the proposal; it is not a replacement name claim.
    # Classify a reassembled answer, not individual streaming words such as "correct".
    identity_turns=[{'speaker':t['speaker'],'text':t['text'],'timestamp':t['end'],'end_timestamp':t['end']}
        for t in utterances(turns,join_sentences=True) if t['speaker']!='callee' or not affirmative_answer(t['text'])]
    if not caller_identified(identity_turns,value.first_name,value.last_name):return False
    minutes=int((value.interval.end-value.interval.start).total_seconds()/60)
    expected={'purpose':speech_words(value.purpose),'location':room_words(value.location),
        'duration':speech_words(str(minutes)+' minutes')}
    established=set();question=None
    for turn in utterances(turns,join_sentences=True):
        text=' '.join(words(turn['text']))
        if turn['speaker']=='assistant':
            if re.search(r'\b(?:friend|their|someone|another|his|her)\b',text):
                established.clear();question=None;continue
            question=('duration' if re.search(r'\b(?:how long|duration|how many minutes|length)\b',text) else
                'location' if re.search(r'\b(?:where|which room|what room|location|arrangement)\b',text) else
                'purpose' if re.search(r'\b(?:purpose|what.*(?:about|for)|reason|which course)\b',text) else None)
            continue
        if turn['speaker']!='callee':continue
        # Unresolved corrections, uncertainty or a third-party request cannot establish omitted fields.
        if '?' in turn['text'] or re.search(r"\b(?:no|not|actually|instead|wait|wrong|change|maybe|perhaps|friend|someone|their|rather)\b",text):
            established.clear();question=None;continue
        if question:
            answer=re.sub(r'^(?:it is|it\'s|for|in|at|i would like|i\'d like) ', '', text)
            answer=re.sub(r' (?:please|thank you|thanks)$','',answer)
            parsed=room_words(answer) if question=='location' else speech_words(answer)
            if parsed==expected[question]:established.add(question)
            else:established.discard(question)
        elif not affirmative_answer(turn['text']):
            # An unsolicited new room/purpose/duration must not leave old omitted details valid.
            established.clear()
        question=None
    return established==set(expected)


def matches(text,value,*,short):
    """Compare the entire spoken date/time and every non-omitted field, never a suffix."""
    actual=speech_words(text)
    prefixes=[''] if short else [value.first_name+' '+value.last_name]
    prefixes=[p+' '+name for p in READBACK_PREFIXES for name in prefixes]
    start=value.interval.start.astimezone(ZoneInfo(value.interval.zone))
    core=start.strftime('%I:%M %p')
    if not short:
        minutes=int((value.interval.end-value.interval.start).total_seconds()/60)
        core+=f', {minutes} minutes, {value.purpose}, {value.location}'
    for prefix in prefixes:
        before=speech_words(prefix)
        if actual[:len(before)]!=before:continue
        rest=actual[len(before):]
        for date in date_words(start,include_year=False):
            if rest[:len(date)]!=date:continue
            tail=rest[len(date):]
            for at in ('','at '):
                for question in ('Is that correct','Is that right','Is that okay','Does that work'):
                    if tail==speech_words(at+core+' '+question):return True
    return False
