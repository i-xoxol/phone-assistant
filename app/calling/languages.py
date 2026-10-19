"""Language is a caller preference, never proof of identity or permission."""
import re
import unicodedata
from pydantic import BaseModel, ConfigDict, Field

# Short startup text is local, never a per-call TTS/translation request. For
# other codes the Live instructions still select the requested conversation language.
GREETINGS={
    'es': ('Hola', 'Soy {assistant}, {role}.', 'tu asistente', 'asistente de {owner}', '¿En qué puedo ayudarle?'),
    'fr': ('Bonjour', 'Je suis {assistant}, {role}.', 'votre assistant', "l’assistant de {owner}", 'Comment puis-je vous aider ?'),
    'de': ('Guten Tag', 'Ich bin {assistant}, {role}.', 'Ihr Assistent', 'der Assistent von {owner}', 'Wie kann ich Ihnen helfen?'),
    'pl': ('Dzień dobry', 'Jestem {assistant}, {role}.', 'twój asystent', 'asystent osoby {owner}', 'W czym mogę pomóc?'),
    'ru': ('Здравствуйте', 'Я {assistant}, {role}.', 'ваш помощник', 'помощник {owner}', 'Чем могу помочь?'),
    'pt': ('Olá', 'Sou {assistant}, {role}.', 'seu assistente', 'assistente de {owner}', 'Como posso ajudar?'),
    'it': ('Buongiorno', 'Sono {assistant}, {role}.', 'il suo assistente', 'l’assistente di {owner}', 'Come posso aiutarla?'),
    'ar': ('مرحباً', 'أنا {assistant}، {role}.', 'مساعدك', 'مساعد {owner}', 'كيف يمكنني مساعدتك؟'),
    'zh': ('您好', '我是{assistant}，{role}。', '您的助理', '{owner}的助理', '有什么可以帮您？'),
    'ja': ('こんにちは', '{role}の{assistant}です。', 'あなたのアシスタント', '{owner}のアシスタント', 'どのようなご用件でしょうか？'),
    'ko': ('안녕하세요', '{role} {assistant}입니다.', '고객님의 비서', '{owner}의 비서', '무엇을 도와드릴까요?'),
    'hi': ('नमस्ते', 'मैं {assistant}, {role} हूँ।', 'आपका सहायक', '{owner} का सहायक', 'मैं आपकी क्या सहायता कर सकता हूँ?'),
}


def localized_greeting(language,assistant,owner,*,is_owner=False,nickname=''):
    phrases=GREETINGS.get(language.split('-')[0])
    if not phrases:
        return f"Hello, I'm {assistant}, "+('your assistant.' if is_owner else owner+"'s assistant.")+' How can I help?'
    hello,intro,to_owner,about_owner,question=phrases
    role=(to_owner if is_owner else about_owner).format(owner=owner)
    return hello+(', '+nickname if nickname else '')+'. '+intro.format(assistant=assistant,role=role)+' '+question


class LanguageCode(BaseModel):
    model_config=ConfigDict(extra='forbid')
    language: str=Field(pattern=r'^[a-z]{2,3}(?:-[A-Z]{2})?$',max_length=12)


class CallerLanguage(LanguageCode):
    request_quote: str=Field(min_length=3,max_length=300)


class OwnerLanguage(LanguageCode):
    phone_number: str=Field(pattern=r'^\+[1-9][0-9]{7,14}$')


LANGUAGE_TOOL={'type':'function','name':'remember_caller_language','strict':True,
    'description':'Switch this incoming call to the language explicitly requested by the actual caller and remember '
        'it for this presented caller number. Supply an ISO language code (en, uk, es etc.) and a short exact quote '
        'from the caller request. Not for language inferred from accent or a quoted hypothetical request. '
        'No phone-number argument: the server binds it to this call. Unknown/ambiguous numbers are current-call only. '
        'This changes no identity, permissions, contact name or confidential information.',
    'parameters':CallerLanguage.model_json_schema()}


def normalized(text):
    return re.sub(r'\W+',' ',unicodedata.normalize('NFKC',text).casefold()).strip()


def remember_language(manager, call_id, body):
    record=manager.db.get(call_id)
    if record['direction']!='inbound':
        raise ValueError('Incoming caller language only')
    quote=normalized(body.request_quote)
    speech=' '.join(turn['text'] for turn in record.get('transcript',[])[-20:] if turn['speaker']=='callee')
    if len(quote)<3 or quote not in normalized(speech):
        return {'status':'unconfirmed','remembered':False,'language':body.language,
                'instruction':'Use the requested language now, but its request is not confirmed in the caller transcript. '
                    'Do not claim it was remembered. If needed ask the caller to repeat the language request.'}
    routing=manager.routing.get(record)
    number=record['phone_number'] if not routing.get('caller_identity_ambiguous') else ''
    if not re.fullmatch(r'\+[1-9][0-9]{7,14}',number or ''): number=''
    try:
        result=manager.knowledge.set_language(number,body.language,source='caller',call_id=call_id)
    except ValueError:
        result=manager.knowledge.set_language('',body.language,source='caller',call_id=call_id)
    manager.db.emit(call_id,'caller.language_changed',{'language':body.language,'remembered':result['remembered']})
    return {**result,'instruction':'Continue naturally in this language. Say the preference is remembered only when remembered=true.'}
