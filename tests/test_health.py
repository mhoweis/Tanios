"""Milestone 0: the server answers at its health-check addresses."""

from fastapi.testclient import TestClient

from tanios import __version__
from tanios.config import Settings
from tanios.main import create_app


class FakeDatabase:
    def __init__(self, reachable: bool) -> None:
        self.reachable = reachable
        self.disposed = False

    async def ping(self) -> bool:
        return self.reachable

    async def dispose(self) -> None:
        self.disposed = True


def settings(**overrides) -> Settings:
    # _env_file=None keeps a developer's local .env out of the tests.
    return Settings(_env_file=None, **overrides)


def test_healthz_reports_ok_and_the_version():
    with TestClient(create_app(settings())) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_healthz_does_not_depend_on_the_database():
    with TestClient(create_app(settings(), FakeDatabase(reachable=False))) as client:
        response = client.get("/healthz")

    assert response.status_code == 200


def test_readyz_is_ready_when_the_database_answers():
    with TestClient(create_app(settings(), FakeDatabase(reachable=True))) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "whatsapp": "off"}


def test_readyz_is_not_ready_when_the_database_is_down():
    with TestClient(create_app(settings(), FakeDatabase(reachable=False))) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not ready", "database": "down", "whatsapp": "off"}


def test_readyz_is_not_ready_when_no_database_is_configured():
    with TestClient(create_app(settings())) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not ready",
        "database": "not configured",
        "whatsapp": "off",
    }


def test_the_database_is_closed_when_the_server_stops():
    database = FakeDatabase(reachable=True)

    with TestClient(create_app(settings(), database)):
        assert not database.disposed

    assert database.disposed


def test_api_pages_are_hidden_in_production():
    with TestClient(create_app(settings(env="production"))) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_api_pages_are_available_in_development():
    with TestClient(create_app(settings(env="development"))) as client:
        assert client.get("/docs").status_code == 200
