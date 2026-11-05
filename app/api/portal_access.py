"""Separate portal origins/cookies; never trust caller-supplied forwarding headers."""
from dataclasses import dataclass
from urllib.parse import urlsplit
from fastapi import HTTPException

PUBLIC_COOKIE = '__Host-phone-owner'
PRIVATE_COOKIE = 'phone-private-owner'


@dataclass(frozen=True)
class PortalAccess:
    origin: str
    cookie: str
    secure: bool
    private: bool = False


def portal_access(request):
    settings = request.app.state.settings
    private = settings.private_portal_origin
    if private and request.headers.get('host', '').lower() == urlsplit(private).netloc.lower():
        peer = request.client.host if request.client else ''
        if peer not in settings.private_portal_trusted_peers:
            raise HTTPException(403, 'Private portal requires the configured private connection')
        return PortalAccess(private, PRIVATE_COOKIE, urlsplit(private).scheme == 'https', True)
    return PortalAccess(settings.public_base_url, PUBLIC_COOKIE, True)
