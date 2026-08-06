"""Historical sweep by quarterly blocks, resumable across sessions (ALD-30).

Same queries and the same parser as the daily sweep (PRD §8.6). What differs is
only the bookkeeping: where the worklist comes from, and when a block counts as
covered.

The worklist is `CoverageStore.pending()` over the grid of ALD-26 — every
market, price mode and quarter the source actually has data for, in global
chronological order. Blocks are opened one at a time as the engine asks for
them, so a session that dies leaves a handful of blocks `abierto` and the rest
untouched, and the next session starts exactly there.

The HTTP cache stays **off**. It was put in for this spider, to resume without
downloading again; the coverage table does that now for a few hundred KB
instead of the 20+ GB that caching 9 600 multi-megabyte responses would cost.

Usage:
    uv run python -m scraper.spiders.backfill
    uv run python -m scraper.spiders.backfill --limit 500
    uv run python -m scraper.spiders.backfill --market 210 \
        --desde 2011-07-01 --hasta 2011-09-30
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date

import scrapy

from scraper.backfill_state import Block, BlockLedger, CoverageStore, plan_grid
from scraper.console import Snapshot
from scraper.harvest import UNREADABLE, lost_request, rows_from
from scraper.parsers.results import EMPTY_MARKER
from scraper.pipelines.ndjson import RAW_DIR
from scraper.query import ALL, DateWindow, build_url, next_windows

LOG_PATH = RAW_DIR.parent / "backfill.log"

# How many blocks may be open at once. Scrapy drains `start()` into the
# scheduler as fast as it can — measured: 226 blocks opened while 4 finished —
# so without a ceiling the whole grid would be written as `abierto` before the
# first minute was out, and `abierto` is supposed to mean a session died
# holding this block. Enough to keep two concurrent requests fed, few enough
# that the count after a crash is a number somebody can read.
MAX_OPEN_BLOCKS = 8

# How often `start()` looks again for room. Requests take seconds; this is
# noise next to them.
ROOM_POLL_SECONDS = 0.2


class BackfillSpider(scrapy.Spider):
    name = "backfill"

    def __init__(
        self,
        store: CoverageStore | None = None,
        grid: Sequence[Block] | None = None,
        destinations: list[str] | None = None,
        since: date | None = None,
        until: date | None = None,
        limit: int | None = None,
        max_open_blocks: int = MAX_OPEN_BLOCKS,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.max_open_blocks = max_open_blocks
        self.store = store if store is not None else CoverageStore()
        blocks = list(grid) if grid is not None else plan_grid(destinations=destinations)
        # Whole blocks, never clipped: a block covering less than the grid asks
        # for would never match it again and would be retried forever.
        if since is not None:
            blocks = [block for block in blocks if block.end >= since]
        if until is not None:
            blocks = [block for block in blocks if block.start <= until]
        self.grid = blocks

        pending = self.store.pending(self.grid)
        # A bounded session is the normal way this runs: an hour free, five
        # hundred blocks, stop. The rest is still there tomorrow.
        self.blocks = pending[:limit] if limit else pending
        self.covered = len(self.grid) - len(pending)

        self.ledger = BlockLedger(self.store)
        self.failures: list[str] = []
        self.resolved = 0
        self.current: Block | None = None

    # --- what the progress console reads ---

    def progress(self) -> Snapshot:
        return Snapshot(
            done=self.covered + self.resolved,
            total=len(self.grid),
            label=self.current.label if self.current else "",
        )

    # --- the worklist ---

    async def start(self):
        """A few blocks at a time, waiting for room before opening the next.

        Scrapy pulls from here as fast as the scheduler will take it, which is
        the whole grid — so the waiting is done here. Awaiting inside `start()`
        hands the reactor back, so the crawl keeps running while this stalls.
        """
        for block in self.blocks:
            while self.ledger.open_blocks() >= self.max_open_blocks:
                await asyncio.sleep(ROOM_POLL_SECONDS)
            self.ledger.opened(block)
            self.current = block
            yield self._request(block, block.window)

    def _request(self, block: Block, window: DateWindow) -> scrapy.Request:
        return scrapy.Request(
            build_url(
                ALL,
                window,
                destination_id=block.destination_id,
                prices_per_id=block.prices_per_id,
            ),
            callback=self.parse,
            errback=self.errback,
            cb_kwargs={"block": block, "window": window},
            dont_filter=True,
        )

    # --- one response ---

    def parse(self, response, block: Block, window: DateWindow):
        """Rows for a window that fits, halves for one that does not."""
        if EMPTY_MARKER in response.text:
            # A real gap: the probe measures a year, and a market with data in
            # 1998 did not necessarily report every quarter of it.
            self._settle(block)
            return

        try:
            splits = next_windows(window, response.text)
        except UNREADABLE as error:
            self._lost(block, window, error)
            return

        if splits:
            # One request became several; the block is not done until all of
            # them are.
            self.ledger.split(block, len(splits))
            for half in splits:
                yield self._request(block, half)
            return

        try:
            rows = rows_from(
                response,
                destination_id=block.destination_id,
                prices_per_id=block.prices_per_id,
                window=window,
            )
        except UNREADABLE as error:
            self._lost(block, window, error)
            return

        yield from rows
        self._settle(block, rows=len(rows))

    def errback(self, failure):
        kwargs = failure.request.cb_kwargs
        self._lost(kwargs["block"], kwargs["window"], lost_request(failure))

    # --- closing a block ---

    def _settle(self, block: Block, rows: int = 0) -> None:
        if self.ledger.finished(block, rows=rows):
            self.resolved += 1

    def _lost(self, block: Block, window: DateWindow, error: Exception) -> None:
        """A window nobody could read taints its whole quarter.

        Retrying the quarter costs requests and nothing else, because the load
        is idempotent on the row key. Recording it as covered when part of it
        was never read is the one failure that leaves no trace.
        """
        detail = (
            f"{block.quarter} destino={block.destination_id} "
            f"precio={block.price_mode} "
            f"ventana={window.start:%Y-%m-%d}..{window.end:%Y-%m-%d}: "
            f"{type(error).__name__}: {error}"
        )
        self.failures.append(detail)
        self.logger.error(detail)
        if self.ledger.failed(block, f"{type(error).__name__}: {error}"):
            self.resolved += 1

    def closed(self):
        # Scrapy connects this to spider_closed, and pydispatch only passes the
        # arguments the receiver accepts.
        self.store.close()


def _day(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str]) -> int:
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", action="append", dest="markets")
    parser.add_argument("--desde", type=_day, help="ignorar bloques que terminan antes")
    parser.add_argument("--hasta", type=_day, help="ignorar bloques que empiezan después")
    parser.add_argument(
        "--limit",
        type=int,
        help="cuántos bloques hacer en esta sesión; el resto queda para la próxima",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="log de Scrapy en la consola en vez de la barra de avance",
    )
    args = parser.parse_args(argv)

    settings = get_project_settings()
    if not args.plain:
        # LOG_FILE is what removes Scrapy's console handler, and it is read
        # when logging is configured — it cannot be set any later than this.
        settings.set("LOG_FILE", str(LOG_PATH))
        settings.set("PROGRESS_CONSOLE_ENABLED", True)

    process = CrawlerProcess(settings)
    crawler = process.create_crawler(BackfillSpider)
    process.crawl(
        crawler,
        destinations=args.markets,
        since=args.desde,
        until=args.hasta,
        limit=args.limit,
    )
    process.start()

    spider = crawler.spider
    failures = list(getattr(spider, "failures", []))
    for failure in failures:
        print(f"✗ {failure}", file=sys.stderr)
    if failures:
        print(
            f"{len(failures)} ventana(s) fallaron; esos bloques vuelven a la cola "
            f"la próxima sesión. Detalle en {LOG_PATH}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
