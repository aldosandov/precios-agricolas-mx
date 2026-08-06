"""Daily incremental sweep: every market, both price modes, a few days back.

The market is the pinned criterion (PRD §8.2): 49 requests per window instead
of 222, and the table keeps product, quality, presentation and origin. Each
market is asked twice, once per price mode, because mode 2 divides prices by
the presentation weight and the response does not say which one it answered.

The window reaches several days back on purpose. The source publishes late
some days, and rerunning a day costs nothing: the partition is rewritten, not
appended to (see scraper/pipelines/ndjson.py).

One market must not cost the other 48 their day, so a response that cannot be
read is recorded and the sweep goes on. The run still ends in red: a lost day
is unrecoverable, but so is a schema change nobody notices.

Usage:
    uv run python -m scraper.spiders.daily              # last 7 days, 49 markets
    uv run python -m scraper.spiders.daily --days 3
    uv run python -m scraper.spiders.daily --market 210 --market 100
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta

import scrapy

from scraper.contract import ContractBreach, QueryContext, build_records
from scraper.coverage import active_markets
from scraper.parsers.results import EMPTY_MARKER, ParseError, parse_results
from scraper.pipelines.ndjson import RAW_DIR, UnwritableRow, to_raw_rows
from scraper.query import (
    ALL,
    DateWindow,
    NoPaginator,
    QueryRejected,
    WindowExhausted,
    build_url,
    next_windows,
    source_date,
)

# Several days back: the source sometimes publishes late, and reprocessing a
# day is free because the load is idempotent (PRD §8.9). Five covers the
# previous Thursday from a Monday, since the weekend eats two days of window.
DEFAULT_DAYS = 5

# Where the sweep leaves the windows it could not read, for the monitoring step
# to report them one by one instead of as a count.
FAILURES_PATH = RAW_DIR.parent / "failures.txt"

PRICE_MODES = ("1", "2")

# Everything that means "this response is not usable" and is worth losing one
# market over, but not the whole day.
UNREADABLE = (ParseError, ContractBreach, UnwritableRow, QueryRejected, NoPaginator)


class DailySpider(scrapy.Spider):
    name = "daily"

    def __init__(
        self,
        days: int | str = DEFAULT_DAYS,
        destinations: list[str] | None = None,
        end: date | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # Scrapy passes -a arguments as strings.
        span = int(days)
        if span < 1:
            raise ValueError(f"days must be positive, got {span}")
        last_day = end or source_date()
        self.window = DateWindow(last_day - timedelta(days=span - 1), last_day)
        # Only the markets the coverage scan found alive: the other six have
        # not published in years and cost a tenth of the sweep's requests.
        self.destination_ids = list(destinations or active_markets())
        self.failures: list[str] = []

    def plan_requests(self) -> list[scrapy.Request]:
        """Every market against both price modes, over the same window."""
        return [
            self._request(destination_id, prices_per_id, self.window)
            for destination_id in self.destination_ids
            for prices_per_id in PRICE_MODES
        ]

    async def start(self):
        # Scrapy 2.13 replaced start_requests() with this; 2.17 stopped calling
        # the old one at all, silently crawling nothing.
        for request in self.plan_requests():
            yield request

    def _request(
        self, destination_id: str, prices_per_id: str, window: DateWindow
    ) -> scrapy.Request:
        return scrapy.Request(
            build_url(
                ALL,
                window,
                destination_id=destination_id,
                prices_per_id=prices_per_id,
            ),
            callback=self.parse,
            cb_kwargs={
                "destination_id": destination_id,
                "prices_per_id": prices_per_id,
                "window": window,
            },
            dont_filter=True,
        )

    def parse(
        self,
        response,
        destination_id: str,
        prices_per_id: str,
        window: DateWindow,
    ):
        """Rows for a window that fits, halves for one that does not."""
        if EMPTY_MARKER in response.text:
            # A valid window with no prices: no rows and no paginator either.
            # Common in the daily run, since markets do not report every day.
            return

        try:
            splits = next_windows(window, response.text)
        except WindowExhausted as error:
            # A single day still overflows. Nothing left to split, and writing
            # a truncated page would look like a complete one.
            self._record(destination_id, prices_per_id, window, error)
            return
        except UNREADABLE as error:
            self._record(destination_id, prices_per_id, window, error)
            return

        if splits:
            for half in splits:
                yield self._request(destination_id, prices_per_id, half)
            return

        try:
            yield from self._rows(response, destination_id, prices_per_id, window)
        except UNREADABLE as error:
            self._record(destination_id, prices_per_id, window, error)

    def _rows(self, response, destination_id: str, prices_per_id: str, window: DateWindow):
        context = QueryContext(
            product_id=ALL,
            origin_id=ALL,
            destination_id=destination_id,
            prices_per_id=prices_per_id,
            window=window,
            source_url=response.url,
            fetched_at=datetime.now(UTC),
        )
        table = parse_results(response.text)
        yield from to_raw_rows(build_records(table, context))

    def _record(
        self,
        destination_id: str,
        prices_per_id: str,
        window: DateWindow,
        error: Exception,
    ) -> None:
        failure = (
            f"destino={destination_id} precio={prices_per_id} "
            f"ventana={window.start:%Y-%m-%d}..{window.end:%Y-%m-%d}: "
            f"{type(error).__name__}: {error}"
        )
        self.failures.append(failure)
        self.logger.error(failure)
        crawler = getattr(self, "crawler", None)
        if crawler is not None:
            crawler.stats.inc_value("ingest/failed_windows")


def main(argv: list[str]) -> int:
    """Run the sweep and report in the exit code, for GitHub Actions."""
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--market", action="append", dest="markets")
    parser.add_argument(
        "--cache",
        action="store_true",
        help="servir de la caché HTTP lo ya descargado (para repetir sin "
        "volver a pedirle al SNIIM); apagada por defecto porque la corrida "
        "diaria tiene que ver los precios de hoy",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="barra de avance en vez del log; el log completo se va a "
        "out/daily.log. Para mirar una corrida, no para Actions",
    )
    args = parser.parse_args(argv)

    settings = get_project_settings()
    if args.cache:
        settings.set("HTTPCACHE_ENABLED", True)
    if args.progress:
        # The log has to leave the terminal before the console takes it over:
        # LOG_FILE is what removes Scrapy's console handler, and it is read
        # when logging is configured, so it cannot be set any later than this.
        settings.set("LOG_FILE", str(RAW_DIR.parent / "daily.log"))
        settings.set("PROGRESS_CONSOLE_ENABLED", True)

    process = CrawlerProcess(settings)
    crawler = process.create_crawler(DailySpider)
    process.crawl(crawler, days=args.days, destinations=args.markets)
    process.start()

    # The failures are written down, not just logged: the monitoring step runs
    # in another process and has to say which windows broke, not only how many.
    detail = list(getattr(crawler.spider, "failures", []))
    if detail:
        FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        FAILURES_PATH.write_text("\n".join(detail) + "\n", encoding="utf-8")
    elif FAILURES_PATH.exists():
        FAILURES_PATH.unlink()

    failures = crawler.stats.get_value("ingest/failed_windows", 0)
    if failures:
        print(f"{failures} ventana(s) fallaron; revisar el log", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
