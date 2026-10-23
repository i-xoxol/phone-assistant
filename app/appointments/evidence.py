"""Conservative spoken evidence matching; uncertainty never authorizes disclosure or a write."""
import re,unicodedata
from zoneinfo import ZoneInfo

READBACK_PREFIXES=('','Okay','OK','All right','Alright','Right','Sure','Thank you','Thanks',
    'Let me confirm','Let me confirm that','Just to confirm','Thank you let me confirm that','Okay let me confirm')

def words(text):
    text=unicodedata.normalize('NFKC',text).casefold().replace('’',"'")
    return tuple(re.findall(r"[^\W_]+(?:['-][^\W_]+)*",text))


def spelled_word(text):
    """Decode a complete letter sequence within one explicitly identified name field."""
    if any(not (c.isalpha() or c.isspace() or c in '.,-') for c in text):return None
    tokens=words(text.replace('-',' ').replace('.',' '));letters=[];i=0
    while i<len(tokens):
        if len(tokens[i])!=1 or not tokens[i].isalpha():return None
        letters.append(tokens[i]);i+=1
        if tokens[i:i+2]==('as','in'):
            if i+2>=len(tokens) or not tokens[i+2].isalpha():return None
            i+=3
    return ''.join(letters) if 2<=len(letters)<=64 else None


LOOKUP_FILLERS=r'(?:(?:yes|yeah|sure|okay|ok|uh|um|oh|ah|of course)[.,\s]+)*'


def lookup_acknowledgment(text):
    return '?' not in text and bool(re.fullmatch(r'(?:yes|yeah|sure|okay|ok|uh|um|oh|ah|of course)(?: (?:yes|yeah|sure|okay|ok|uh|um|oh|ah|of course))*',' '.join(words(text))))


def lookup_spelling(text):
    """One elicited component: exact letters, optionally after its spoken label."""
    text=re.sub('^'+LOOKUP_FILLERS,'',text.casefold().replace('’',"'").strip(),count=1).strip()
    direct=spelled_word(text)
    if direct:return direct
    match=re.fullmatch(r"([^\W\d_]+(?:['-][^\W\d_]+)*)[.,:\s]+(.+)",text)
    if not match:return None
    label,letters=match.groups();spelling=spelled_word(letters)
    if not spelling or label in {'not','no','for','friend','his','her','their','instead','but','if','unless','except','or','and','wrong'}:return None
    # In an explicitly requested component, the caller's complete letters
    # correct the spoken label, which ASR may transcribe very differently.
    # The entire answer must still be consumed; qualifiers above are rejected.
    return spelling


def lookup_name_question(text):
    raw=text;text=' '.join(words(text))
    bare=re.fullmatch(r'(?:(?:thank you|thanks|okay|ok|now|and|please|could you|can you|would you|spell|repeat|say) )*'
        r'(?:(?:the|your) )?(first name|given name|last name|surname|family name|full name|first and last name|name)(?: (?:please|for me|again))*',text)
    request=re.search(r"\b(?:repeat|spell|say|tell me|give me|what is|what's|who am i speaking|(?:could|can|would|may) (?:you|i) (?:please )?(?:have|confirm))\b",text)
    if '?' not in raw and not request and not bare:return None
    if re.search(r'\byour (?:full name|first and last name|name)\b|\bwho am i speaking (?:with|to)\b',text):return 'full'
    if re.search(r'\byour (?:last name|surname|family name)\b',text):return 'last'
    if re.search(r'\byour (?:first name|given name)\b',text):return 'first'
    if bare:
        if bare[1] in ('full name','first and last name','name'):return 'full'
        return 'first' if bare[1] in ('first name','given name') else 'last'
    return None


