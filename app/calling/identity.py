"""Owner-supplied pronouns, kept distinct from the caller's identity."""

WORDS = {'he/him': ('he', 'him', 'his'), 'she/her': ('she', 'her', 'her'),
         'they/them': ('they', 'them', 'their')}


def owner_words(settings):
    return WORDS[settings.owner_pronouns]


def owner_identity_policy(settings):
    words = '/'.join(owner_words(settings))
    policy = (f'\nOWNER PRONOUNS: {settings.caller_name} is the owner. Use {words} when referring '
              'to the owner, including introductions, messages and follow-up replies. ')
    if settings.owner_pronouns != 'they/them':
        policy += 'Do not use they/them/their to refer to the owner. '
    return policy + ('Use corresponding grammatical gender in other languages. If speaking directly to the owner, '
        'use you/your. These are the owner\'s pronouns, not the incoming caller\'s; do not confuse their identities.\n')
