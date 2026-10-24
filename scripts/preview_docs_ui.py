"""Read-only localhost preview of the real monitor with fictional documentation data.

No app imports, credentials, database, provider traffic, calls, or calendar writes.
Run: python -m scripts.preview_docs_ui; open http://127.0.0.1:8765/live
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STAMP = '2026-10-23T14:30:00Z'
CALL = {
    'call_id': 'documentation-demo', 'direction': 'outbound',
    'phone_number': '+12025550101', 'recipient': 'Example service desk',
    'status': 'in_progress', 'created_at': STAMP, 'started_at': None,
    'duration_seconds': 72,
    'objective': 'Ask about pickup hours. Do not change the service request or authorize charges.',
    'transcript': [
        {'speaker': 'assistant', 'timestamp': 0, 'end_timestamp': 5,
         'text': "Hello, I'm an AI assistant calling for Alex. Could I check your pickup hours?"},
        {'speaker': 'user', 'timestamp': 9, 'end_timestamp': 14,
         'text': 'Of course. Our pickup desk is open until five today.'},
        {'speaker': 'assistant', 'timestamp': 18, 'end_timestamp': 22,
         'text': "Thank you. I'll pass that along to Alex."},
    ],
}
MESSAGE = {
    'message_id': 'fictional-message', 'call_id': 'documentation-demo',
    'caller_name': 'Example caller', 'caller_id': '+12025550102',
    'callback_number': None, 'created_at': STAMP, 'status': 'new',
    'capture_method': 'assistant',
    'body': 'Please let Alex know that the sample is ready for pickup. No reply is needed.',
}


class Preview(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, content, kind='application/json; charset=utf-8', status=200):
        if not isinstance(content, bytes):
            content = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        self.send(json.dumps({'detail': 'Read-only documentation preview.'}), status=405)

    do_PUT = do_POST
    do_DELETE = do_POST

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in ('/', '/live', '/live/'):
            html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
            html = html.replace('{{csrf}}', 'documentation-demo')
            banner = '<aside style="text-align:center;padding:10px;background:#173c36;color:#effaf0;font:12px system-ui">Documentation demo · Fictional data · No phone connection</aside>'
            self.send(html.replace('<body>', '<body>' + banner), 'text/html; charset=utf-8')
        elif url.path in ('/live/app.js', '/live/style.css'):
            path = ROOT / 'app/web' / url.path.rsplit('/', 1)[-1]
            kind = 'text/javascript' if path.suffix == '.js' else 'text/css'
            self.send(path.read_bytes(), kind + '; charset=utf-8')
        elif url.path == '/live/bootstrap':
            # The referrer selects a fixture, never a real record or service.
            inbox = parse_qs(urlsplit(self.headers.get('Referer', '')).query).get('view') == ['inbox']
            data = {
                'calls': [CALL], 'call': CALL, 'cursor': 0, 'caller_book': [],
                'inbox': {'unread_count': int(inbox), 'messages': [MESSAGE] if inbox else [], 'next_cursor': None},
                'concierge': {'enabled': False}, 'requests': [],
                'owner_contact_enabled': False,
                'voice': {'voice': 'marin', 'voices': [{'id': 'marin', 'label': 'Marin'}]},
            }
            self.send(json.dumps(data))
        elif url.path == '/live/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(b'event: connected\ndata: {}\n\n')
                self.wfile.flush()
                while True:
                    time.sleep(5)
                    self.wfile.write(b'event: heartbeat\ndata: {}\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        else:
            self.send(json.dumps({'detail': 'Not part of the documentation preview.'}), status=404)


if __name__ == '__main__':
    print('Fictional read-only monitor: http://127.0.0.1:8765/live', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), Preview).serve_forever()
