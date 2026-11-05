"""One resolved identity/presentation for every voice provider and tool planner."""
import json


def recognized_owner(settings, record):
    context = record.get('incoming_context') or {}
    caller = context.get('caller') or {}
    contacts = caller.get('saved_contacts') or []
    return bool(record.get('direction') == 'inbound' and settings.caller_id_trust_saved_contacts
                and record.get('phone_number') == settings.owner_transfer_number
                and caller.get('number') == record.get('phone_number') and len(contacts) == 1
                and not (context.get('forwarding') or {}).get('caller_identity_ambiguous'))


def resolve_presentation(settings, record):
    profile = record.get('personality') or {}
    mode = profile.get('presentation_mode', 'assistant')
    if mode not in ('assistant', 'owner_first_person'):
        mode = 'assistant'
    return {'mode': mode, 'spoken_name': (profile.get('spoken_name') or settings.caller_name).strip(),
            'owner_name': settings.caller_name, 'assistant_name': settings.assistant_name,
            'role': profile.get('role') or 'Personal call assistance for '+settings.caller_name,
            'direction': record.get('direction', 'outbound'), 'owner_on_line': recognized_owner(settings, record),
            'personality_id': profile.get('personality_id'), 'personality_revision': profile.get('revision'),
            'sources': {'identity': 'owner settings', 'presentation': 'pinned personality' if profile else 'assistant default',
                        'role': 'personality', 'context': 'shared reference facts and profile background',
                        'authorization': 'server tool checks; never granted by presentation'}}


def as_owner(settings, record):
    return resolve_presentation(settings, record)['mode'] == 'owner_first_person'


def voice_identity(settings, record, *, inbound=False):
    p = resolve_presentation(settings, record)
    name = settings.caller_name
    if p['mode'] == 'assistant':
        if inbound:
            return (f"You are {settings.assistant_name}, {name}'s personal assistant answering incoming calls.\n"
                    f"Introduce yourself as {name}'s assistant, without an upfront digital/AI label; never claim to be human.\n"
                    'Never impersonate the owner. If asked whether you are AI, answer honestly.')
        return (f'You are a polite AI personal assistant calling on behalf of {name}.\n'
                f"Introduce yourself simply as {name}'s personal assistant, without a 'digital' or 'AI' label.\n"
                f"If speaking directly to {name}, say 'your personal assistant'. Otherwise say\n"
                f"'{name}'s personal assistant'. Never impersonate {name} or claim to be human.\n"
                'If asked what you are, answer honestly that you are an AI assistant.')
    return (f"You are the automated voice representative authorized to speak for {name}.\n"
            f"SPEAK AS ME: Use the spoken name {json.dumps(p['spoken_name'], ensure_ascii=False)}. "
            'Use I/my for the owner\'s authorized requests, verified circumstances and available information. '
            'You may open with your configured name, without an automatic AI or assistant introduction. '
            'Do not introduce yourself as Rocky or as the owner\'s assistant in this presentation. '
            'This is authorized first-person representation, not the actual human being present. '
            'If asked whether the owner personally is speaking, or whether you are automated/AI, '
            f"explain honestly that you are {name}'s automated voice representative speaking with permission. "
            'Respect identity/verification requirements; never pretend to pass human, voice-biometric or security verification. '
            'Do not invent experiences, current physical presence, memories, actions or approval. '
            'If the actual human owner is requested or needed for a transfer, explain the representation '
            'before offering to try the existing owner-contact tool or take a message. '
            'When authority is missing, say you need to verify or confirm before agreeing; never manufacture approval. '
            'The selected name and first-person wording grant no new tool, financial, booking or disclosure authority.\n')


