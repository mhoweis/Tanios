"""Milestone 2: the models settings file."""

from decimal import Decimal
from pathlib import Path

import pytest

from tanios.catalog import (
    ESCALATION,
    CatalogError,
    load_catalog,
    parse_catalog,
    resolve_path,
)

REPO = Path(__file__).parent.parent

VALID = {
    "providers": {"OpenRouter": {"api_base": "https://openrouter.ai/api/v1/"}},
    "tiers": {
        "small": [
            {"provider": "openrouter", "model": "a/model", "input_price": 0.1, "output_price": 0.5}
        ]
    },
    "tasks": {"chat": "small"},
}


def test_the_shipped_models_file_is_valid_and_covers_every_text_tier():
    catalog = load_catalog(REPO / "config" / "models.toml")

    assert set(ESCALATION) <= set(catalog.tiers)
    assert all(catalog.tiers[tier] for tier in ESCALATION)
    assert catalog.tier_for("chat") == "small"
    assert "openrouter" in catalog.providers


def test_the_shipped_free_tier_is_marked_free_and_no_paid_tier_is():
    catalog = load_catalog(REPO / "config" / "models.toml")

    assert all(entry.free for entry in catalog.tiers["free"])
    for tier in ("small", "medium", "top"):
        assert not any(entry.free for entry in catalog.tiers[tier])


def test_provider_names_ignore_case_and_addresses_lose_the_trailing_slash():
    catalog = parse_catalog(VALID)

    assert catalog.providers["openrouter"].api_base == "https://openrouter.ai/api/v1"
    assert catalog.tiers["small"][0].input_price == Decimal("0.1")


def test_the_owners_own_file_wins_over_the_shipped_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.toml").write_text("")

    assert resolve_path(None) == Path("config/models.toml")

    (tmp_path / "config" / "models.local.toml").write_text("")
    assert resolve_path(None) == Path("config/models.local.toml")
    assert resolve_path("/elsewhere/models.toml") == Path("/elsewhere/models.toml")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"providers": {}}, "at least one provider"),
        ({"providers": {"x": {"api_base": "ftp://nope"}}}, "api_base"),
        ({"tiers": {"huge": []}}, "unknown tier 'huge'"),
        ({"tiers": {"small": [{"provider": "nobody", "model": "m"}]}}, "provider 'nobody'"),
        ({"tiers": {"small": [{"provider": "openrouter"}]}}, "has no model"),
        (
            {"tiers": {"small": [{"provider": "openrouter", "model": "m", "free": "yes"}]}},
            "true or false",
        ),
        (
            {"tiers": {"small": [{"provider": "openrouter", "model": "m", "input_price": -1}]}},
            "zero or more",
        ),
        ({"tasks": {"chat": "enormous"}}, "unknown tier 'enormous'"),
    ],
)
def test_mistakes_in_the_file_are_reported_in_plain_words(change, message):
    with pytest.raises(CatalogError, match=message):
        parse_catalog({**VALID, **change})


def test_a_missing_file_and_broken_toml_are_reported_clearly(tmp_path):
    with pytest.raises(CatalogError, match="does not exist"):
        load_catalog(tmp_path / "nope.toml")

    broken = tmp_path / "broken.toml"
    broken.write_text("[providers.openrouter\napi_base = ")
    with pytest.raises(CatalogError, match="not valid TOML"):
        load_catalog(broken)
