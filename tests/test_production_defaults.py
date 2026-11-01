"""Production must not start on development defaults (#65).

``ENVIRONMENT=production`` gates three things: the interactive docs and
OpenAPI schema are no longer served, placeholder ``mock-*`` R2 credentials are
refused at construction time, and localhost CORS origins are dropped.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings, api_docs_urls

REAL_R2 = {
    "R2_ACCOUNT_ID": "account-123",
    "R2_ACCESS_KEY_ID": "access-key-123",
    "R2_SECRET_ACCESS_KEY": "secret-key-123",
}


def _production(**overrides) -> Settings:
    return Settings(ENVIRONMENT="production", **REAL_R2, **overrides)


def test_production_rejects_mock_r2_credentials():
    with pytest.raises(ValidationError) as excinfo:
        Settings(ENVIRONMENT="production")

    message = str(excinfo.value)
    for field in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        assert field in message
    # The error names the settings but never echoes their values.
    assert "mock-" not in message


@pytest.mark.parametrize(
    "field",
    ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"],
)
def test_production_rejects_any_single_mock_r2_credential(field):
    values = {"ENVIRONMENT": "production", **REAL_R2, field: f"mock-{field.lower()}"}

    with pytest.raises(ValidationError) as excinfo:
        Settings(**values)

    assert field in str(excinfo.value)
    assert values[field] not in str(excinfo.value)


@pytest.mark.parametrize("environment", ["production", "Production", " PRODUCTION "])
def test_production_is_matched_case_insensitively(environment):
    with pytest.raises(ValidationError):
        Settings(ENVIRONMENT=environment)


def test_production_with_real_r2_credentials_constructs():
    settings = _production()

    assert settings.ENVIRONMENT == "production"
    assert settings.R2_ACCOUNT_ID == REAL_R2["R2_ACCOUNT_ID"]


def test_production_drops_localhost_origins():
    settings = _production(
        ALLOWED_ORIGINS=[
            "https://lynkshare.app",
            "http://localhost:8000",
            "http://localhost:3000",
            "http://127.0.0.1:8000",
            "https://[::1]:8000",
        ]
    )

    assert settings.ALLOWED_ORIGINS == ["https://lynkshare.app"]
    for origin in settings.ALLOWED_ORIGINS:
        assert "localhost" not in origin
        assert "127.0.0.1" not in origin


def test_development_defaults_keep_localhost_origins_and_docs():
    settings = Settings(_env_file=None)

    assert settings.ENVIRONMENT == "development"
    assert "http://localhost:8000" in settings.ALLOWED_ORIGINS
    assert "http://localhost:3000" in settings.ALLOWED_ORIGINS
    assert api_docs_urls(settings.ENVIRONMENT)["docs_url"] == "/docs"


@pytest.mark.parametrize("environment", ["production", "Production", " PRODUCTION "])
def test_docs_helper_disables_every_schema_endpoint_in_production(environment):
    assert api_docs_urls(environment) == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


@pytest.mark.parametrize("environment", ["development", "test", "staging", ""])
def test_docs_helper_keeps_fastapi_defaults_outside_production(environment):
    assert api_docs_urls(environment) == {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


def test_development_app_serves_docs_and_schema():
    from app.main import app

    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"
