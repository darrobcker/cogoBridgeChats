"""Sign-in (PROTOCOL.md §5): the MCP SDK's OAuth authorization server, kept in the store.

The SDK checks the protocol — PKCE, redirect addresses, client secrets — and this answers its questions from
`core`, where the rules are. The Allow page it sends people to is in `web`. A connection is identified by nothing
but its tokens: no email, no password, no name.
"""
from __future__ import annotations

from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import core
from .store import Store

SCOPE = "chats"


def _safe_redirect(uri: str) -> bool:
    """https, or an app on the person's own machine: OAuth 2.1 allows nothing else."""
    parsed = urlparse(uri)
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1",
                                                                                        "::1"))


class Provider:
    def __init__(self, store: Store, base_url: str):
        self.store, self.base = store, base_url.rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = core.client(self.store, client_id)
        return OAuthClientInformationFull.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not all(_safe_redirect(str(uri)) for uri in client_info.redirect_uris or []):
            raise RegistrationError("invalid_redirect_uri", "redirect addresses must be https, or on this machine")
        try:
            core.register_client(self.store, client_info.client_id, client_info.model_dump_json())
        except core.Refused as refused:
            raise RegistrationError("invalid_client_metadata", str(refused)) from refused

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request = core.sign_in_request(self.store, {
            "client_id": client.client_id, "redirect_uri": str(params.redirect_uri),
            "explicit": params.redirect_uri_provided_explicitly, "challenge": params.code_challenge,
            "state": params.state, "scopes": params.scopes or [], "resource": params.resource})
        return f"{self.base}/allow?r={request}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, code: str) -> AuthorizationCode | None:
        found = core.authorization_code(self.store, code)
        if not found or found["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=code, scopes=found["scopes"], expires_at=found["expires_t"],
                                 client_id=found["client_id"], code_challenge=found["challenge"],
                                 redirect_uri=found["redirect_uri"], redirect_uri_provided_explicitly=found["explicit"],
                                 resource=found["resource"], subject=found["grant"])

    def _tokens(self, pair: tuple[str, str] | None, scopes: list[str]) -> OAuthToken:
        if not pair:
            raise TokenError("invalid_grant", "that code or token is no longer valid")
        return OAuthToken(access_token=pair[0], token_type="Bearer", expires_in=core.ACCESS_S, refresh_token=pair[1],
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        return self._tokens(core.exchange_code(self.store, authorization_code.code), authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        found = core.renewal(self.store, refresh_token)
        if not found or found["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=found["client_id"], scopes=found.get("scopes") or [],
                            subject=found["grant"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        return self._tokens(core.renew(self.store, refresh_token.token), refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = core.access(self.store, token)
        if not found:
            return None
        return AccessToken(token=token, client_id=found["client_id"], scopes=found.get("scopes") or [],
                           expires_at=int(found["expires_t"]), subject=found["grant"], resource=f"{self.base}/mcp")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        core.revoke(self.store, token.token)
