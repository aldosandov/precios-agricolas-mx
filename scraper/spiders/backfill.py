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
from collections import Counter
from collections.abc import Callable, Sequence
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

# How long a finished quarter waits for the item pipeline to catch up before it
# is loaded anyway. Ten seconds is far past what draining a few thousand rows
# takes; loading early is not a loss either, because the row count is checked
# again before anything is deleted.
SETTLE_POLL_SECONDS = 0.2
SETTLE_TRIES = 50


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

        # How many blocks of each quarter this session still owes. A quarter
        # hitting zero is the moment its dates are complete on disk and can be
        # handed over — the spider only announces it, and does not know or care
        # that what happens next is a load into BigQuery.
        self.owed = Counter(block.start for block in self.blocks)
        # Rows the spider handed to the pipeline, by quarter.
        self.harvested: Counter[date] = Counter()
        self.on_quarter: Callable[[date, date], None] | None = None
        # Lines worth putting on screen that are not failures. The progress
        # console drains these the same way it drains `failures`.
        self.notes: list[str] = []

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
        # Counted per quarter, not per block: this is what the pipeline's own
        # count has to catch up with before the quarter can be handed over.
        self.harvested[block.start] += rows
        if self.ledger.finished(block, rows=rows):
            self._resolved(block)

    def _resolved(self, block: Block) -> None:
        """One more block accounted for, however it went.

        A block that failed still leaves the queue for this session, and its
        quarter still gets handed over: what was captured belongs in BigQuery,
        and the block comes back on the next session to be merged on top.
        """
        self.resolved += 1
        self.owed[block.start] -= 1
        if self.owed[block.start] == 0:
            del self.owed[block.start]
            if self.on_quarter is not None:
                self.on_quarter(block.start, block.end)

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
            self._resolved(block)

    def closed(self, reason):  # noqa: ARG002
        # `Spider.close` calls this directly with the reason — it is not a
        # signal handler, so the argument is not optional. Dropping it raises
        # a TypeError that Scrapy catches and logs, and the store never closes.
        self.store.close()


def _day(value: str) -> date:
    return date.fromisoformat(value)


def load_and_purge(start: date, end: date, *, table: str | None = None) -> str:
    """Put a finished quarter into BigQuery and take it off the disk.

    The whole history is ~10.7 GB of NDJSON and this runs on a personal laptop,
    so nothing is kept that is already stored somewhere better. Loading a
    quarter at a time is also what keeps the MERGE cheap: the grid is
    chronological, so the date range prunes to that quarter's partitions
    instead of scanning the history that a per-market sweep would.

    Imported here rather than at the top: the spider has no business knowing
    about the warehouse, and this is the entry point, where the two halves of
    the project are allowed to meet.
    """
    from transform.load import RAW_TABLE, load, partition_files, purge

    paths = partition_files(RAW_DIR, since=start, until=end)
    if not paths:
        return f"{start:%Y-%m-%d}..{end:%Y-%m-%d}: nada que cargar"

    report = load(paths, target=table or RAW_TABLE)
    purged = purge(paths, report)
    return (
        f"{start:%Y-%m-%d}..{end:%Y-%m-%d}: {report.staged_rows} filas -> "
        f"{report.affected_rows} en BigQuery, {purged} partición(es) purgadas"
    )


