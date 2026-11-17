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
        "DEFAULT_MODEL",
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
