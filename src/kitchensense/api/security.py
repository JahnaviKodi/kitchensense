"""Turning an ``Authorization`` header into a validated principal.

Every refusal leaves by the same door: :func:`unauthorized`, which builds the
``WWW-Authenticate`` challenge RFC 6750 asks for. Nothing else in the API
raises a 401, so there is one place to read to know what the API tells an
unauthenticated caller.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from kitchensense.auth.errors import (
    AuthConfigurationError,
    MissingTokenError,
    TokenError,
)
from kitchensense.auth.principal import Principal
from kitchensense.auth.verifier import TokenVerifier
from kitchensense.config import Settings

__all__ = [
    "PrincipalDep",
    "ensure_verifier",
    "get_principal",
    "get_verifier",
    "unauthorized",
]

logger = logging.getLogger(__name__)

REALM = "kitchensense"

# auto_error=False so an absent header reaches our own code. FastAPI's built-in
# refusal is a 403 with no WWW-Authenticate, which is the wrong status and a
# missing header.
bearer_scheme = HTTPBearer(auto_error=False, description="Entra External ID access token")


def unauthorized(error: TokenError) -> HTTPException:
    """A 401 carrying the challenge a client needs to act on.

    Note that an insufficient scope also comes back as 401. RFC 6750 §3.1
    prescribes 403 for that case — the credential is good, the permission is
    not — and this deliberately does not follow it, because a single status for
    every refusal was asked for. The distinction survives in the
    ``error="insufficient_scope"`` code, which is where a client should be
    reading it from anyway.
    """
    parts = [f'realm="{REALM}"']
    if error.error_code:
        parts.append(f'error="{error.error_code}"')
        parts.append(f'error_description="{_quotable(error.description)}"')

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=error.description,
        headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)},
    )


def _quotable(text: str) -> str:
    """Header values cannot carry a quote, a backslash or a newline."""
    return text.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()


def ensure_verifier(app: FastAPI) -> TokenVerifier:
    """Attach a token verifier to the app, once.

    Built on demand for the same reason the database handle is: an ASGI
    transport that skips the lifespan still has to find one. Constructing it
    opens no connection and fetches no metadata.
    """
    verifier: TokenVerifier | None = getattr(app.state, "verifier", None)
    if verifier is None:
        verifier = TokenVerifier(Settings.from_env())
        app.state.verifier = verifier
    return verifier


def get_verifier(request: Request) -> TokenVerifier:
    return ensure_verifier(request.app)


VerifierDep = Annotated[TokenVerifier, Depends(get_verifier)]
CredentialsDep = Annotated[
    HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
]


async def get_principal(
    verifier: VerifierDep, credentials: CredentialsDep
) -> Principal:
    """Validate the bearer token, or refuse the request.

    Raises:
        HTTPException: 401 for anything wrong with the token; 503 if the
            server has no tenant configured, which is not the caller's fault
            and should not send them looking at their credentials.
    """
    # Checked before the header is even looked at. A deployment with no tenant
    # cannot accept anyone's token, so whether one was presented makes no
    # difference to the answer — and answering 401 to the caller who sent
    # nothing while answering 503 to the caller who sent something would be an
    # incoherent pair of replies to the same broken server.
    try:
        verifier.require_configured()
    except AuthConfigurationError as exc:
        logger.error("Refusing a request: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this deployment.",
        ) from exc

    if credentials is None or not credentials.credentials:
        raise unauthorized(MissingTokenError("this endpoint requires an access token"))

    try:
        return await verifier.verify(credentials.credentials)
    except TokenError as exc:
        raise unauthorized(exc) from exc


PrincipalDep = Annotated[Principal, Depends(get_principal)]
