import pytest

from swarm.main import validate_config


def config(url="https://testnet.binance.vision/api", sandbox=True):
    return {"exchange": {"sandbox": sandbox, "urls": {"rest": url}}}


def test_valid_config():
    validate_config(config())


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_requires_boolean_true(value):
    with pytest.raises(ValueError):
        validate_config(config(sandbox=value))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.binance.com",
        "https://api.binance.com/testnet",
        "https://api.binance.us",
        "https://testnet.binance.vision.evil.org",
        "https://testnet@api.binance.com",
        "http://testnet.binance.vision",
        "https://testnet.binance.vision:8443",
        "https://example.org/testnet",
    ],
)
def test_forbidden_endpoints(url):
    with pytest.raises(ValueError):
        validate_config(config(url))
