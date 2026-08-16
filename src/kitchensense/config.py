"""Runtime configuration, read from the environment.

Everything here has a default that works, so nothing has to be set to run the
app locally. The Container App overrides the Azure-specific values from
``infra/main.bicep``, which is what keeps staging pointed at staging's vault
rather than at the defaults below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

__all__ = [
    "PLACEHOLDER_HOUSEHOLD_ID",
    "Settings",
]

# TODO(auth): this is a stand-in. Once Entra External ID is wired up, the
# household comes from the signed-in user's token — a claim on the validated
# JWT, resolved per request — and this constant goes away along with the
# `get_household_id` dependency that returns it. Until then every request is
# treated as coming from the same household, so the deployment holds exactly
# one kitchen's data and must not be given anyone else's.
#
# The repository layer is already built for the real thing: household_id is a
# required keyword argument on every method, so the only change needed here is
# where the value comes from, not how it is threaded through.
PLACEHOLDER_HOUSEHOLD_ID = UUID("11111111-1111-4111-8111-111111111111")

# Production values, overridden by environment variables the Container App
# sets. They are identifiers, not secrets: the vault URI is public, and the
# client id names an identity that only Azure can present.
DEFAULT_KEY_VAULT_URI = "https://kv-ks-feq2gmz4zaoa4.vault.azure.net/"
DEFAULT_MANAGED_IDENTITY_CLIENT_ID = "ec5d34e8-9b33-468b-b0f3-07336fe3a866"
DEFAULT_POSTGRES_SECRET_NAME = "postgres-connection-string"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
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

    @classmethod
    def from_env(cls) -> Settings:
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
        )
