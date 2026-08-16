"""Why a token was refused.

Each error carries the RFC 6750 ``error`` code that belongs in the
``WWW-Authenticate`` header, and a description safe to hand back to the
caller. Descriptions are written for whoever is debugging a client, and say
what was wrong without echoing the token or anything from inside it.
"""

from __future__ import annotations

__all__ = [
    "AuthConfigurationError",
    "InsufficientScopeError",
    "InvalidTokenError",
    "MissingTokenError",
    "TokenError",
]


class TokenError(Exception):
    """Base class for a refused request."""

    error_code = "invalid_request"

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


class MissingTokenError(TokenError):
    """No bearer token was presented.

    Distinct from a bad one: RFC 6750 says a challenge for an *absent*
    credential carries no ``error`` code, because nothing was wrong with what
    the client sent — it simply has not authenticated yet.
    """

    error_code = ""


class InvalidTokenError(TokenError):
    """The token was present but did not survive validation.

    Signature, issuer, audience, expiry, not-before, malformed structure, an
    unknown signing key — all of it lands here, on purpose. Telling a caller
    *which* check failed tells an attacker which one to work on next.
    """

    error_code = "invalid_token"


class InsufficientScopeError(TokenError):
    """A valid token that is not permitted to do this."""

    error_code = "insufficient_scope"


class AuthConfigurationError(Exception):
    """The server cannot validate tokens at all.

    Not a client error. Raised when the tenant, client id or audience is
    missing from the environment, and answered with a 503 rather than a 401 —
    a 401 would tell the caller to go and fix a credential that was never the
    problem.
    """
