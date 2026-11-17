from tanios.config import Settings


def test_settings_have_safe_defaults(monkeypatch):
    for name in ("TANIOS_ENV", "TANIOS_LOG_LEVEL", "TANIOS_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.env == "development"
    assert settings.database_url is None


def test_settings_come_from_tanios_environment_variables(monkeypatch):
    monkeypatch.setenv("TANIOS_ENV", "production")
    monkeypatch.setenv("TANIOS_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/tanios")

    settings = Settings(_env_file=None)

    assert settings.env == "production"
    assert settings.database_url == "postgresql+asyncpg://u:p@db:5432/tanios"


def test_variables_without_the_prefix_are_ignored(monkeypatch):
    monkeypatch.delenv("TANIOS_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://other")

    assert Settings(_env_file=None).database_url is None


def test_blank_lines_in_the_env_file_count_as_not_set(monkeypatch):
    monkeypatch.setenv("TANIOS_WHATSAPP_VERIFY_TOKEN", "")
    monkeypatch.setenv("TANIOS_WHATSAPP_APP_SECRET", "   ")
    monkeypatch.setenv("TANIOS_OWNER_NUMBER", "")

    settings = Settings(_env_file=None)

    assert settings.whatsapp_verify_token is None
    assert settings.whatsapp_app_secret is None
    assert "TANIOS_OWNER_NUMBER" in settings.missing_for_messaging()


def test_messaging_needs_every_setting_and_reports_only_names(monkeypatch):
    for name in (
        "DATABASE_URL",
        "WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "OWNER_NUMBER",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.setenv(f"TANIOS_{name}", "value-for-" + name.lower())

    complete = Settings(_env_file=None)
    assert complete.missing_for_messaging() == []

    monkeypatch.delenv("TANIOS_OPENROUTER_API_KEY")
    assert Settings(_env_file=None).missing_for_messaging() == ["TANIOS_OPENROUTER_API_KEY"]


def test_secrets_do_not_appear_when_settings_are_printed(monkeypatch):
    monkeypatch.setenv("TANIOS_WHATSAPP_ACCESS_TOKEN", "EAAJ-very-secret")
    monkeypatch.setenv("TANIOS_OPENROUTER_API_KEY", "sk-or-very-secret")

    assert "very-secret" not in repr(Settings(_env_file=None))


def test_r2_extra_provider_keys_come_from_their_own_settings(monkeypatch):
    monkeypatch.setenv("TANIOS_OPENROUTER_API_KEY", "sk-or-1")
    monkeypatch.setenv("TANIOS_KEYS__DEEPSEEK", "sk-ds-2")
    monkeypatch.setenv("TANIOS_KEYS__BLANK", "")

    settings = Settings(_env_file=None)

    assert settings.provider_keys() == {"openrouter": "sk-or-1", "deepseek": "sk-ds-2"}
    assert "sk-ds-2" not in repr(settings)


def test_r2_any_one_provider_key_is_enough_for_messaging(monkeypatch):
    monkeypatch.delenv("TANIOS_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("TANIOS_KEYS__DEEPSEEK", "sk-ds-2")

    assert "TANIOS_OPENROUTER_API_KEY" not in Settings(_env_file=None).missing_for_messaging()


def test_r4_the_monthly_cap_is_a_non_negative_amount_or_unset(monkeypatch):
    import pytest
    from pydantic import ValidationError

    monkeypatch.setenv("TANIOS_MONTHLY_CAP_USD", "7.50")
    assert str(Settings(_env_file=None).monthly_cap_usd) == "7.50"

    monkeypatch.setenv("TANIOS_MONTHLY_CAP_USD", "")
    assert Settings(_env_file=None).monthly_cap_usd is None

    monkeypatch.setenv("TANIOS_MONTHLY_CAP_USD", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_r3_free_models_for_private_content_are_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("TANIOS_ALLOW_FREE_FOR_PRIVATE", raising=False)
    assert Settings(_env_file=None).allow_free_for_private is False

    monkeypatch.setenv("TANIOS_ALLOW_FREE_FOR_PRIVATE", "true")
    assert Settings(_env_file=None).allow_free_for_private is True
