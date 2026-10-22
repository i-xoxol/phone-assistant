"""Human-readable calendar details and incomplete meeting arrangements."""
import re
from html import escape


def missing_arrangement(value):
    text=' '.join(re.findall(r'\w+',value.casefold()))
    if not text or re.search(r'\b(?:tbd|unknown|unspecified|not specified|not sure|to be determined|to be confirmed)\b',text):return True
    if re.search(r'\b(?:office|room) [a-z]\b',text):return False
    # A mode is not a meeting place or a way to connect. Keep actual named
    # rooms, addresses and connection instructions verbatim for spoken consent.
    generic={'in','person','personal','inperson','meeting','appointment','at','the','a','an','my','your',
        'office','campus','on','online','remote','virtual','via','by','over','phone','call','telephone',
        'zoom','teams','microsoft','google','meet','video','conference','please','usual','link'}
    return all(word in generic for word in text.split())


def require_arrangement(value):
    if missing_arrangement(value):raise ValueError('A specific meeting place or connection method is required')


def calendar_title(value):
    return 'Appointment with '+' '.join((value.first_name+' '+value.last_name).split())


def calendar_description(value):
    # Google descriptions can contain HTML. Caller text must remain text, not
    # markup or additional forged field labels. Never include a raw transcript.
    clean=lambda text:escape(' '.join(text.split()),quote=False)
    minutes=(value.interval.end-value.interval.start).total_seconds()/60
    lines=[f'Caller: {clean(value.first_name+" "+value.last_name)}',
        f'Purpose: {clean(value.purpose)}',f'Meeting details: {clean(value.location)}',f'Duration: {minutes:g} minutes']
    if missing_arrangement(value.location):lines.append('Room/address or connection details were not specified during booking.')
    return '\n'.join(lines)+'\n\nBooked through the phone assistant after caller confirmation.'


def arrangement_clarification():
    return {'state':'needs_details','reason':'specific_arrangement_required','missing_fields':['arrangement'],
        'readback_queued':False,'calendar_write_attempted':False,'reservation':False,
        'instruction':'Ask one short question for the actual room/address, or how to connect for a remote meeting. '
            'In person, online, Zoom or phone alone is incomplete. Reuse all other collected details. '
            'An existing owner-approved office location may be offered as a proposal, but never invent a location or link. '
            'Check the completed arrangement, then wait for agreement to its exact readback before booking.'}
