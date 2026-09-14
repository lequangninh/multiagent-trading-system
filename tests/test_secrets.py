from pathlib import Path

import pytest

from swarm.main import load_config
from swarm.secrets import (
    SecretError,
    expand,
    is_placeholder,
    load_dotenv,
    refuse_placeholders,
    require,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", True),
        ("changeme", True),
        ("  ChangeMe ", True),
        ("your_key_here", True),
        ("<paste key>", True),
        ("xxxxxxxx", True),
        ("...", True),
        ("swarm_local_only", False),
        ("Ab3fj29fk3Kd93j", False),
        ("https://hooks.example/abc", False),
    ],
)
def test_placeholder_detection(value, expected):
    assert is_placeholder(value) is expected


def test_dotenv_seeds_but_never_overrides_and_skips_empty(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nPOSTGRES_PASSWORD='fromfile'\nexport BINANCE_TESTNET_API_KEY=\"k1\"\n"
        "BINANCE_TESTNET_API_SECRET=\nlowercase=no\nBROKEN LINE\n"
    )
    monkeypatch.setenv("POSTGRES_PASSWORD", "fromenv")
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    assert load_dotenv(env) == ["BINANCE_TESTNET_API_KEY"]
    import os

    assert os.environ["POSTGRES_PASSWORD"] == "fromenv"
    assert os.environ["BINANCE_TESTNET_API_KEY"] == "k1"
    assert "BINANCE_TESTNET_API_SECRET" not in os.environ and "lowercase" not in os.environ
    assert load_dotenv(tmp_path / "missing") == []


def test_require_and_refuse(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(SecretError, match="not set"):
        require("POSTGRES_PASSWORD")
    monkeypatch.setenv("POSTGRES_PASSWORD", "changeme")
    with pytest.raises(SecretError, match="placeholder"):
        require("POSTGRES_PASSWORD")
    with pytest.raises(SecretError, match="POSTGRES_PASSWORD"):
        refuse_placeholders()
    monkeypatch.setenv("POSTGRES_PASSWORD", "real-one")
    refuse_placeholders()
    assert expand({"a": ["x${POSTGRES_PASSWORD}y", 1], "b": None}) == {
        "a": ["xreal-oney", 1],
        "b": None,
    }
    with pytest.raises(SecretError):
        expand("${SWARM_LLM_API_KEY}")


def test_load_config_expands_dsn_and_refuses_placeholders(monkeypatch):
    monkeypatch.setattr("swarm.main.load_dotenv", lambda: [])
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw-for-test")
    config = load_config(ROOT / "config/settings.yaml")
    assert config["database"]["dsn"] == "postgresql://swarm:pw-for-test@localhost:5432/swarm"
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "placeholder")
    with pytest.raises(SecretError, match="BINANCE_TESTNET_API_KEY"):
        load_config(ROOT / "config/settings.yaml")


def test_no_secret_values_in_tracked_files():
    for path in (
        "config/settings.yaml",
        "docker-compose.yml",
        "dashboards/datasources/timescale.yaml",
    ):
        text = (ROOT / path).read_text()
        assert "swarm_local_only" not in text, path
    assert "changeme" in (ROOT / ".env.example").read_text()
    assert ".env\n" in (ROOT / ".gitignore").read_text()