def lookup_spelling_completed(turns,*,first_matched=False,first_missing=False):
    first_asked=first_answered=last_asked=False
    last_field=last_spell=False
    for turn in utterances(turns,join_sentences=True,join_caller_pauses=True):
        if turn['speaker']=='assistant':
            field=lookup_name_question(turn['text'])
            if field=='first' and re.search(r'\bspell\b',turn['text'],re.I):first_asked=True
            if field=='last' and first_answered:last_asked=True
            last_field=field=='last'
            last_spell=last_field and bool(re.search(r'\bspell\b',turn['text'],re.I))
        elif turn['speaker']=='callee':
            if lookup_acknowledgment(turn['text']):continue
            text=re.sub('^'+LOOKUP_FILLERS,'',turn['text'].casefold(),count=1).strip(' .,')
            if not text:continue
            if last_asked:return True
            if first_matched and last_field and (last_spell or lookup_spelling(turn['text'])):return True
            if first_asked:first_answered=True
            if first_missing and first_asked and first_answered:return True
    return False


def contains_words(text,value):
    tokens,part=words(text),words(value)
    return bool(part) and any(tokens[i:i+len(part)]==part for i in range(len(tokens)-len(part)+1))


def affirmative_answer(text):
    """Clear agreement in response to the bound proposal; no prescribed booking phrase."""
    if '?' in text:return False
    text=' '.join(words(text))
    if len(text.split())>40:return False
    agree=(r"yes|yeah|yep|yup|sure|okay|ok|all right|alright|correct|right|absolutely|definitely|certainly|"
        r"perfect|great|fine|sounds (?:good|great|fine|right|perfect)(?: to me)?|looks good|go ahead|let's do (?:it|that)|"
        r"that (?:works|will work|would work|should work)(?: for me)?|that sounds (?:good|great|fine|right|perfect)|"
        # Live captions can retain an extra copula/contraction before a modal
        # ("that's would work"). Match the complete positive clause, never drop
        # arbitrary words or quantities from a caller's answer.
        r"(?:that|this|it)(?:'s| is)? (?:will|would|should) work(?: for me)?|"
        r"(?:that|this|it) sounds (?:good|great|fine|right|perfect)(?: to me)?|"
        r"(?:i think )?(?:(?:that|this|it)(?:'ll| will| would| should)?|that time|the time|that slot|the slot) works?(?: for me)?|"
        r"(?:that is|that's|it is|it's|that would be|that will be) (?:okay|ok)(?: (?:for|with) me)?|"
        r"works for me|i can (?:do|make) (?:it|that)|i'm good with (?:it|that)|"
        r"i agree (?:with|to) (?:this|that|the) appointment|"
        r"(?:that is|that's|it is|it's|that would be|that will be) (?:correct|right|fine|good|great|perfect)(?: (?:for|with) me)?|"
        r"(?:(?:i want|i would like|i'd like) you to |you (?:can|may) |please )?(?:book|schedule) (?:it|that|the appointment|that appointment|this appointment)|"
        r"так|так правильно|правильно")
    unit=r'(?:'+agree+r'|please|thank you|thanks)'
    return bool(re.fullmatch(r'(?:(?:uh|um|well) )*'+unit+r'(?: (?:and )?'+unit+r')*',text)
        and re.search(r'\b(?:'+agree+r')\b',text))


def ambiguous_agreement(text):
    """A non-committal answer with no new appointment fields permits clarification only."""
    allowed=set("yes yeah yep yup sure okay ok correct right good great fine perfect that this it that's it's is "
        "would will should could might work works for me sounds sound to thank you thanks please and "
        "maybe perhaps think i guess probably unsure".split())
    tokens=words(text)
    return bool(tokens and len(tokens)<=40 and '?' not in text and set(tokens)<=allowed and not affirmative_answer(text))


def non_substantive_status(text):
    """A final assistant status cannot revoke current caller consent by wording alone.

    New caller speech is still evaluated in full. Questions, quantities, meeting
    fields, third-party references and corrections cannot use this path.
    """
    if '?' in text or len(words(text))>40:return False
    text=' '.join(words(text))
    text=re.sub(r'\b(?:give me (?:just )?(?:a|one) (?:moment|second|minute)|(?:just )?(?:a|one) (?:moment|second))\b',' ',text)
    quantities=set(('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
        'sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand first second third fourth fifth sixth '
        'seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth '
        'nineteenth twentieth thirtieth fortieth fiftieth sixtieth noon midnight morning afternoon evening '
        'am pm eastern central mountain pacific utc gmt est edt cst cdt mst mdt pst pdt').split())
    if quantities&set(re.findall(r'[a-z]+',text)):return False
    changed=r"\d|\b(?:no|not|never|don't|do not|can't|cannot|won't|cancel|instead|different|another|actually|wrong|change|rather|unless|except|if|friend|someone|their|his|her|name|purpose|room|office|building|campus|address|location|online|zoom|phone|minute|minutes|hour|hours|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december)\b"
    status_words=set("i i'll i'm will am let me just a while finish finishing check checking verify verifying "
        "finalize finalizing complete completing book booking schedule scheduling process processing take taking "
        "care of get getting put putting add adding appointment booking request details step calendar it that this "
        "the your for you with on in to through now please okay ok sure great perfect thank thanks all right alright".split())
    return not re.search(changed,text.casefold()) and set(words(text))<=status_words


