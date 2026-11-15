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
