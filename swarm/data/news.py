"""RSS ingestion without model calls."""

import asyncio
import email.utils
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import structlog

log = structlog.get_logger()
TAG_RE = re.compile(r"\b(BTC|ETH|SOL|BITCOIN|ETHEREUM|SOLANA)\b", re.I)
ALIASES = {"BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL"}


def parse_feed(payload: str, source: str) -> list[dict]:
    root = ET.fromstring(payload)
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "").strip()
        body = item.findtext("description", "").strip()
        url = item.findtext("link", "").strip()
        raw_date = item.findtext("pubDate")
        if not title or not url:
            continue
        try:
            parsed = (
                email.utils.parsedate_to_datetime(raw_date)
                if raw_date
                else datetime.now(timezone.utc)
            )
        except (ValueError, TypeError, OverflowError):
            log.warning("rss_invalid_date", source=source, url=url)
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        tags = sorted(
            {ALIASES.get(tag.upper(), tag.upper()) for tag in TAG_RE.findall(f"{title} {body}")}
        )
        items.append(
            {
                "ts": parsed,
                "source": source,
                "title": title,
                "body": body,
                "url": url,
                "symbol_tags": tags,
            }
        )
    return items


async def ingest_once(store, feeds: list[str], *, session=None) -> int:
    owns_session = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    inserted = 0
    try:
        for feed in feeds:
            source = urlparse(feed).hostname or feed
            try:
                async with session.get(feed) as response:
                    response.raise_for_status()
                    items = parse_feed(await response.text(), source)
                if items:
                    await store.pool.executemany(
                        """INSERT INTO news (ts,source,title,body,url,symbol_tags)
                        VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (source,url) DO NOTHING""",
                        [
                            (
                                x["ts"],
                                x["source"],
                                x["title"],
                                x["body"],
                                x["url"],
                                x["symbol_tags"],
                            )
                            for x in items
                        ],
                    )
                    inserted += len(items)
            except Exception as exc:
                log.warning("rss_error", feed=feed, error=str(exc))
    finally:
        if owns_session:
            await session.close()
    return inserted


async def poll(store, feeds: list[str], interval_s: int = 300) -> None:
    while True:
        await ingest_once(store, feeds)
        await asyncio.sleep(interval_s)