def appointment_continuation(text,*,partial_pause=False):
    """Only status/acknowledgment or another question about this same booking may carry consent."""
    normalized=' '.join(words(text))
    if len(normalized.split())>60:return False
    target=r'(?:it|that|this|the appointment|that appointment|this appointment|the booking|that booking)'
    question=(r'(?:(?:would you like|do you want) me to (?:book|schedule) '+target+r'|'
        r'(?:shall|can|may) i (?:book|schedule) '+target+r'|is that (?:correct|right|okay)|does that work(?: for you)?|'
        r'could you (?:please )?confirm (?:that )?you agree to '+target+r')')
    acknowledgment=r'(?:okay|ok|all right|alright|right|sure|great|perfect|thank you|thanks)'
    # A live caption may end at "Okay, give me a" while a booking starts.
    # This only preserves a prior complete readback AND caller agreement; it
    # cannot establish either. Only a final, unfinished, non-question pause is
    # accepted. Any later substantive completion is evaluated in full again.
    if partial_pause and '?' not in text and re.fullmatch(
            r'(?:'+acknowledgment+r' )*give me(?: just)?(?: (?:a|one))?',normalized):return True
    pause=r'(?:(?:just )?(?:a|one) moment(?: please)?|give me (?:just )?(?:a|one) moment(?: please)?)'
    status=(r"(?:let me|i will|i'll|i am|i'm) (?:check|checking|verify|verifying|finish|finalize|complete|book|booking|schedule|scheduling|process|processing) "
        r'(?:'+target+r'|the details|the final step|that step|the request)(?: (?:now|for you|with the calendar|on the calendar))?')
    booking=r'(?:booking|scheduling) '+target+r'(?: now| for you)?'
    unit=r'(?:'+acknowledgment+'|'+pause+'|'+status+'|'+booking+'|'+question+r')'
    return bool(re.fullmatch(unit+r'(?: '+unit+r')*',normalized)
        and ('?' not in text or re.search(question,normalized)))


def spoken_consent(turns,value,*,matches=None):
    """Bind agreement to a complete proposal, retaining it across harmless assistant follow-ups."""
    blocks=[]
    for turn in turns:
        if blocks and blocks[-1]['speaker']==turn['speaker']:
            blocks[-1]['text']+=turn['text'];blocks[-1]['end_ms']=turn['end_ms']
        else:blocks.append(dict(turn))
    heard=False;answer='';readback_end=-1
    for index,block in enumerate(blocks):
        if block['speaker']=='assistant':
            if (matches(block['text']) if matches else complete_readback(block['text'],value)):
                heard=True;answer='';readback_end=block['end_ms']
            elif not (heard and (appointment_continuation(block['text'],
                    partial_pause=index==len(blocks)-1 and affirmative_answer(answer))
                    or (index==len(blocks)-1 and affirmative_answer(answer) and non_substantive_status(block['text'])))):
                heard=False;answer=''
        elif block['speaker']=='callee' and heard and block['start_ms']>=readback_end:
            answer=block['text']
            if not (affirmative_answer(answer) or ambiguous_agreement(answer)):heard=False
        else:heard=False;answer=''
    return heard,heard and affirmative_answer(answer)


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


