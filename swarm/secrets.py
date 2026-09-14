"""Secrets come only from the environment, optionally seeded from a git-ignored .env.

Startup refuses placeholder values so a copied .env.example can never run as-is.
Values are never logged or printed; errors name the variable, not its content.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    "POSTGRES_PASSWORD",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "SWARM_ALERT_WEBHOOK",
    "SWARM_LLM_API_KEY",
)
PLACEHOLDERS = {
    "",
    "changeme",
    "change_me",
    "change-me",
    "placeholder",
    "todo",
    "replace_me",
    "replace-me",
    "xxx",
    "xxxx",
    "secret",
    "password",
    "example",
    "null",
    "none",
    "your_key",
    "your_secret",
    "your-key",
    "your-secret",
}
_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class SecretError(ValueError):
    pass


def is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return (
        v in PLACEHOLDERS
        or v.startswith(("your_", "your-", "<", "..."))
        or v.endswith(">")
        or set(v) <= {"x", "*", "."}
    )


def load_dotenv(path: Path | None = None) -> list[str]:
    """Seed os.environ from KEY=VALUE lines; real environment always wins. Returns keys set."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not value or key in os.environ:
            continue  # Empty values in .env mean "unset", so optional secrets stay optional.
        os.environ[key] = value
        loaded.append(key)
    return loaded


def refuse_placeholders(names=SECRET_NAMES) -> None:
    bad = [n for n in names if n in os.environ and is_placeholder(os.environ[n])]
    if bad:
        raise SecretError(f"placeholder secret(s) refused: {', '.join(bad)}; set real values")


def require(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise SecretError(
            f"{name} is not set: export it, or copy .env.example to .env and fill it in"
        )
    if is_placeholder(value):
        raise SecretError(f"{name} is a placeholder value; set the real secret")
    return value


def expand(value):
    """Replace ${NAME} in string leaves with required environment values. No defaults."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: require(m.group(1)), value)
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    return value
