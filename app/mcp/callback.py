"""Standard Webhooks signing and HTTPS callbacks pinned to public IP addresses."""
import asyncio
import base64
import binascii
import http.client
import ipaddress
import json
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

from standardwebhooks.webhooks import Webhook

MAX_BYTES = 262144


class CallbackError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def validate_secret(secret):
    try:
        if not secret.startswith('whsec_'):
            raise ValueError()
        key = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError()
    except (ValueError, binascii.Error, AttributeError):
        raise ValueError('Signing secret must be whsec_ followed by base64 of 24–64 bytes') from None
    return secret


def callback_url(url):
    try:
        parts = urlsplit(url)
        url.encode('ascii')
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.fragment or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            raise ValueError()
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
    except (ValueError, UnicodeError, AttributeError):
        raise CallbackError('invalid_url') from None
    return parts


def signed_headers(subscription_id, event_id, secret, body, *, previous_secret=None):
    stamp = datetime.now(timezone.utc)
    signature = Webhook(secret).sign(event_id, stamp, body.decode('utf-8'))
    if previous_secret:
        signature += ' ' + Webhook(previous_secret).sign(event_id, stamp, body.decode('utf-8'))
    return {'Content-Type': 'application/json', 'webhook-id': event_id,
            'webhook-timestamp': str(int(stamp.timestamp())), 'webhook-signature': signature,
            'X-MCP-Subscription-Id': subscription_id}


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once at connection time; never ask HTTP/TLS to resolve the host again.
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise CallbackError('blocked_address')
        sock = socket.create_connection(addresses[0][4][:2], timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _post(url, body, headers):
    parts = callback_url(url)
    connection = PublicHTTPSConnection(parts.hostname, parts.port or 443, timeout=8,
                                       context=ssl.create_default_context())
    try:
        path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        connection.request('POST', path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise CallbackError('oversized_response')
        # http.client never follows redirects, including redirects to internal addresses.
        return response.status, payload
    finally:
        connection.close()


async def post_callback(url, body, headers):
    if len(body) > MAX_BYTES:
        raise CallbackError('oversized_payload')
    try:
        return await asyncio.wait_for(asyncio.to_thread(_post, url, body, headers), 10)
    except (TimeoutError, socket.timeout):
        raise CallbackError('timeout') from None
    except CallbackError:
        raise
    except (OSError, http.client.HTTPException):
        raise CallbackError('connection_failed') from None


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
