"""Runtime configuration, read from the environment.

The Azure-specific values come from ``infra/main.bicep``, which derives them
from the resources it deploys. Nothing here is a secret — vault URIs, tenant
and client ids are all public identifiers — but none of them is hardcoded
either, so staging points at staging.

The tenant settings have **no defaults**. A blank one means authentication is
not configured, and the API refuses to serve protected endpoints rather than
falling back to something. There is no value that could stand in for "which
directory issues our tokens" that would not be a way of accepting the wrong
ones.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Settings"]

# Production values, overridden by environment variables the Container App
# sets. Identifiers, not secrets: the vault URI is public, and the client id
# names an identity only Azure can present.
DEFAULT_KEY_VAULT_URI = "https://kv-ks-feq2gmz4zaoa4.vault.azure.net/"
DEFAULT_MANAGED_IDENTITY_CLIENT_ID = "ec5d34e8-9b33-468b-b0f3-07336fe3a866"
DEFAULT_POSTGRES_SECRET_NAME = "postgres-connection-string"

# The permission a caller needs to use the kitchen record. Not tenant
# configuration — it is the name this API gives its own scope, and it appears
# in the app registration.
DEFAULT_REQUIRED_SCOPE = "inventory.readwrite"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str

    # --- database ---
    key_vault_uri: str
    managed_identity_client_id: str
    postgres_secret_name: str
    # A stopped PostgreSQL server does not refuse connections, it drops them,
    # so an unbounded connect would hang the health probe rather than answer
    # it. Short enough to keep /health/deep honest about being a probe.
    database_probe_timeout_seconds: float
    # Reading the secret is a network round trip to Key Vault, bounded for the
    # same reason.
    key_vault_timeout_seconds: float
    # How long to stop asking after a failed lookup. The SDK retries
    # internally, so a failure costs seconds and a worker thread; without a
    # cooldown, every request during a slow role-assignment propagation pays
    # that again. The cost is that recovery is noticed up to this late.
    key_vault_retry_cooldown_seconds: float

    # --- identity ---
    entra_tenant_id: str
    entra_client_id: str
    entra_audience: str
    entra_openid_configuration_url: str
    required_scope: str
    # How long a fetched key set is trusted before being refetched. A rotation
    # is normally noticed sooner than this, by way of an unfamiliar key id.
    jwks_cache_seconds: float
    # The floor between two forced refreshes. Without it, tokens carrying
    # invented key ids would turn into one outbound request each.
    jwks_min_refresh_seconds: float
    # Clock skew allowance on exp and nbf. Entra recommends about five
    # minutes; this is tighter, and tighter is the safer direction.
    token_leeway_seconds: float
    identity_timeout_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        tenant_id = _env("ENTRA_TENANT_ID")
        return cls(
            app_env=_env("APP_ENV", "local"),
            key_vault_uri=_env("KEY_VAULT_URI", DEFAULT_KEY_VAULT_URI),
            managed_identity_client_id=_env(
                "AZURE_CLIENT_ID", DEFAULT_MANAGED_IDENTITY_CLIENT_ID
            ),
            postgres_secret_name=_env(
                "POSTGRES_SECRET_NAME", DEFAULT_POSTGRES_SECRET_NAME
            ),
            database_probe_timeout_seconds=float(
                _env("DATABASE_PROBE_TIMEOUT_SECONDS", "5")
            ),
            key_vault_timeout_seconds=float(_env("KEY_VAULT_TIMEOUT_SECONDS", "10")),
            key_vault_retry_cooldown_seconds=float(
                _env("KEY_VAULT_RETRY_COOLDOWN_SECONDS", "30")
            ),
            entra_tenant_id=tenant_id,
            entra_client_id=_env("ENTRA_CLIENT_ID"),
            # Separately configurable even though it is the client id today.
            # An API that later accepts tokens minted for an Application ID
            # URI changes its audience without changing its identity.
            entra_audience=_env("ENTRA_AUDIENCE") or _env("ENTRA_CLIENT_ID"),
            entra_openid_configuration_url=_env("ENTRA_OPENID_CONFIGURATION_URL")
            or openid_configuration_url(tenant_id),
            required_scope=_env("REQUIRED_SCOPE", DEFAULT_REQUIRED_SCOPE),
            jwks_cache_seconds=float(_env("JWKS_CACHE_SECONDS", "3600")),
            jwks_min_refresh_seconds=float(_env("JWKS_MIN_REFRESH_SECONDS", "60")),
            token_leeway_seconds=float(_env("TOKEN_LEEWAY_SECONDS", "60")),
            identity_timeout_seconds=float(_env("IDENTITY_TIMEOUT_SECONDS", "10")),
        )


def openid_configuration_url(tenant_id: str) -> str:
    """Where to discover the issuer and signing keys for an External ID tenant.

    External ID tenants live on ``ciamlogin.com``, not the ``login.
    microsoftonline.com`` host a workforce tenant uses — pointing at the latter
    yields metadata for a directory that does not contain these users, and
    every token fails on the issuer. The tenant GUID is accepted in place of
    the tenant subdomain, so the id alone is enough to build this.

    Returns an empty string for an empty tenant, so an unconfigured deployment
    fails the configuration check rather than fetching a nonsense URL.
    """
    if not tenant_id:
        return ""
    return (
        f"https://{tenant_id}.ciamlogin.com/{tenant_id}/v2.0"
        "/.well-known/openid-configuration"
    )
