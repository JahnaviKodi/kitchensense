"""Discovering the tenant's issuer and signing keys, and caching them.

Nothing about the issuer or the key set is configured. Both are read from the
tenant's OpenID configuration document, which is the only place they are
authoritative — Entra rotates signing keys on its own schedule, and an issuer
pinned by hand is a value that can drift out of agreement with the tokens
being issued.

Caching is the whole difficulty here. Fetching the key set per request would
put an outbound HTTPS call in front of every API call; never refetching would
break the moment a key rotated. So: a time-to-live for the ordinary case, plus
an out-of-band refresh when a token arrives signed by a key id we have not
seen — which is exactly what a rotation looks like from here — rate-limited so
that a flood of tokens bearing invented key ids cannot turn into a flood of
requests at Entra.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jwt import PyJWK

from kitchensense.auth.errors import InvalidTokenError

__all__ = ["KeySource", "OpenIDConfiguration"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenIDConfiguration:
    issuer: str
    jwks_uri: str


class KeySource:
    """Fetches and caches the tenant's OpenID configuration and signing keys.

    The HTTP client is injected so tests can serve both documents from an
    in-memory transport. Nothing here reaches the network unless asked to.
    """

    def __init__(
        self,
        *,
        discovery_url: str,
        tenant_id: str,
        client: httpx.AsyncClient,
        cache_seconds: float = 3600.0,
        min_refresh_seconds: float = 60.0,
    ) -> None:
        self._discovery_url = discovery_url
        self._tenant_id = tenant_id
        self._client = client
        self._cache_seconds = cache_seconds
        self._min_refresh_seconds = min_refresh_seconds

        self._lock = asyncio.Lock()
        self._configuration: OpenIDConfiguration | None = None
        self._keys: dict[str, PyJWK] = {}
        self._keys_fetched_at: float = 0.0

    async def configuration(self) -> OpenIDConfiguration:
        if self._configuration is not None:
            return self._configuration
        async with self._lock:
            if self._configuration is None:
                self._configuration = await self._fetch_configuration()
            return self._configuration

    async def signing_key(self, kid: str) -> PyJWK:
        """The key with this id, refetching once if it is unknown.

        An unfamiliar key id is the normal appearance of a key rotation, so it
        triggers a refresh rather than a rejection. The refresh is rate-limited
        because it is also what a forged token with a made-up ``kid`` looks
        like, and that must not become an amplification lever pointed at the
        tenant's metadata endpoint.
        """
        if not kid:
            raise InvalidTokenError("the token header carries no key id")

        key = await self._cached_key(kid)
        if key is not None:
            return key

        async with self._lock:
            # Another request may have refreshed while this one waited.
            key = self._keys.get(kid)
            if key is not None:
                return key

            if time.monotonic() - self._keys_fetched_at < self._min_refresh_seconds:
                logger.warning(
                    "Token presented an unknown key id and the key set was "
                    "refreshed too recently to try again"
                )
                raise InvalidTokenError("the token was signed by an unknown key")

            logger.info("Unknown key id; refreshing the signing keys")
            await self._refresh_keys()
            key = self._keys.get(kid)

        if key is None:
            raise InvalidTokenError("the token was signed by an unknown key")
        return key

    async def _cached_key(self, kid: str) -> PyJWK | None:
        expired = time.monotonic() - self._keys_fetched_at >= self._cache_seconds
        if self._keys and not expired:
            return self._keys.get(kid)

        async with self._lock:
            still_expired = (
                time.monotonic() - self._keys_fetched_at >= self._cache_seconds
            )
            if not self._keys or still_expired:
                await self._refresh_keys()
            return self._keys.get(kid)

    async def _refresh_keys(self) -> None:
        """Replace the cached key set. Caller holds the lock."""
        if self._configuration is None:
            self._configuration = await self._fetch_configuration()

        document = await self._get_json(self._configuration.jwks_uri)
        keys: dict[str, PyJWK] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = PyJWK.from_dict(entry)
            except Exception:
                # One unusable key — an unsupported curve, say — must not cost
                # us the rest of the set.
                logger.warning("Skipping an unusable JWK with kid %r", kid)

        if not keys:
            raise InvalidTokenError("the tenant published no usable signing keys")

        self._keys = keys
        self._keys_fetched_at = time.monotonic()
        logger.info("Cached %d signing key(s)", len(keys))

    async def _fetch_configuration(self) -> OpenIDConfiguration:
        document = await self._get_json(self._discovery_url)

        issuer = str(document.get("issuer") or "").strip()
        jwks_uri = str(document.get("jwks_uri") or "").strip()
        if not issuer or not jwks_uri:
            raise InvalidTokenError(
                "the tenant's OpenID configuration is missing an issuer or jwks_uri"
            )

        # Entra's multi-tenant metadata templates the issuer as
        # ".../{tenantid}/v2.0". Left as-is it would never equal the concrete
        # issuer inside a real token, and every request would fail on a
        # mismatch that looks nothing like a configuration problem.
        issuer = issuer.replace("{tenantid}", self._tenant_id)

        logger.info("Discovered issuer %s", issuer)
        return OpenIDConfiguration(issuer=issuer, jwks_uri=jwks_uri)

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except Exception as exc:
            # Presented to the caller as a refused token, because from their
            # side that is what happened. The cause goes to the logs.
            logger.warning("Could not read %s: %s", url, exc)
            raise InvalidTokenError(
                "the token could not be validated against the identity provider"
            ) from exc
        return payload
