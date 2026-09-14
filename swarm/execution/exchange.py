"""Binance Spot Testnet only; the final HTTP boundary is allowlisted."""

import asyncio
from urllib.parse import urlsplit

import ccxt.async_support as ccxt
import ccxt.pro as ccxtpro

from swarm.main import TESTNET_HOSTS, validate_config
from swarm.secrets import require


class TestnetBinance(ccxt.binance):
    async def fetch(self, url, method="GET", headers=None, body=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in TESTNET_HOSTS:
            raise ValueError("Non-testnet HTTP request blocked")
        return await super().fetch(url, method, headers, body)


def create_exchange(config):
    validate_config(config)
    key = require("BINANCE_TESTNET_API_KEY")
    secret = require("BINANCE_TESTNET_API_SECRET")
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
                # Account-wide reconciliation intentionally uses the higher-weight endpoint.
                "fetchOpenOrders": {"warnWithoutSymbol": False},
                "warnOnFetchOpenOrdersWithoutSymbol": False,
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


def safe_error(exc):
    """Only allowlisted diagnostic fields; never print request URLs or credentials."""
    import re

    text = str(exc)
    match = re.search(r'"code"\s*:\s*(-?\d+)', text)
    code = int(match.group(1)) if match else None
    hints = {
        -1021: "check_local_clock",
        -1022: "check_testnet_secret",
        -2014: "check_testnet_key_format",
        -2015: "check_testnet_key_permissions_or_ip",
        -1003: "exchange_rate_limit",
    }
    hint = hints.get(code, "exchange_request_failed")
    if "warnWithoutSymbol" in text or "warnOnFetchOpenOrdersWithoutSymbol" in text:
        hint = "ccxt_accountwide_open_orders_warning"
    return dict(error_type=type(exc).__name__, exchange_code=code, hint=hint)
