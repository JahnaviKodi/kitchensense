"""A fake External ID tenant, served from memory.

No test in this suite reaches the network. The tenant's OpenID configuration
and JWKS come from an ``httpx.MockTransport``, which the verifier accepts by
injection, so everything from the discovery request down through signature
verification runs its real code path against real RSA signatures — only the
socket is missing.

The signing keys are generated once per session. A 2048-bit keypair costs a
noticeable fraction of a second, and the tests need a *second* key purely to
sign a token the tenant will not vouch for.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from kitchensense.config import Settings
from kitchensense.domain.household import household_id_for

TENANT_ID = "14e3c719-bb1f-41cf-a75b-ba38e91e072d"
CLIENT_ID = "87749dc2-96a6-4eb6-a811-02400be2309c"
AUDIENCE = CLIENT_ID
ISSUER = f"https://{TENANT_ID}.ciamlogin.com/{TENANT_ID}/v2.0"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"{ISSUER}/discovery/v2.0/keys"

REQUIRED_SCOPE = "inventory.readwrite"

TENANT_KID = "test-signing-key"
IMPOSTOR_KID = "impostor-key"


@cache
def _keypair(name: str) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def tenant_key() -> rsa.RSAPrivateKey:
    """The key the fake tenant publishes and signs with."""
    return _keypair("tenant")


def impostor_key() -> rsa.RSAPrivateKey:
    """A well-formed key the tenant has never heard of."""
    return _keypair("impostor")


def _public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def jwks(*, keys: Iterable[tuple[rsa.RSAPrivateKey, str]] | None = None) -> dict[str, Any]:
    published = keys if keys is not None else [(tenant_key(), TENANT_KID)]
    return {"keys": [_public_jwk(key, kid) for key, kid in published]}


def openid_configuration() -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "jwks_uri": JWKS_URL,
        "authorization_endpoint": f"{ISSUER}/oauth2/v2.0/authorize",
        "token_endpoint": f"{ISSUER}/oauth2/v2.0/token",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def make_token(
    *,
    subject: str = "user-one",
    scopes: str | None = REQUIRED_SCOPE,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    key: rsa.RSAPrivateKey | None = None,
    kid: str = TENANT_KID,
    algorithm: str = "RS256",
    expires_in: timedelta = timedelta(hours=1),
    not_before: timedelta = timedelta(seconds=-60),
    name: str | None = "Test Person",
    extra_claims: dict[str, Any] | None = None,
    omit: Iterable[str] = (),
) -> str:
    """Mint an access token shaped like one Entra would issue."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "exp": int((now + expires_in).timestamp()),
        "nbf": int((now + not_before).timestamp()),
        "iat": int(now.timestamp()),
        "ver": "2.0",
        "tid": TENANT_ID,
    }
    if scopes is not None:
        claims["scp"] = scopes
    if name is not None:
        claims["name"] = name
    if extra_claims:
        claims.update(extra_claims)
    for claim in omit:
        claims.pop(claim, None)

    signing_key = key if key is not None else tenant_key()
    return jwt.encode(
        claims, signing_key, algorithm=algorithm, headers={"kid": kid}  # type: ignore[arg-type]
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def household_for(subject: str) -> uuid.UUID:
    """The household a given subject resolves to, derived the same way the app does."""
    return household_id_for(issuer=ISSUER, subject=subject)


class TenantStub:
    """Serves the tenant's metadata, and counts who asked for what.

    The counts are what let a test tell "the key set was cached" from "the key
    set was fetched again", which is most of what there is to check about a
    JWKS cache.
    """

    def __init__(
        self,
        *,
        jwks_document: dict[str, Any] | None = None,
        published_issuer: str | None = None,
    ) -> None:
        self.jwks_document = jwks_document if jwks_document is not None else jwks()
        # What the discovery document *says* the issuer is, which is not always
        # what a token carries — Entra can template it.
        self.published_issuer = published_issuer
        self.discovery_requests = 0
        self.jwks_requests = 0
        self.discovery_status = 200
        self.jwks_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DISCOVERY_URL:
            self.discovery_requests += 1
            document = openid_configuration()
            if self.published_issuer is not None:
                document["issuer"] = self.published_issuer
            return httpx.Response(
                self.discovery_status,
                content=json.dumps(document),
                headers={"content-type": "application/json"},
            )
        if url == JWKS_URL:
            self.jwks_requests += 1
            return httpx.Response(
                self.jwks_status,
                content=json.dumps(self.jwks_document),
                headers={"content-type": "application/json"},
            )
        # Anything else is a test reaching somewhere it did not mean to.
        return httpx.Response(404, text=f"unexpected request to {url}")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def settings(**overrides: Any) -> Settings:
    """Settings pointed at the fake tenant."""
    base = {
        "app_env": "test",
        "key_vault_uri": "https://example.invalid/",
        "managed_identity_client_id": "unused",
        "postgres_secret_name": "unused",
        "database_probe_timeout_seconds": 5.0,
        "key_vault_timeout_seconds": 10.0,
        "key_vault_retry_cooldown_seconds": 30.0,
        "entra_tenant_id": TENANT_ID,
        "entra_client_id": CLIENT_ID,
        "entra_audience": AUDIENCE,
        "entra_openid_configuration_url": DISCOVERY_URL,
        "required_scope": REQUIRED_SCOPE,
        "jwks_cache_seconds": 3600.0,
        "jwks_min_refresh_seconds": 0.0,
        "token_leeway_seconds": 0.0,
        "identity_timeout_seconds": 10.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]
