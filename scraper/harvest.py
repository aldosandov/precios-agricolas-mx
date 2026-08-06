"""Turning one response into rows, the same way whichever sweep asked for it.

Two spiders ask the SNIIM for different reasons — one wants today, the other
wants 1998 — but from the response onwards there is nothing to tell apart: the
same header-driven parser, the same contract, the same raw-layer row (PRD
§8.6). This is that shared half, kept out of both spiders so neither can drift
from the other.

What is *not* here is what actually differs: which windows to ask for, and what
to do with a window that breaks. A daily sweep loses a market for a day; a
backfill loses a quarter it will retry. Same failure, different consequence.
"""

from __future__ import annotations

from datetime import UTC, datetime

from scraper.contract import ContractBreach, QueryContext, build_records
from scraper.parsers.results import ParseError, parse_results
from scraper.pipelines.ndjson import UnwritableRow, to_raw_rows
from scraper.query import ALL, DateWindow, NoPaginator, QueryRejected, WindowExhausted

# Everything that means "this response is not usable". Worth losing one window
# over, never worth guessing past: a response read wrong is a gap nobody sees.
# `WindowExhausted` belongs here too — a single day that still overflows has
# nothing left to split, and writing a truncated page would look complete.
UNREADABLE = (
    ParseError,
    ContractBreach,
    UnwritableRow,
    QueryRejected,
    NoPaginator,
    WindowExhausted,
)


class LostRequest(Exception):
    """A request that never produced a response, after every retry allowed."""


def lost_request(failure) -> LostRequest:
    """Why a request never came back, in the words the failure file will carry.

    Scrapy gives up after `RETRY_TIMES`, bumps `retry/max_reached` and moves on,
    so without an errback the window is not in `failures`, not in the file the
    monitor reads, and not in the alert. The run ends green having quietly
    skipped a market.
    """
    # HttpError carries the response it refused; a transport error does not.
    response = getattr(failure.value, "response", None)
    detail = f"HTTP {response.status}" if response is not None else repr(failure.value)
    # Not RETRY_TIMES: robots.txt and a few others are never retried, and
    # claiming four attempts for a single one would send the next reader
    # looking at the wrong thing.
    attempts = failure.request.meta.get("retry_times", 0) + 1
    return LostRequest(f"{detail} tras {attempts} intento(s)")


def rows_from(
    response,
    *,
    destination_id: str,
    prices_per_id: str,
    window: DateWindow,
) -> list[dict]:
    """The raw-layer rows one response is worth. Raises on anything unreadable."""
    context = QueryContext(
        product_id=ALL,
        origin_id=ALL,
        destination_id=destination_id,
        prices_per_id=prices_per_id,
        window=window,
        source_url=response.url,
        fetched_at=datetime.now(UTC),
    )
    return to_raw_rows(build_records(parse_results(response.text), context))
