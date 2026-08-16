"""Token validation, one refusal at a time.

Real RSA signatures, real PyJWT, real httpx — only the socket is replaced.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from kitchensense.auth.errors import (
    AuthConfigurationError,
    InsufficientScopeError,
    InvalidTokenError,
)
from kitchensense.auth.verifier import TokenVerifier
from kitchensense.domain.household import household_id_for
from tests import identity


def verifier(stub: identity.TenantStub, **overrides: object) -> TokenVerifier:
    return TokenVerifier(
        identity.settings(**overrides), transport=stub.transport
    )


def verify(token: str, stub: identity.TenantStub | None = None, **overrides: object):
    tenant = stub or identity.TenantStub()

    async def scenario():
        subject = verifier(tenant, **overrides)
        try:
            return await subject.verify(token)
        finally:
            await subject.aclose()

    return asyncio.run(scenario())


# ----------------------------------------------------------------------
# The happy path
# ----------------------------------------------------------------------


def test_a_good_token_yields_a_principal() -> None:
    principal = verify(identity.make_token(subject="alice"))

    assert principal.subject == "alice"
    assert principal.issuer == identity.ISSUER
    assert principal.has_scope(identity.REQUIRED_SCOPE)
    assert principal.name == "Test Person"


def test_the_issuer_comes_from_discovery_not_configuration() -> None:
    """Nothing pins the issuer by hand; it is read from the tenant."""
    stub = identity.TenantStub()
    verify(identity.make_token(), stub)

    assert stub.discovery_requests == 1


def test_a_principal_never_prints_its_claims() -> None:
    """This ends up in logs and tracebacks."""
    principal = verify(identity.make_token(subject="alice", name="Alice Example"))

    printed = repr(principal)
    assert "alice" not in printed
    assert "Alice Example" not in printed


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_a_token_signed_by_the_wrong_key_is_rejected() -> None:
    """A well-formed token, correct in every claim, signed by a stranger."""
    forged = identity.make_token(key=identity.impostor_key())

    with pytest.raises(InvalidTokenError):
        verify(forged)


def test_a_token_signed_by_an_unpublished_key_id_is_rejected() -> None:
    forged = identity.make_token(key=identity.impostor_key(), kid=identity.IMPOSTOR_KID)

    with pytest.raises(InvalidTokenError):
        verify(forged)


def test_an_expired_token_is_rejected() -> None:
    expired = identity.make_token(expires_in=timedelta(seconds=-30))

    with pytest.raises(InvalidTokenError):
        verify(expired)


def test_a_token_that_is_not_yet_valid_is_rejected() -> None:
    premature = identity.make_token(not_before=timedelta(hours=1))

    with pytest.raises(InvalidTokenError):
        verify(premature)


def test_a_token_for_another_audience_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        verify(identity.make_token(audience="00000000-0000-0000-0000-000000000000"))


def test_a_token_from_another_issuer_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        verify(identity.make_token(issuer="https://login.example.invalid/v2.0"))


def test_a_token_missing_the_required_scope_is_rejected() -> None:
    with pytest.raises(InsufficientScopeError):
        verify(identity.make_token(scopes="inventory.read"))


def test_a_token_with_no_scope_claim_at_all_is_rejected() -> None:
    with pytest.raises(InsufficientScopeError):
        verify(identity.make_token(scopes=None))


def test_application_roles_do_not_stand_in_for_a_scope() -> None:
    """A client-credentials token has no user, so it has no kitchen."""
    with pytest.raises(InsufficientScopeError):
        verify(
            identity.make_token(
                scopes=None, extra_claims={"roles": [identity.REQUIRED_SCOPE]}
            )
        )


@pytest.mark.parametrize("claim", ["exp", "nbf", "sub", "iss", "aud"])
def test_a_token_missing_a_required_claim_is_rejected(claim: str) -> None:
    with pytest.raises(InvalidTokenError):
        verify(identity.make_token(omit=[claim]))


@pytest.mark.parametrize("token", ["", "   ", "not-a-jwt", "a.b.c"])
def test_a_malformed_token_is_rejected(token: str) -> None:
    with pytest.raises(InvalidTokenError):
        verify(token)


def test_an_unsigned_token_is_rejected() -> None:
    """``alg: none`` is the oldest trick there is."""
    import jwt

    unsigned = jwt.encode(
        {
            "sub": "alice",
            "iss": identity.ISSUER,
            "aud": identity.AUDIENCE,
            "exp": 4102444800,
            "nbf": 0,
            "scp": identity.REQUIRED_SCOPE,
        },
        key="",
        algorithm="none",
        headers={"kid": identity.TENANT_KID},
    )

    with pytest.raises(InvalidTokenError):
        verify(unsigned)


def test_an_hmac_token_signed_with_the_public_key_is_rejected() -> None:
    """Algorithm confusion, spelled out.

    The tenant's public key is public. If the verifier honoured the header's
    ``alg`` instead of pinning it, an attacker could re-sign any claims they
    liked with HS256 using that public key as the shared secret, and the
    verifier would agree. Pinning RS256 is what stops it.
    """
    import base64
    import hashlib
    import hmac
    import json

    from cryptography.hazmat.primitives import serialization

    public_pem = (
        identity.tenant_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    def segment(payload: dict[str, object]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")

    # Assembled by hand: PyJWT refuses to *sign* with a PEM key, which is a
    # guard on the issuing side. An attacker is not using PyJWT to forge this.
    signing_input = (
        segment({"alg": "HS256", "typ": "JWT", "kid": identity.TENANT_KID})
        + b"."
        + segment(
            {
                "sub": "attacker",
                "iss": identity.ISSUER,
                "aud": identity.AUDIENCE,
                "exp": 4102444800,
                "nbf": 0,
                "scp": identity.REQUIRED_SCOPE,
            }
        )
    )
    signature = base64.urlsafe_b64encode(
        hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(InvalidTokenError):
        verify(forged)


def test_an_unconfigured_tenant_is_not_a_token_problem() -> None:
    """A 503, not a 401 — nothing was wrong with the caller's credential."""
    with pytest.raises(AuthConfigurationError):
        verify(identity.make_token(), entra_tenant_id="")