class QuarterLoader:
    """Loads each finished quarter off the reactor, and finishes before the run does.

    Off the reactor because a load takes seconds to minutes: blocking that long
    stalls the downloads in flight, and autothrottle would read the pause as
    the source having gone slow and widen every delay after it.

    Finishing before the run does because otherwise it does not finish at all.
    The reactor stops as soon as the crawl is idle, and a load still in a
    thread at that moment is abandoned halfway — measured: the last quarter of
    a session was loaded twice, once by the thread nobody waited for and once
    by the sweep of leftovers. `wait()` hangs off `spider_closed`, which Scrapy
    does await.

    A load that fails is a note and not a stop: the partitions stay on disk and
    get picked up by the leftover sweep or by `transform.load` by hand.
    """

    def __init__(self, spider, table: str | None = None, writer=None):
        self.spider = spider
        self.table = table
        self.writer = writer
        self.pending: list = []

    def say(self, line: str) -> None:
        self.spider.notes.append(line)
        self.spider.logger.info(line)

    def flush(self, start: date, end: date) -> None:
        from twisted.internet.defer import Deferred

        done = Deferred()
        self.pending.append(done)
        self._attempt(start, end, 0, done)

    def _settled(self, start: date, end: date) -> bool:
        """True once the pipeline has written every row the spider handed it.

        Every block of the quarter having resolved is not the same thing:
        Scrapy processes items after the callback's generator has already
        ended, so at that moment some rows are still on their way through the
        item pipeline. Measured, loading anyway: 7 585 rows into BigQuery of
        the 7 770 that turned up on disk a moment later.
        """
        if self.writer is None:
            return True
        return self.writer.rows_between(start, end) >= self.spider.harvested[start]

    def _attempt(self, start: date, end: date, tries: int, done) -> None:
        from twisted.internet import reactor
        from twisted.internet.threads import deferToThread

        if not self._settled(start, end) and tries < SETTLE_TRIES:
            reactor.callLater(SETTLE_POLL_SECONDS, self._attempt, start, end, tries + 1, done)
            return

        # Rows sit in the file object's buffer until it is closed, so nothing
        # may read these partitions before this. Done on the reactor and not in
        # the thread below, because the writer belongs to the reactor.
        if self.writer is not None:
            self.writer.close_dated(start, end)

        deferred = deferToThread(load_and_purge, start, end, table=self.table)
        deferred.addCallback(self.say)
        deferred.addErrback(
            lambda failure: self.say(
                f"carga de {start:%Y-%m-%d}..{end:%Y-%m-%d} falló: "
                f"{failure.value}. Las particiones siguen en disco."
            )
        )
        deferred.addBoth(self._finished, done)

    def _finished(self, result, done):
        if done in self.pending:
            self.pending.remove(done)
        done.callback(None)
        return result

    def wait(self):
        from twisted.internet.defer import DeferredList

        return DeferredList(list(self.pending))


def main(argv: list[str]) -> int:
    from scrapy import signals
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

    from transform.load import partition_dates, partition_files

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
    parser.add_argument("--table", help="tabla destino; por omisión la cruda del proyecto")
    parser.add_argument(
        "--no-load",
        action="store_true",
        help="solo dejar NDJSON en disco, sin cargarlo ni purgarlo. El "
        "histórico completo son ~10.7 GB, así que esto es para probar",
    )
    args = parser.parse_args(argv)

    settings = get_project_settings()
    if not args.plain:
        # LOG_FILE is what removes Scrapy's console handler, and it is read
        # when logging is configured — it cannot be set any later than this.
        settings.set("LOG_FILE", str(LOG_PATH))
        settings.set("PROGRESS_CONSOLE_ENABLED", True)
        # Scrapy defaults to DEBUG, which over a session of hours is mostly
        # urllib3 chatter from the BigQuery client. INFO keeps the file worth
        # opening when something has to be investigated.
        settings.set("LOG_LEVEL", "INFO")

    process = CrawlerProcess(settings)
    crawler = process.create_crawler(BackfillSpider)
    if not args.no_load:
        # The spider does not exist until the reactor starts, so the wiring
        # waits for it. This is the one place the ingest and the warehouse are
        # allowed to know about each other.
        def wire(spider):
            loader = QuarterLoader(spider, args.table, getattr(crawler, "raw_writer", None))
            spider.on_quarter = loader.flush
            # Scrapy awaits spider_closed handlers that return a Deferred, so
            # this is what keeps the reactor alive until the loads are in.
            crawler.signals.connect(loader.wait, signals.spider_closed)

        crawler.signals.connect(wire, signals.spider_opened)
    process.crawl(
        crawler,
        destinations=args.markets,
        since=args.desde,
        until=args.hasta,
        limit=args.limit,
    )
    process.start()

    if not args.no_load:
        # Whatever the session stopped in the middle of: a quarter cut short by
        # --limit, or the tail of a run that was interrupted. Leaving it would
        # make the next session load partitions it did not write.
        leftovers = partition_files(RAW_DIR)
        if leftovers:
            dates = partition_dates(leftovers)
            print(load_and_purge(dates[0], dates[-1], table=args.table))

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
