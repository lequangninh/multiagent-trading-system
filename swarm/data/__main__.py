"""Run market and RSS ingestion: uv run python -m swarm.data."""

import argparse
import asyncio
from pathlib import Path

from swarm.data.feed import MarketFeed
from swarm.data.news import poll
from swarm.data.store import Store
from swarm.main import DEFAULT_SETTINGS, load_config


async def run(config):
    store = Store(config["database"]["dsn"])
    feed = None
    try:
        await store.connect()
        feed = MarketFeed(config["universe"], store)
        async with asyncio.TaskGroup() as group:
            group.create_task(feed.run())
            group.create_task(
                poll(store, config["news"]["feeds"], config["news"]["poll_interval_s"])
            )
    finally:
        try:
            if feed is not None:
                await feed.close()
        finally:
            await store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_SETTINGS)
    args = parser.parse_args()
    asyncio.run(run(load_config(args.config)))


if __name__ == "__main__":
    main()