def test_a_tenant_that_cannot_be_reached_refuses_the_token() -> None:
    stub = identity.TenantStub()
    stub.discovery_status = 500

    with pytest.raises(InvalidTokenError):
        verify(identity.make_token(), stub)


# ----------------------------------------------------------------------
# Key caching
# ----------------------------------------------------------------------


def test_the_key_set_is_fetched_once_and_reused() -> None:
    stub = identity.TenantStub()

    async def scenario() -> None:
        subject = verifier(stub)
        try:
            for index in range(5):
                await subject.verify(identity.make_token(subject=f"user-{index}"))
        finally:
            await subject.aclose()

    asyncio.run(scenario())

    assert stub.jwks_requests == 1
    assert stub.discovery_requests == 1


def test_an_unknown_key_id_triggers_a_refresh() -> None:
    """What a key rotation looks like from here.

    The tenant starts out publishing one key; a token then arrives signed by a
    second one it has only just started using. Refetching is the difference
    between riding out a rotation and rejecting every token until a restart.
    """
    stub = identity.TenantStub()

    async def scenario() -> None:
        subject = verifier(stub)
        try:
            await subject.verify(identity.make_token())
            assert stub.jwks_requests == 1

            # The tenant rotates: a new key appears in the published set.
            rotated = identity.impostor_key()
            stub.jwks_document = identity.jwks(
                keys=[
                    (identity.tenant_key(), identity.TENANT_KID),
                    (rotated, "rotated-key"),
                ]
            )

            principal = await subject.verify(
                identity.make_token(key=rotated, kid="rotated-key")
            )
            assert principal.subject == "user-one"
            assert stub.jwks_requests == 2
        finally:
            await subject.aclose()

    asyncio.run(scenario())


def test_refreshes_are_rate_limited() -> None:
    """A forged key id must not become a request at the tenant, every time.

    Without a floor between forced refreshes, anyone could turn a stream of
    junk tokens into a stream of outbound requests to Entra — traffic we would
    be paying for and Entra would be rate-limiting us over.
    """
    stub = identity.TenantStub()

    async def scenario() -> None:
        subject = verifier(stub, jwks_min_refresh_seconds=300.0)
        try:
            await subject.verify(identity.make_token())
            for index in range(10):
                with pytest.raises(InvalidTokenError):
                    await subject.verify(
                        identity.make_token(
                            key=identity.impostor_key(), kid=f"invented-{index}"
                        )
                    )
        finally:
            await subject.aclose()

    asyncio.run(scenario())

    assert stub.jwks_requests == 1


def test_concurrent_first_requests_fetch_the_keys_once() -> None:
    stub = identity.TenantStub()

    async def scenario() -> None:
        subject = verifier(stub)
        try:
            await asyncio.gather(
                *(
                    subject.verify(identity.make_token(subject=f"user-{index}"))
                    for index in range(8)
                )
            )
        finally:
            await subject.aclose()

    asyncio.run(scenario())

    assert stub.jwks_requests == 1
    assert stub.discovery_requests == 1


def test_a_templated_issuer_is_resolved_against_the_tenant() -> None:
    """Entra's metadata can template the issuer as ``.../{tenantid}/v2.0``.

    Compared literally it matches no real token, and every request fails on an
    issuer mismatch that reads like a misconfiguration rather than a quirk of
    the document.
    """
    stub = identity.TenantStub(
        published_issuer="https://{tenantid}.ciamlogin.com/{tenantid}/v2.0"
    )

    principal = verify(identity.make_token(), stub)

    assert principal.issuer == identity.ISSUER


# ----------------------------------------------------------------------
# Household derivation
# ----------------------------------------------------------------------


def test_the_household_follows_from_the_subject() -> None:
    principal = verify(identity.make_token(subject="alice"))

    assert principal.household_id == household_id_for(
        issuer=identity.ISSUER, subject="alice"
    )


def test_the_same_subject_always_lands_on_the_same_household() -> None:
    first = verify(identity.make_token(subject="alice"))
    second = verify(identity.make_token(subject="alice", name="Renamed"))

    assert first.household_id == second.household_id


def test_different_subjects_land_on_different_households() -> None:
    alice = verify(identity.make_token(subject="alice"))
    bob = verify(identity.make_token(subject="bob"))

    assert alice.household_id != bob.household_id
