"""Validating an Entra External ID access token.

Every check that matters happens here, and a :class:`Principal` only exists
once they have all passed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt

from kitchensense.auth.errors import (
    AuthConfigurationError,
    InsufficientScopeError,
    InvalidTokenError,
)
from kitchensense.auth.jwks import KeySource
from kitchensense.auth.principal import Principal
from kitchensense.config import Settings

__all__ = ["TokenVerifier"]

logger = logging.getLogger(__name__)

# Pinned rather than read from the token header. Accepting whatever a token
# asks for is the classic algorithm-confusion hole: an attacker re-signs with
# HS256 using the *public* key as the HMAC secret, and a verifier that obliges
# accepts it. Entra signs with RS256.
ALLOWED_ALGORITHMS = ["RS256"]

# Claims a token must carry. "sub" is on the list because the household is
# derived from it, so a token without one has nowhere to put its data.
REQUIRED_CLAIMS = ["exp", "nbf", "iss", "aud", "sub"]


class TokenVerifier:
    """Verifies bearer tokens against the configured External ID tenant."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # The transport is injectable so tests can serve the tenant's metadata
        # from memory. In production it is None and httpx does the usual thing.
        self._client = httpx.AsyncClient(
            transport=transport, timeout=settings.identity_timeout_seconds
        )
        self._keys = KeySource(
            discovery_url=settings.entra_openid_configuration_url,
            tenant_id=settings.entra_tenant_id,
            client=self._client,
            cache_seconds=settings.jwks_cache_seconds,
            min_refresh_seconds=settings.jwks_min_refresh_seconds,
        )

    @property
    def settings(self) -> Settings:
        return self._settings

    def require_configured(self) -> None:
        """Refuse to run at all without a tenant to validate against.

        Called before any token is inspected. Without this, an unconfigured
        deployment would reject every token as invalid and look, from the
        outside, exactly like a client with bad credentials.
        """
        missing = [
            name
            for name, value in (
                ("ENTRA_TENANT_ID", self._settings.entra_tenant_id),
                ("ENTRA_CLIENT_ID", self._settings.entra_client_id),
                ("ENTRA_AUDIENCE", self._settings.entra_audience),
            )
            if not value
        ]
        if missing:
            raise AuthConfigurationError(
                "authentication is not configured: " + ", ".join(missing) + " unset"
            )

    async def verify(self, token: str) -> Principal:
        """Validate a token and describe who presented it.

        Raises:
            AuthConfigurationError: the server has no tenant configured.
            InvalidTokenError: the token failed any structural or
                cryptographic check.
            InsufficientScopeError: valid, but not permitted here.
        """
        self.require_configured()

        if not token or not token.strip():
            raise InvalidTokenError("no token was presented")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("the token is not a well-formed JWT") from exc

        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            # Checked before the key is fetched: no point asking Entra for a
            # key to verify a signature we would refuse anyway.
            raise InvalidTokenError(f"unsupported signing algorithm {algorithm!r}")

        signing_key = await self._keys.signing_key(header.get("kid", ""))
        configuration = await self._keys.configuration()

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._settings.entra_audience,
                issuer=configuration.issuer,
                leeway=self._settings.token_leeway_seconds,
                options={
                    "require": REQUIRED_CLAIMS,
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": False,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.PyJWTError as exc:
            # One message for every failure mode. Distinguishing "expired"
            # from "wrong audience" from "bad signature" would tell whoever is
            # probing which knob to turn next; the specific reason goes to the
            # logs instead.
            logger.info("Rejected a token: %s", type(exc).__name__)
            raise InvalidTokenError("the token is not valid") from exc

        principal = self._to_principal(claims)
        self._require_scope(principal)
        return principal

    def _to_principal(self, claims: dict[str, Any]) -> Principal:
        subject = str(claims.get("sub") or "").strip()
        issuer = str(claims.get("iss") or "").strip()
        if not subject or not issuer:
            raise InvalidTokenError("the token has no subject")

        return Principal(
            subject=subject,
            issuer=issuer,
            audience=self._settings.entra_audience,
            scopes=_scopes(claims),
            expires_at=datetime.fromtimestamp(float(claims["exp"]), tz=UTC),
            name=_optional(claims.get("name")),
            email=_optional(claims.get("preferred_username") or claims.get("email")),
        )

    def _require_scope(self, principal: Principal) -> None:
        required = self._settings.required_scope
        if not principal.has_scope(required):
            raise InsufficientScopeError(
                f"the token does not carry the {required!r} scope"
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    """The delegated permissions on the token.

    Entra puts them in ``scp`` as one space-separated string. ``roles`` — the
    application-permission equivalent, issued for client-credentials tokens
    with no user behind them — is deliberately not read: every household here
    is a person, and a token with no subject has no kitchen to look at.
    """
    raw = claims.get("scp")
    if isinstance(raw, str):
        return frozenset(part for part in raw.split(" ") if part)
    if isinstance(raw, list):
        return frozenset(str(part) for part in raw if part)
    return frozenset()


def _optional(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
