"""A signing key printed in this repository is warned about at startup.

Every placeholder passes ``min_length``, so nothing else would ever say that a
deployment copied ``.env.example`` and signs reviewer tokens with a public key.
"""

from __future__ import annotations

import logging

import pytest

from app.config import PUBLIC_JWT_SECRETS, Settings


@pytest.mark.parametrize("secret", sorted(PUBLIC_JWT_SECRETS))
def test_a_public_secret_is_warned_about(secret: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        Settings(jwt_secret=secret, _env_file=None)  # type: ignore[call-arg]
    assert "JWT_SECRET is a placeholder" in caplog.text


def test_a_private_secret_is_not(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        Settings(jwt_secret="x" * 48, _env_file=None)  # type: ignore[call-arg]
    assert "JWT_SECRET" not in caplog.text


def test_the_code_default_is_one_of_them() -> None:
    assert Settings.model_fields["jwt_secret"].default in PUBLIC_JWT_SECRETS


# --- ENVIRONMENT=production refuses what development only warns about ---------

_PRODUCTION = {
    "environment": "production",
    "jwt_secret": "x" * 48,
    "database_url": "postgresql+asyncpg://app:s3cret-enough@db.internal:5432/youth",
    "cors_origins": ["https://you.th"],
    "public_site_url": "https://you.th",
}


def test_production_starts_on_real_values() -> None:
    Settings(**_PRODUCTION, _env_file=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("override", "named"),
    [
        ({"jwt_secret": sorted(PUBLIC_JWT_SECRETS)[0]}, "JWT_SECRET"),
        ({"database_url": Settings.model_fields["database_url"].default}, "DATABASE_URL"),
        ({"public_site_url": "http://localhost:5173"}, "PUBLIC_SITE_URL"),
        ({"cors_origins": ["http://localhost:5173"]}, "CORS_ORIGINS"),
    ],
)
def test_production_refuses_a_development_default(
    override: dict[str, object], named: str
) -> None:
    with pytest.raises(ValueError, match=named):
        Settings(**{**_PRODUCTION, **override}, _env_file=None)  # type: ignore[arg-type]
