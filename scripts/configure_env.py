"""Store one locally supplied setting without echoing its value."""
import argparse
import re
import sys

from dotenv import set_key

from app.config import ROOT

PATTERNS = {
    'TWILIO_AUTH_TOKEN': r'[a-fA-F0-9]{32}',
    'TWILIO_ACCOUNT_SID': r'AC[a-fA-F0-9]{32}',
    'TWILIO_PHONE_NUMBER': r'\+[1-9][0-9]{7,14}',
    'OPENAI_API_KEY': r'sk-[A-Za-z0-9_-]{20,}',
    'PUBLIC_BASE_URL': r'https://[A-Za-z0-9.-]+(?::[0-9]+)?',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--field', required=True, choices=PATTERNS)
    args = parser.parse_args()
    value = sys.stdin.read().strip()
    if not re.fullmatch(PATTERNS[args.field], value):
        print('Value did not match the required format; nothing saved.', file=sys.stderr)
        return 1
    set_key(ROOT / '.env', args.field, value)
    print(args.field + ' saved locally (value hidden).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
