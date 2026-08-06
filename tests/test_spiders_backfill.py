"""The historical sweep by quarterly blocks (ALD-30).

The spider itself is thin — the queries, the parser and the bookkeeping all
exist already — so what is pinned here is the wiring, which is where it can go
wrong silently: that the queue is what the checkpoint says is pending, that a
block closes exactly when its last request comes back, and that a broken window
takes its quarter down with it instead of leaving it recorded as covered.

Runs offline against the ALD-13 fixtures; the reactor never starts.
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest
from scrapy.http import HtmlResponse, Request, Response
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure

from scraper.backfill_state import CoverageStore, plan_grid
from scraper.fixtures import FIXTURES_DIR
from scraper.pipelines.ndjson import PartitionWriter
from scraper.spiders.backfill import BackfillSpider

TODAY = date(2026, 8, 6)
PUEBLA = "210"
Q3_2011 = (date(2011, 7, 1), date(2011, 9, 30))


@pytest.fixture
def store() -> CoverageStore:
    with CoverageStore(":memory:") as opened:
        yield opened


@pytest.fixture
def quarter() -> list:
    """The two blocks of one market and one quarter: one per price mode."""
    return [
        block
        for block in plan_grid(today=TODAY, destinations=[PUEBLA])
        if (block.start, block.end) == Q3_2011
    ]


def _spider(store, blocks, **kwargs) -> BackfillSpider:
    return BackfillSpider(store=store, grid=blocks, **kwargs)


def _response(request, fixture: str):
    body = (FIXTURES_DIR / f"{fixture}.html").read_bytes()
    return HtmlResponse(request.url, body=body, encoding="utf-8", request=request)


def _parse(spider, request, fixture: str) -> list:
    response = _response(request, fixture)
    return list(spider.parse(response, **request.cb_kwargs))


def _started(spider) -> list:
    async def drain():
        return [request async for request in spider.start()]

    return asyncio.run(drain())


# --- the queue is the checkpoint ---


def test_the_queue_is_what_the_checkpoint_says_is_pending(store, quarter):
    spider = _spider(store, quarter)

    assert spider.blocks == quarter
    assert spider.progress().total == 2


def test_a_covered_block_is_not_asked_for_again(store, quarter):
    store.open_block(quarter[0])
    store.close_block(quarter[0], rows=10, requests=1)

    spider = _spider(store, quarter)

    assert spider.blocks == [quarter[1]]
    # Already covered still counts as progress: the bar measures the grid, not
    # the session.
    assert spider.progress().done == 1


def test_a_session_can_be_cut_short_and_the_rest_waits(store, quarter):
    """An hour free, five hundred blocks, stop. That is how this runs."""
    spider = _spider(store, quarter, limit=1)

    assert spider.blocks == [quarter[0]]
    assert spider.progress().total == 2


def test_a_date_filter_selects_whole_blocks_and_never_clips_them(store):
    """A block covering less than the grid asks for would never match it again
    and would come back on every session."""
    spider = _spider(
        store,
        plan_grid(today=TODAY, destinations=[PUEBLA]),
        since=date(2011, 7, 1),
        until=date(2011, 9, 30),
    )

    assert {(block.start, block.end) for block in spider.blocks} == {Q3_2011}
    assert len(spider.blocks) == 2


def test_a_block_is_opened_when_its_request_goes_out(store, quarter):
    spider = _spider(store, quarter)

    requests = _started(spider)

    assert len(requests) == 2
    assert store.counts().open == 2


def test_no_more_blocks_are_opened_than_can_be_in_flight(store):
    """Scrapy pulls from `start()` as fast as the scheduler will take it —
    measured: 226 blocks opened while 4 finished. Without a ceiling the whole
    grid would be written as `abierto` inside the first minute, and `abierto`
    is supposed to mean a session died holding this block."""
    blocks = plan_grid(today=TODAY, destinations=[PUEBLA])[:40]
    spider = _spider(store, blocks, max_open_blocks=4)

    async def drain_until_it_stalls():
        started, requests = [], spider.start()
        try:
            while True:
                started.append(await asyncio.wait_for(requests.__anext__(), timeout=0.5))
        except (TimeoutError, StopAsyncIteration):
            return started

    assert len(asyncio.run(drain_until_it_stalls())) == 4
    assert store.counts().open == 4


def test_the_query_pins_the_market_and_the_price_mode(store, quarter):
    spider = _spider(store, quarter)

    request = _started(spider)[0]

    assert f"DestinoId={PUEBLA}" in request.url
    assert "fechaInicio=01%2F07%2F2011" in request.url
    assert "fechaFinal=30%2F09%2F2011" in request.url
    assert "ProductoId=-1" in request.url


# --- a block closes when its last request comes back ---


def test_a_block_that_never_split_closes_on_its_only_response(store, quarter):
    spider = _spider(store, quarter)
    request = _started(spider)[0]

    items = _parse(spider, request, "range_destination_fixed")

    assert len(items) == 192
    assert quarter[0] not in store.pending(quarter)
    assert store.counts().done == 1


def test_a_truncated_response_is_split_instead_of_paginated(store, quarter):
    spider = _spider(store, quarter)
    request = _started(spider)[0]

    yielded = _parse(spider, request, "paginated_overflow")

    assert all(isinstance(item, Request) for item in yielded)
    assert len(yielded) == 2
    # Still one request out plus the two it became, so the block is not done.
    assert spider.ledger.in_flight(quarter[0]) == 2
    assert quarter[0] in store.pending(quarter)


def test_a_split_block_closes_only_when_every_half_came_back(store, quarter):
    spider = _spider(store, quarter)
    block = quarter[0]
    halves = _parse(spider, _started(spider)[0], "paginated_overflow")

    _parse(spider, halves[0], "range_destination_fixed")
    assert block in store.pending(quarter)

    _parse(spider, halves[1], "range_destination_fixed")

    assert block not in store.pending(quarter)
    row = store.connection.execute("SELECT * FROM cobertura").fetchone()
    assert row["filas"] == 384
    assert row["peticiones"] == 3


def test_a_quarter_the_market_did_not_report_closes_empty_and_is_not_a_failure(
    store, quarter
):
    """The probe measures a year: a market with data in 1998 did not
    necessarily report every quarter of it."""
    spider = _spider(store, quarter)
    request = _started(spider)[0]

    assert _parse(spider, request, "probe_no_records") == []
    assert spider.failures == []
    assert quarter[0] not in store.pending(quarter)


# --- a broken window takes its quarter with it ---


def test_an_unreadable_response_fails_the_whole_quarter(store, quarter):
    spider = _spider(store, quarter)
    request = _started(spider)[0]
    response = _response(request, "range_destination_fixed")
    broken = response.replace(body=response.text.replace("Precio Frec", "Precio Modal"))

    assert list(spider.parse(broken, **request.cb_kwargs)) == []
    assert "Precio Modal" in spider.failures[0]
    assert quarter[0] in store.pending(quarter)
    assert store.counts().failed == 1


def test_a_half_that_breaks_taints_the_quarter_even_if_the_other_half_worked(
    store, quarter
):
    """Recording a quarter as covered when part of it was never read is the one
    failure that leaves no trace. Retrying it only costs requests."""
    spider = _spider(store, quarter)
    block = quarter[0]
    halves = _parse(spider, _started(spider)[0], "paginated_overflow")

    _parse(spider, halves[0], "rejected_all_criteria")
    _parse(spider, halves[1], "range_destination_fixed")

    assert block in store.pending(quarter)
    assert store.counts().failed == 1


def test_a_request_that_gave_up_fails_its_quarter_instead_of_hanging_it(store, quarter):
    """Without this the block's in-flight count never reaches zero and the
    quarter stays `abierto` forever, held in every session that follows."""
    spider = _spider(store, quarter)
    request = _started(spider)[0]
    failure = Failure(HttpError(Response(request.url, status=503)))
    failure.request = request

    spider.errback(failure)

    assert "HTTP 503" in spider.failures[0]
    assert store.counts().open == 1  # the other block, never answered
    assert store.counts().failed == 1
    assert quarter[0] in store.pending(quarter)


def test_one_broken_block_does_not_stop_the_other(store, quarter):
    spider = _spider(store, quarter)
    first, second = _started(spider)

    _parse(spider, first, "rejected_all_criteria")
    _parse(spider, second, "range_destination_fixed")

    assert store.pending(quarter) == [quarter[0]]
    assert spider.progress().done == 2  # both resolved, one of them badly


# --- handing a finished quarter over (ALD-55) ---


def _watched(store, blocks, **kwargs):
    spider = _spider(store, blocks, **kwargs)
    handed: list = []
    spider.on_quarter = lambda start, end: handed.append((start, end))
    return spider, handed


def test_a_quarter_is_handed_over_when_its_last_block_resolves(store, quarter):
    """Its dates are complete on disk at that moment and nowhere else: ~10.7 GB
    of NDJSON for the whole history, on a personal laptop."""
    spider, handed = _watched(store, quarter)
    first, second = _started(spider)

    _parse(spider, first, "range_destination_fixed")
    assert handed == []

    _parse(spider, second, "range_destination_fixed")

    assert handed == [Q3_2011]


def test_a_quarter_is_handed_over_once_and_not_per_block(store, quarter):
    spider, handed = _watched(store, quarter)
    for request in _started(spider):
        _parse(spider, request, "range_destination_fixed")

    assert len(handed) == 1


def test_a_block_that_failed_still_completes_its_quarter(store, quarter):
    """What was captured belongs in BigQuery; the block comes back next session
    and merges on top."""
    spider, handed = _watched(store, quarter)
    first, second = _started(spider)

    _parse(spider, first, "rejected_all_criteria")
    _parse(spider, second, "range_destination_fixed")

    assert handed == [Q3_2011]


def test_a_quarter_cut_short_by_limit_is_handed_over_with_what_it_got(store, quarter):
    """A session owes only the blocks it scheduled, so half a quarter is still
    handed over. That is safe and deliberate: the load is idempotent, and the
    markets left out are fetched next session and merged on top. Holding the
    partition back instead would be the thing that grows the disk."""
    spider, handed = _watched(store, quarter, limit=1)

    _parse(spider, _started(spider)[0], "range_destination_fixed")

    assert spider.blocks == quarter[:1]
    assert handed == [Q3_2011]


def test_each_quarter_is_handed_over_separately(store):
    blocks = [
        block
        for block in plan_grid(today=TODAY, destinations=[PUEBLA])
        if block.start.year == 2011
    ]
    spider, handed = _watched(store, blocks)

    for request in _started(spider):
        _parse(spider, request, "range_destination_fixed")

    assert [start for start, _ in handed] == [
        date(2011, 1, 1),
        date(2011, 4, 1),
        date(2011, 7, 1),
        date(2011, 10, 1),
    ]


def test_a_quarter_waits_for_the_pipeline_before_it_is_loaded(store, quarter):
    """Scrapy processes items after the callback's generator has ended, so
    every block resolving does not mean every row is written. Measured when it
    loaded anyway: 7 585 rows into BigQuery of the 7 770 on disk."""
    from scraper.spiders.backfill import QuarterLoader

    spider = _spider(store, quarter)
    writer = PartitionWriter(Path("/dev/null/never-written"))
    loader = QuarterLoader(spider, writer=writer)
    spider.harvested[date(2011, 7, 1)] = 384

    assert loader._settled(*Q3_2011) is False

    writer.rows[date(2011, 7, 15)] = 384

    assert loader._settled(*Q3_2011) is True


def test_rows_outside_the_range_do_not_count_as_the_quarter_catching_up(store, quarter):
    from scraper.spiders.backfill import QuarterLoader

    spider = _spider(store, quarter)
    writer = PartitionWriter(Path("/dev/null/never-written"))
    writer.rows[date(2011, 10, 1)] = 500
    spider.harvested[date(2011, 7, 1)] = 384

    assert QuarterLoader(spider, writer=writer)._settled(*Q3_2011) is False
