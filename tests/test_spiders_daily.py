"""The daily incremental spider (ALD-19).

The sweep pins the market: 49 destinations, every product and origin, both
price modes, over a window that reaches several days back because the source
sometimes publishes late and the load is idempotent.

What is pinned here is the plan (which requests go out), the subdivision (a
truncated response is halved, never paginated) and the isolation: one market
that breaks must not cost the other 48 their day, but the run still has to
end in red.

Runs offline against the ALD-13 fixtures; the reactor never starts.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

from scrapy.http import HtmlResponse, Request, Response
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.internet.error import TimeoutError as TransportTimeout
from twisted.python.failure import Failure

from scraper.fixtures import FIXTURES_DIR
from scraper.query import DateWindow
from scraper.spiders.daily import DailySpider

REPO = Path(__file__).resolve().parent.parent

WINDOW = DateWindow(date(2026, 7, 1), date(2026, 7, 3))
PUEBLA = "210"


def _spider(**kwargs) -> DailySpider:
    return DailySpider(destinations=[PUEBLA], **kwargs)


def _response(fixture: str, window: DateWindow = WINDOW):
    body = (FIXTURES_DIR / f"{fixture}.html").read_bytes()
    request = Request(
        "https://example.test/results",
        cb_kwargs={
            "destination_id": PUEBLA,
            "prices_per_id": "2",
            "window": window,
        },
    )
    return HtmlResponse(request.url, body=body, encoding="utf-8", request=request)


def _parse(fixture: str, spider: DailySpider, window: DateWindow = WINDOW) -> list:
    response = _response(fixture, window)
    return list(spider.parse(response, **response.request.cb_kwargs))


# --- the plan: 49 markets, two price modes ---


def test_every_market_is_asked_for_both_price_modes():
    requests = DailySpider(days=1).plan_requests()

    modes = {request.cb_kwargs["prices_per_id"] for request in requests}
    markets = {request.cb_kwargs["destination_id"] for request in requests}
    assert modes == {"1", "2"}
    assert len(markets) == 43
    assert len(requests) == 86


def test_markets_that_stopped_reporting_are_not_asked_at_all():
    """Six of the 49 have not published in years; asking them daily is a tenth
    of the sweep spent on rows that do not exist."""
    markets = {r.cb_kwargs["destination_id"] for r in DailySpider(days=1).plan_requests()}

    assert markets.isdisjoint({"71", "122", "102", "111", "112", "231"})


def test_the_planned_requests_are_what_scrapy_actually_starts_with():
    """Scrapy 2.17 ignores start_requests(); a spider that only defines it
    crawls nothing and says 'finished'."""

    async def drain():
        return [request async for request in _spider(days=1).start()]

    assert len(asyncio.run(drain())) == 2


def test_the_window_reaches_several_days_back_by_default():
    spider = _spider()

    assert spider.window.days > 1
    assert spider.window.end == date.today()


def test_the_query_pins_the_market_and_leaves_product_and_origin_open():
    request = _spider(days=1).plan_requests()[0]

    assert f"DestinoId={PUEBLA}" in request.url
    assert "ProductoId=-1" in request.url
    assert "OrigenId=-1" in request.url


# --- rows out ---


def test_a_response_becomes_raw_layer_rows():
    spider = _spider()

    items = _parse("range_destination_fixed", spider)

    assert len(items) == 192
    assert items[0]["destino_id"] == 210
    assert items[0]["tipo_precio"] == "kilogramo_calculado"
    assert items[0]["fecha"] == "2026-07-01"


def test_a_window_without_prices_yields_nothing_and_is_not_a_failure():
    spider = _spider()

    assert _parse("probe_no_records", spider) == []
    assert spider.failures == []


# --- truncation: halve the window, never paginate ---


def test_a_truncated_response_is_split_instead_of_paginated():
    spider = _spider()

    yielded = _parse("paginated_overflow", spider)

    windows = [request.cb_kwargs["window"] for request in yielded]
    assert windows == [
        DateWindow(date(2026, 7, 1), date(2026, 7, 2)),
        DateWindow(date(2026, 7, 3), date(2026, 7, 3)),
    ]
    assert not any(isinstance(item, dict) for item in yielded)


def test_a_split_request_keeps_the_market_and_the_price_mode():
    spider = _spider()

    request = _parse("paginated_overflow", spider)[0]

    assert request.cb_kwargs["destination_id"] == PUEBLA
    assert request.cb_kwargs["prices_per_id"] == "2"
    assert "fechaInicio=01%2F07%2F2026" in request.url


# --- isolation: one market must not cost the others their day ---


def test_a_rejected_query_is_recorded_and_does_not_raise():
    spider = _spider()

    assert _parse("rejected_all_criteria", spider) == []
    assert len(spider.failures) == 1
    assert PUEBLA in spider.failures[0]


def test_an_unknown_header_stops_that_market_only():
    spider = _spider()
    response = _response("range_destination_fixed")
    broken = response.replace(body=response.text.replace("Precio Frec", "Precio Modal"))

    items = list(spider.parse(broken, **response.request.cb_kwargs))

    assert items == []
    assert "Precio Modal" in spider.failures[0]


def test_a_single_day_that_still_overflows_is_a_failure_not_a_silent_loss():
    """Nothing left to split: the response is truncated and cannot be fixed."""
    spider = _spider()
    day = date(2026, 7, 1)

    yielded = _parse("paginated_overflow", spider, window=DateWindow(day, day))

    assert yielded == []
    assert len(spider.failures) == 1


# --- requests that never come back (ALD-54) ---


def _failed(spider, error, *, retries=0) -> Failure:
    request = spider.plan_requests()[0]
    request.meta["retry_times"] = retries
    failure = Failure(error)
    failure.request = request
    return failure


def test_every_request_carries_an_errback():
    """Without one, a request that gives up is a stat and nothing else."""
    spider = _spider(days=1)

    assert all(request.errback == spider.errback for request in spider.plan_requests())


def test_a_request_that_gave_up_is_recorded_with_its_status_and_its_window():
    spider = _spider(days=1)
    response = Response("https://example.test/results", status=503)

    spider.errback(_failed(spider, HttpError(response), retries=3))

    assert len(spider.failures) == 1
    assert "HTTP 503" in spider.failures[0]
    assert "tras 4 intento(s)" in spider.failures[0]
    assert f"destino={PUEBLA}" in spider.failures[0]


def test_a_transport_error_is_recorded_too_even_without_a_response():
    """A timeout loses the window exactly as thoroughly as a 503 does."""
    spider = _spider(days=1)

    spider.errback(_failed(spider, TransportTimeout("timed out")))

    assert "TimeoutError" in spider.failures[0]


def test_the_attempts_reported_are_the_ones_that_happened():
    """Robots and a few others are never retried; claiming four attempts for
    one would send the next reader looking at the wrong thing."""
    spider = _spider(days=1)

    spider.errback(_failed(spider, TransportTimeout("timed out")))

    assert "tras 1 intento(s)" in spider.failures[0]


def test_a_run_where_every_request_gives_up_ends_red_with_every_window_named(tmp_path):
    """The whole point, end to end and through the real reactor: a source that
    answers 503 to everything must not produce a green run and an empty day."""
    (tmp_path / "fake_downloader.py").write_text(
        textwrap.dedent("""
            from scrapy.http import Response


            class Always503:
                \"\"\"Answers every request without touching the network.\"\"\"

                def process_request(self, request, spider):
                    return Response(request.url, status=503, request=request)
        """),
        encoding="utf-8",
    )
    (tmp_path / "corrida.py").write_text(
        textwrap.dedent("""
            import sys
            from pathlib import Path

            import scrapy.utils.project as project

            out = Path(sys.argv[1])
            loaded = project.get_project_settings()
            loaded.set("DOWNLOADER_MIDDLEWARES", {"fake_downloader.Always503": 1})
            loaded.set("RETRY_TIMES", 1)
            loaded.set("DOWNLOAD_DELAY", 0)
            loaded.set("AUTOTHROTTLE_ENABLED", False)
            loaded.set("ROBOTSTXT_OBEY", False)
            loaded.set("RAW_OUTPUT_DIR", str(out / "raw"))
            loaded.set("LOG_LEVEL", "ERROR")
            project.get_project_settings = lambda: loaded

            from scraper.spiders import daily

            daily.FAILURES_PATH = out / "failures.txt"
            sys.exit(daily.main(["--days", "1", "--market", "210"]))
        """),
        encoding="utf-8",
    )

    run = subprocess.run(
        [sys.executable, str(tmp_path / "corrida.py"), str(tmp_path)],
        cwd=REPO,
        env={"PYTHONPATH": f"{tmp_path}{os.pathsep}{REPO}", "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.returncode == 1, run.stderr[-2000:]
    lines = (tmp_path / "failures.txt").read_text(encoding="utf-8").splitlines()
    # Both price modes of the one market asked for, neither lost in silence.
    assert len(lines) == 2
    assert all("HTTP 503" in line and f"destino={PUEBLA}" in line for line in lines)


# --- the run's verdict ---


def test_a_clean_run_reports_no_failures():
    spider = _spider()

    _parse("range_destination_fixed", spider)

    assert spider.failures == []


# --- what lands on disk ---


def test_the_items_write_as_valid_ndjson_partitioned_by_date(tmp_path):
    from scraper.pipelines.ndjson import write_partitions

    spider = _spider()
    paths = write_partitions(_parse("range_destination_fixed", spider), tmp_path)

    lines = [json.loads(line) for path in paths for line in path.read_text("utf-8").splitlines()]
    assert len(lines) == 192
    assert {path.parent.name for path in paths} == {
        "fecha=2026-07-01",
        "fecha=2026-07-02",
        "fecha=2026-07-03",
    }