def date_words(start,*,include_year=True):
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
    return {speech_words(start.strftime('%B')+' '+d+(' '+y if include_year else '')) for d in days for y in years}


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
    prefixes=[polite+' '+prefix for polite in READBACK_PREFIXES for prefix in prefixes]
    suffixes=['','Is that correct','Is that right','Is that okay','Does that work']
    actual=speech_words(text)
    tails=[]
    dates=date_words(start)
    for prefix in prefixes:
        before=speech_words(prefix)
        if actual[:len(before)]!=before:continue
        remaining=actual[len(before):]
        for date in dates:
            if remaining[:len(date)]==date:tails.append(remaining[len(date):])
    if not tails:return False
    for zone in zones:
        core=f'{start.strftime("at %I:%M %p")} {zone}, {int(seconds/60)} minutes, {value.purpose}, {value.location}'
        for suffix in suffixes:
            if speech_words(core+' '+suffix) in tails:return True
    return False


def utterances(turns,*,join_sentences=False,join_caller_pauses=False):
    """Reassemble adjacent transcript deltas, preserving speaker, pauses and sentence boundaries."""
    result=[]
    for turn in turns:
        start=turn.get('timestamp',turn.get('start_ms',0)/1000)
        end=turn.get('end_timestamp',turn.get('end_ms',start*1000)/1000)
        if (result and result[-1]['speaker']==turn['speaker']
                and ((join_caller_pauses and turn['speaker']=='callee') or start<=result[-1]['end']+.75)
                and (join_sentences or not re.search(r'[.!?]\s*$',result[-1]['text']))):
            separator=' ' if re.search(r'[.!?]$',result[-1]['text']) and not turn['text'][:1].isspace() else ''
            result[-1]['text']+=separator+turn['text'];result[-1]['end']=max(end,result[-1]['end'])
        else:result.append({'speaker':turn['speaker'],'text':turn['text'],'end':end})
    return result


def lookup_utterances(turns):
    """Rejoin caller answers across complete, neutral assistant continuations."""
    prepared=utterances(turns,join_sentences=True,join_caller_pauses=True)
    joined=[]
    for turn in prepared:
        if (turn['speaker']=='assistant' and '?' not in turn['text']
                and re.fullmatch(r'(?:m+h+m*|h+m+|m+ h+m+|uh huh|okay|ok|thanks|thank you|sure|go on(?: please)?|take your time)(?: (?:m+h+m*|h+m+|m+ h+m+|uh huh|okay|ok|thanks|thank you|sure|go on(?: please)?|take your time))*',
                    ' '.join(words(turn['text'].replace('-',' '))))):continue
        if joined and turn['speaker']=='callee' and joined[-1]['speaker']=='callee':
            separator=' ' if re.search(r'[.!?]$',joined[-1]['text']) and not turn['text'][:1].isspace() else ''
            joined[-1]['text']+=separator+turn['text'];joined[-1]['end']=max(joined[-1]['end'],turn['end'])
        else:joined.append(dict(turn))
    return joined


def lookup_spelled_names(turns):
    """Collect literal component spellings; callers' later corrections still govern validity."""
    result={'first_name':None,'last_name':None};question=None
    for turn in lookup_utterances(turns):
        if turn['speaker']=='assistant':question=lookup_name_question(turn['text']);continue
        if turn['speaker']!='callee' or question not in ('first','last'):continue
        body=re.sub(r"^(?:it's|it is)\s+",'',turn['text'].casefold().strip())
        spelling=lookup_spelling(body)
        if spelling:
            result[question+'_name']=spelling
            if question=='first':result['last_name']=None
            question=None
    return result


