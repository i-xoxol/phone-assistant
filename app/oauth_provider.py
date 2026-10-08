"""Small single-owner OAuth 2.1 provider for the remote MCP endpoint.

Authorization requires a short-lived pairing code generated locally on the
the service host.  ChatGPT receives ordinary rotating access and refresh tokens; neither
the pairing code nor the Microsoft Graph token is embedded in the plugin.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 90 * 24 * 60 * 60
AUTH_REQUEST_SECONDS = 10 * 60
AUTH_CODE_SECONDS = 5 * 60
PAIRING_CODE_SECONDS = 15 * 60


def _now() -> int:
    return int(time.time())


def _opaque_token() -> str:
    return secrets.token_urlsafe(32)


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _pairing_hash(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


class PairingOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """SQLite-backed OAuth provider intended for one private MCP owner."""

    def __init__(self, database: str | Path, public_base_url: str):
        self.database = Path(database).expanduser()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_auth_requests (
                    request_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    token_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                    token_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_pairing_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    salt BLOB NOT NULL,
                    code_hash BLOB NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                """
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT payload FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["payload"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("OAuth client has no client_id")
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO oauth_clients(client_id, payload) VALUES (?, ?)",
                (client_info.client_id, client_info.model_dump_json()),
            )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != f"{self.public_base_url}/mcp":
            raise ValueError('OAuth resource must match this phone service')
        callback = urlsplit(str(params.redirect_uri))
        if not ((callback.scheme == 'https' and callback.hostname == 'chatgpt.com') or
                (callback.scheme == 'http' and callback.hostname in ('localhost', '127.0.0.1'))):
            raise ValueError('Only ChatGPT and local Codex callbacks can pair with this private service')
        request_key = _opaque_token()
        payload = {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "params": params.model_dump(mode="json"),
        }
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO oauth_auth_requests(request_key, payload, expires_at) VALUES (?, ?, ?)",
                (request_key, json.dumps(payload), _now() + AUTH_REQUEST_SECONDS),
            )
        return f"{self.public_base_url}/approve?{urlencode({'request': request_key})}"

    def approval_details(self, request_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT payload, expires_at FROM oauth_auth_requests WHERE request_key = ?",
                (request_key,),
            ).fetchone()
        if not row or row["expires_at"] < _now():
            return None
        return json.loads(row["payload"])

    def approve(self, request_key: str, pairing_code: str) -> str:
        details = self.approval_details(request_key)
        if not details:
            raise ValueError("This authorization request has expired. Start the connection again.")
        if not self._consume_pairing_code(pairing_code):
            raise ValueError("The pairing code is invalid, expired, or already used.")

        params = AuthorizationParams.model_validate(details["params"])
        code = _opaque_token()
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or ["phone.calls"],
            expires_at=_now() + AUTH_CODE_SECONDS,
            client_id=details["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="phone-agent-owner",
        )
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM oauth_auth_requests WHERE request_key = ?", (request_key,))
            db.execute(
                "INSERT INTO oauth_codes(token_key, payload, expires_at) VALUES (?, ?, ?)",
                (_token_key(code), auth_code.model_dump_json(), int(auth_code.expires_at)),
            )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, request_key: str) -> str:
        details = self.approval_details(request_key)
        if not details:
            raise ValueError("This authorization request has expired.")
        params = AuthorizationParams.model_validate(details["params"])
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM oauth_auth_requests WHERE request_key = ?", (request_key,))
        return construct_redirect_uri(
            str(params.redirect_uri), error="access_denied", state=params.state
        )

    def create_pairing_code(self, ttl_seconds: int = PAIRING_CODE_SECONDS) -> str:
        # Grouped for transcription while retaining substantially more entropy than
        # a conventional six-digit OTP.
        raw = secrets.token_hex(8).upper()
        code = "-".join(raw[index : index + 4] for index in range(0, len(raw), 4))
        salt = secrets.token_bytes(16)
        digest = _pairing_hash(code, salt)
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM oauth_pairing_codes WHERE expires_at < ? OR used_at IS NOT NULL", (_now(),))
            db.execute(
                "INSERT INTO oauth_pairing_codes(salt, code_hash, expires_at) VALUES (?, ?, ?)",
                (salt, digest, _now() + ttl_seconds),
            )
        return code

    def _consume_pairing_code(self, code: str) -> bool:
        normalized = code.strip().upper()
        with closing(self._connect()) as db, db:
            rows = db.execute(
                "SELECT id, salt, code_hash FROM oauth_pairing_codes "
                "WHERE used_at IS NULL AND expires_at >= ?",
                (_now(),),
            ).fetchall()
            match_id = None
            for row in rows:
                candidate = _pairing_hash(normalized, row["salt"])
                if hmac.compare_digest(candidate, row["code_hash"]):
                    match_id = row["id"]
                    break
            if match_id is None:
                return False
            changed = db.execute(
                "UPDATE oauth_pairing_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (_now(), match_id),
            ).rowcount
        return changed == 1

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._load_token("oauth_codes", authorization_code, AuthorizationCode)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with closing(self._connect()) as db, db:
            deleted = db.execute(
                "DELETE FROM oauth_codes WHERE token_key = ?", (_token_key(authorization_code.code),)
            ).rowcount
        if deleted != 1:
            raise TokenError("invalid_grant", "authorization code was already used")
        return self._issue_tokens(client.client_id or "", authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self._load_token("oauth_refresh_tokens", refresh_token, RefreshToken)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with closing(self._connect()) as db, db:
            deleted = db.execute(
                "DELETE FROM oauth_refresh_tokens WHERE token_key = ?", (_token_key(refresh_token.token),)
            ).rowcount
        if deleted != 1:
            raise TokenError("invalid_grant", "refresh token was already used")
        if refresh_token.client_id != client.client_id or not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_grant", "refresh token client or scope mismatch")
        return self._issue_tokens(client.client_id or "", scopes, f"{self.public_base_url}/mcp")

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self._load_token("oauth_access_tokens", token, AccessToken)
        if access and access.resource == f"{self.public_base_url}/mcp" and 'phone.calls' in access.scopes:
            return access
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with closing(self._connect()) as db, db:
            # Disconnect the whole client grant, including its webhook subscriptions.
            for table in ('oauth_access_tokens', 'oauth_refresh_tokens'):
                db.execute(f"DELETE FROM {table} WHERE json_extract(payload,'$.client_id')=?",
                           (token.client_id,))

    def connection_active(self, client_id: str) -> bool:
        with closing(self._connect()) as db:
            for table in ('oauth_access_tokens', 'oauth_refresh_tokens'):
                rows = db.execute(f"SELECT payload FROM {table} WHERE expires_at>? "
                                  "AND json_extract(payload,'$.client_id')=?", (_now(), client_id))
                if any('phone.calls' in json.loads(r['payload'])['scopes'] for r in rows):
                    return True
        return False

    def _load_token(self, table: str, token: str, model: Any) -> Any | None:
        if table not in {"oauth_codes", "oauth_access_tokens", "oauth_refresh_tokens"}:
            raise ValueError("invalid token table")
        with closing(self._connect()) as db, db:
            row = db.execute(
                f"SELECT payload, expires_at FROM {table} WHERE token_key = ?", (_token_key(token),)
            ).fetchone()
        if not row or row["expires_at"] < _now():
            return None
        return model.model_validate_json(row["payload"])

    def _issue_tokens(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access_value = _opaque_token()
        refresh_value = _opaque_token()
        access = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=_now() + ACCESS_TOKEN_SECONDS,
            resource=resource,
            subject="phone-agent-owner",
        )
        refresh = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=_now() + REFRESH_TOKEN_SECONDS,
            subject="phone-agent-owner",
        )
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO oauth_access_tokens(token_key, payload, expires_at) VALUES (?, ?, ?)",
                (_token_key(access_value), access.model_dump_json(), access.expires_at),
            )
            db.execute(
                "INSERT INTO oauth_refresh_tokens(token_key, payload, expires_at) VALUES (?, ?, ?)",
                (_token_key(refresh_value), refresh.model_dump_json(), refresh.expires_at),
            )
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )
