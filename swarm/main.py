"""Configuration guard and bounded Spot Testnet paper runner."""

import argparse
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from swarm.secrets import SecretError, expand, load_dotenv, refuse_placeholders

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = ROOT / "config/settings.yaml"

TESTNET_HOSTS = {
    "testnet.binance.vision",
    "stream.testnet.binance.vision",
    "ws-api.testnet.binance.vision",
}


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("configuration must be a mapping")
    exchange = config.get("exchange", {})
    if not isinstance(exchange, dict) or exchange.get("sandbox") is not True:
        raise ValueError("exchange.sandbox must be true")
    urls = exchange.get("urls")
    if not isinstance(urls, dict) or not urls:
        raise ValueError("explicit testnet URLs required")

    def inspect(value, exchange_scope=False):
        if isinstance(value, dict):
            for item in value.values():
                inspect(item, exchange_scope)
        elif isinstance(value, list):
            for item in value:
                inspect(item, exchange_scope)
        elif isinstance(value, str):
            if "api.binance.com" in value.lower():
                raise ValueError("mainnet URL forbidden")
            if exchange_scope:
                url = urlsplit(value)
                if (
                    url.scheme not in {"https", "wss"}
                    or url.hostname not in TESTNET_HOSTS
                    or url.username
                    or url.password
                    or url.port not in {None, 443}
                ):
                    raise ValueError("only approved Binance Spot Testnet URLs are allowed")
        elif exchange_scope:
            raise ValueError("URL must be a string")

    inspect(config)
    inspect(urls, True)
    return config


def load_config(path: Path = DEFAULT_SETTINGS) -> dict:
    """Single startup path for every entrypoint: .env seed, placeholder refusal,
    ${VAR} expansion from the environment, then the testnet-only guard."""
    load_dotenv()
    refuse_placeholders()
    return validate_config(expand(yaml.safe_load(Path(path).read_text())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--mode", choices=["paper"])
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument(
        "--smoke-order",
        action="store_true",
        help="Propose one 20 USDT testnet order through the risk gate",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_SETTINGS)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        parser.exit(1, f"configuration rejected: {exc}\n")
    if args.check:
        print("config ok, sandbox=true")
    elif args.mode == "paper":
        import asyncio

        from swarm.execution.paper import run

        try:
            asyncio.run(run(config, args.duration, args.smoke_order))
        except KeyboardInterrupt:
            parser.exit(130, "paper runner stopped; managed exits require a running process\n")
        except (SecretError, ValueError, RuntimeError) as exc:
            # Our own fixed-string messages; safe to show. Provider errors stay type-only.
            parser.exit(1, f"paper runner stopped: {type(exc).__name__}: {exc}\n")
        except Exception as exc:
            parser.exit(1, f"paper runner stopped: {type(exc).__name__}\n")
    else:
        parser.error("use --check or --mode paper")


if __name__ == "__main__":
    main()