def lookup_name_card(turns):
    """Project caller-supplied components, never calendar candidates, into a name card."""
    card={'first_name':None,'last_name':None};question=None;initial_claim=False
    excluded={'no','not','my','for','friend','his','her','their','and','or','but','if','unless','instead',
        'tomorrow','today','please','appointment','meeting','in','at','on','with','about','calling','checking',
        'waiting','late','early','here','there','just','still','listening','ready','confused','looking','trying'}
    def name_words(body):
        parts=words(body)
        return parts if len(parts) in (1,2) and not set(parts)&excluded and all(
            re.fullmatch(r"[^\W\d_]+(?:['-][^\W\d_]+)*",p) for p in parts) else ()
    for turn in lookup_utterances(turns):
        if turn['speaker']=='assistant':question=lookup_name_question(turn['text']);continue
        if turn['speaker']!='callee':continue
        text=turn['text'].casefold().replace('’',"'").strip()
        part=re.fullmatch(r"(?:my|the) (first name|given name|last name|surname|family name)(?: is|'s)\s+(.*)",text)
        field=question;body=text
        if part:field='first' if part[1] in ('first name','given name') else 'last';body=part[2]
        if field in ('first','last'):
            body=re.sub(r"^(?:it's|it is)\s+",'',body)
            value=lookup_spelling(body)
            literal=name_words(re.sub('^'+LOOKUP_FILLERS,'',body,count=1))
            if not value and len(literal)==1:value=literal[0]
            if value:
                card[field+'_name']=value
                if field=='first':card['last_name']=None
                question=None
                continue
        if initial_claim:continue
        for sentence in re.split(r'[.!?]+',text):
            claim=re.fullmatch(r"(?:(?:hi|hello|yes|no|actually|sorry|correction|uh|um)[,\s]+)*(?:my name(?: is|'s)|this is|i am|i'm)\s+(.*)",sentence.strip())
            body=claim[1] if claim else None
            if body is None and question=='full':
                body=re.sub(r'^(?:my\s+){1,2}name[-,:]\s*','',sentence.strip())
                body=re.sub('^'+LOOKUP_FILLERS,'',body,count=1)
            literal=name_words(body) if body is not None else ()
            if literal and (claim is not None or len(literal)==2):
                card['first_name']=literal[0];card['last_name']=literal[1] if len(literal)==2 else None
                initial_claim=True;question=None;break
    return card


