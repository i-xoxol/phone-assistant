import json
import logging


def log_event(event: str, **fields) -> None:
    # Callers pass identifiers/state only, never request bodies or raw SDK errors.
    logging.getLogger('phone_agent').info(json.dumps({'event': event, **fields}))


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    for name in ('openai', 'httpx', 'httpx2', 'twilio', 'websockets'):
        logging.getLogger(name).setLevel(logging.WARNING)