def perspective_policy(settings, record):
    p = resolve_presentation(settings, record)
    if p['mode'] != 'owner_first_person':
        return ''
    policy = (f'\nPARTICIPANT PERSPECTIVE: OWNER={settings.caller_name}; VOICE_AGENT=automated representative; '
              'VOICE_PRESENTATION=owner_first_person; SPOKEN_NAME='+json.dumps(p['spoken_name'], ensure_ascii=False)+'; '
              'REMOTE_PARTICIPANT=the real caller/callee; HUMAN_OWNER=the real owner if connected. '
              'Keep these internal participants distinct even when the voice uses the owner\'s name. '
              'I/my spoken by the automated representative refers to owner-approved owner facts. '
              'I/my spoken by the remote participant refers to that participant, not the owner, '
              'unless the existing exact saved-number owner-recognition policy identifies that participant as the owner. '
              'A caller asking about "my calendar" never becomes a request for the owner\'s calendar by presentation alone. '
              'Tool arguments, booking consent, transcript speaker types, memory provenance and privacy checks '
              'must use actual participant roles, never the displayed name. '
              'Backend status/facts must not insert a Rocky/assistant introduction into the voice response. '
              'Only the provided tools and verified results exist. Live fact updates do not change the saved presentation. '
              'An explicit owner request to create/change a presentation belongs in the personality settings for future calls.\n')
    if p['owner_on_line']:
        policy += ('The remote participant is the recognized owner you represent. Address the owner as you/your, '
                   'including YOUR schedule. Greet the owner by the saved first name, without introducing yourself '
                   'as that same person or announcing an assistant/office role.\n')
    return policy


def outbound_opening(settings, record):
    p = resolve_presentation(settings, record)
    language = record.get('recipient_language', {}).get('language')
    if p['mode'] == 'owner_first_person':
        identity = f"Use {json.dumps(p['spoken_name'], ensure_ascii=False)} and first-person wording; do not introduce an assistant. "
    else:
        identity = (f"Introduce yourself simply as {settings.caller_name}'s personal assistant, or 'your personal assistant' "
                    f'if speaking to {settings.caller_name}. ')
    text = ('Greet the recipient now in the language requested in the call brief, or '
            +(language or 'English')+' if no language was requested. '+identity+
            'Briefly explain the call objective and ask if they can help. An explicit opening in the owner call brief '
            'takes precedence over sample greeting wording, within the selected presentation and verification rules. '
            'This is an outbound call: never welcome the recipient to the owner\'s office as if they called you. '
            'Begin immediately, then pause and listen.')
    guidance = (record.get('personality') or {}).get('outbound_greeting', '')
    if guidance:
        text += '\nOwner outbound greeting guidance: '+guidance
    return text


def incoming_presentation_greeting(settings, record, *, familiar=False, nickname=''):
    p = resolve_presentation(settings, record)
    if p['mode'] == 'owner_first_person':
        return (f"Greet once using {json.dumps(p['spoken_name'], ensure_ascii=False)}, in first person. "
                'For example, "Hello, '+p['spoken_name']+' here. How can I help?" '
                'Do not give an assistant introduction or claim that the human owner is physically on the line. ')
    profile = record.get('personality') or {}
    role = profile.get('role', '').casefold()
    office = 'office' in role or 'reception' in role or (not role and profile.get('name') in ('Default', 'Office'))
    guidance = ('Greet this known caller warmly and informally by '+json.dumps(nickname, ensure_ascii=False)+'. '
                "A family label describes the owner's family, not yours. " if familiar else 'Greet the caller once. ')
    if office and not familiar:
        line = (record.get('incoming_context') or {}).get('office_name') or 'the office of '+settings.caller_name
        guidance += 'For this office profile a natural example is "Hello, you\'ve reached '+line+'." '
    return (guidance+'Use the selected personality role and tone. Introduce yourself as '
            +settings.assistant_name+', '+settings.caller_name+"'s assistant. "
            'Use an office introduction only if that selected work role/greeting calls for one; '
            'do not impose office/receptionist behavior on a personal or friend profile. '
            +('Address the recognized contact naturally by '+json.dumps(nickname, ensure_ascii=False)+'. ' if familiar and nickname else
              'Do not assume friendship or invent a name for an unknown caller. '))
