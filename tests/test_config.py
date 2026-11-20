from app.config import settings
from app.utils.security import redact, redact_message, verify_bearer_token


def test_public_dict_has_no_secret_values():
    public = settings.public_dict()
    blob = str(public)
    assert settings.api_access_token not in blob
    assert settings.nvidia_api_key not in blob
    assert settings.render_api_key not in blob


def test_required_credentials_present():
    assert settings.supabase_database_url
    assert settings.nvidia_api_key
    assert settings.telegram_api_id
    assert settings.telegram_api_hash
    assert settings.telegram_phone
    assert settings.telegram_owner_id
    assert settings.api_access_token


def test_redaction():
    secret = "supersecretvalue1234"
    assert secret not in redact_message(f"key={secret}")
    assert "supersecret" not in redact(secret)


def test_bearer_verification():
    assert verify_bearer_token(f"Bearer {settings.api_access_token}")
    assert not verify_bearer_token("Bearer wrong-token")
    assert not verify_bearer_token(None)