def caller_name_evidence(turns,first,last,*,for_lookup=False):
    """Only the current full claim or two explicitly elicited components may identify the caller."""
    expected_first,expected_last=words(first),words(last)
    prepared=lookup_utterances(turns) if for_lookup else utterances(turns,join_sentences=True,join_caller_pauses=True)
    full=expected_first+expected_last;given=surname=False;question=None
    fillers=r'(?:(?:hello|hi|yes|actually|sorry|no|correction)[,\s]+)*'
    if for_lookup:fillers=r'(?:(?:hello|hi|hai|hey|yes|actually|sorry|no|correction|uh(?:-(?:hi|hai))?|um|ah|er)[,\s]+)*'
    explicit=r"(?:my name(?: is|'s)|call me|i go by)"
    intro=fillers+r"(?:"+explicit+r"|this is|i am|i'm|it's|it is)\b\s*(.*)"
    component=fillers+r"(?:my|the) (first name|given name|last name|surname|family name)(?: is|'s)\b\s*(.*)"
    recheck=r'(?:(?:no|please) )*(?:check|recheck|look up)(?: for)? '+re.escape(' '.join(full))
    if for_lookup:
        recheck=r"(?:(?:no|please) )*(?:(?:can|could) you )?(?:check|recheck|look up)(?: (?:uh|um))?(?: it's)?(?: for)? "+re.escape(' '.join(full))
    sentences=[]
    # A pause in "Yes, [name]" does not end an elicited answer. Keep
    # assistant turns separate so an unrelated question cannot elicit a name.
    for turn in prepared:
        if turn['speaker']!='callee':sentences.append(turn);continue
        # A period inside a complete supplied name (for example St. John) is not a sentence boundary.
        text=turn['text'].casefold().replace('’',"'");spans=[]
        if for_lookup:
            raw_claim=re.fullmatch(intro,text);raw_part=re.fullmatch(component,text)
            letter_answer=re.sub('^'+fillers,'',text,count=1)
            if (lookup_spelling(letter_answer) or (raw_claim and lookup_spelling(raw_claim[1]))
                    or (raw_part and lookup_spelling(raw_part[2]))):
                # Letter punctuation ("C. A. S. E. Y.") is not a series of
                # separate answers. Field boundaries are still required below.
                sentences.append({**turn,'text':text});continue
        if for_lookup and re.fullmatch(recheck,' '.join(words(text))):
            # Keep "No, no. Check [same full name]" as one lookup request.
            # It may retain an earlier self-claim, never establish a new one.
            sentences.append({**turn,'text':text});continue
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
            if for_lookup:
                # The assistant asking again is not a caller correction. Keep
                # established evidence until the caller actually replaces it.
                question=lookup_name_question(text);continue
            if re.search(r'\byour (?:full name|first and last name|name)\b|\bwho am i speaking (?:with|to)\b',text):question='full';given=surname=False
            elif re.search(r'\byour (?:last name|surname|family name)\b',text):question='last';surname=False
            elif re.search(r'\byour (?:first name|given name)\b',text):question='first';given=surname=False
            else:question=None
            continue
        if turn['speaker']!='callee':continue
        if for_lookup and question and words(text) not in (full,expected_first,expected_last):
            if lookup_acknowledgment(text):continue
        if for_lookup and given and surname and question is None and re.fullmatch(recheck,' '.join(words(text))):continue
        claim=re.fullmatch(intro,text)
        part=re.fullmatch(component,text)
        name_cue=explicit+r"|\b(?:this is|i am|i'm|it's|it is|first name|last name|surname|family name|name)\b"
        followup=r"(?:(?:i am|i'm) (?:calling|phoning|asking|inquiring|checking) (?:about|regarding|for|on|to confirm|to check|to discuss)|(?:it's|it is) (?:about|regarding)) (?:my|the|an) (?:(?:existing|upcoming|scheduled) )?appointment(?: please| thank you)?"
        ordinary=re.fullmatch(followup,' '.join(words(text)))
        # A refusal to supply a forgotten appointment time is not a new name.
        # Retain only an already complete claim across this closed vocabulary;
        # names, third parties and incomplete identity corrections cannot pass.
        recall_words=set("no no-no actually i just want wanted need would like to check confirm clarify with you my the "
            "time date and of appointment don't do not really remember recall what was is it called um uh see".split())
        tokens=words(text)
        recall=bool(set(tokens)<=recall_words and {'check','confirm','clarify','remember','recall'}&set(tokens))
        if (ordinary or recall) and question is None:continue
        if part:
            field,answer=part.groups();field='last' if field in ('last name','surname','family name') else 'first'
        elif claim:
            field,answer='full',claim[1]
            if for_lookup and question in ('first','last') and re.fullmatch(fillers+r"(?:it's|it is)\b\s*.*",text):
                field=question
        elif question:
            field,answer=question,text
            # Preserve exact names (including a name that is also a filler).
            # Otherwise remove only a leading acknowledgment on an elicited
            # answer; the remaining complete name must still match exactly.
            if words(answer) not in (full,expected_first,expected_last):
                answer=re.sub('^'+fillers+r"(?:(?:it's|it is)\s+)?",'',answer,count=1)
            if for_lookup and field=='full':
                answer=re.sub(r'^(?:my\s+){1,2}name[-,:]\s*'+LOOKUP_FILLERS,'',answer,count=1)
        elif re.search(r"\b(?:not|isn't)\b",text) and (contains_words(text,first) or contains_words(text,last) or 'name' in text):
            given=surname=False;question=None;continue
        elif re.match(r'(?:actually|no|correction)\b',text):
            given=surname=False;question=None;continue
        elif re.search(name_cue,text):
            given=surname=False;question=None;continue
        else:continue
        spelling=lookup_spelling(answer) if for_lookup and field in ('first','last') else None
        answer=(spelling,) if spelling else words(answer)
        if field=='full':
            given=answer in (full,expected_first);surname=answer==full
            if (for_lookup and not given and len(answer)==len(full) and answer[:len(expected_first)]==expected_first
                    and not set(answer)&{'for','my','friend','his','her','their','not','no','but','if','unless','instead','tomorrow','today','please','and','or','appointment','meeting'}):
                given=True  # Exact first component of this complete current self-claim.
        elif field=='first':given=answer==expected_first;surname=False
        else:surname=answer==expected_last
        question=None
    return bool(given),bool(surname)


def caller_identified(turns,first,last,*,for_lookup=False):
    return all(caller_name_evidence(turns,first,last,for_lookup=for_lookup))
