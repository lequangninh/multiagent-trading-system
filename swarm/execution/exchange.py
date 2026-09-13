"""Binance Spot Testnet only; the final HTTP boundary is allowlisted."""

import asyncio
import os
from urllib.parse import urlsplit

import ccxt.async_support as ccxt
import ccxt.pro as ccxtpro

from swarm.main import TESTNET_HOSTS, validate_config


class TestnetBinance(ccxt.binance):
    async def fetch(self, url, method="GET", headers=None, body=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in TESTNET_HOSTS:
            raise ValueError("Non-testnet HTTP request blocked")
        return await super().fetch(url, method, headers, body)


def create_exchange(config):
    validate_config(config)
    key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    if any(
        not v.strip() or v.lower() in {"placeholder", "changeme", "your_key", "your_secret"}
        for v in (key, secret)
    ):
        raise ValueError("Set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET locally")
    exchange = TestnetBinance(
        {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": 10000,
            "options": {
                "defaultType": "spot",
                "fetchMarkets": {"types": ["spot"]},
                "fetchCurrencies": False,
            },
        }
    )
    exchange.set_sandbox_mode(True)
    return exchange


async def read_retry(method, *args, **kwargs):
    for attempt in range(3):
        try:
            return await method(*args, **kwargs)
        except ccxt.NetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)


class TestnetStream(ccxtpro.binance):
    async def fetch(self, url, method="GET", headers=None, body=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in TESTNET_HOSTS:
            raise ValueError("Non-testnet HTTP request blocked")
        return await super().fetch(url, method, headers, body)
