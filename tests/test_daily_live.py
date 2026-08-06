"""Live end to end: one real market, through the whole ingest, onto disk (ALD-19).

The offline suite proves the pieces against saved HTML. This one proves they
still fit together against the source as it is today: the URL the spider
builds, the header-driven parser, the contract, the §10 row and the partition
layout, in that order and with nothing stubbed.

It is the criterion of ALD-19 — running against a real market produces valid
NDJSON that passes contract validation — so it hits the network on purpose.
"""

import json
import urllib.request
from datetime import UTC, date, datetime

import pytest

from scraper.contract import QueryContext, build_records
from scraper.parsers.results import parse_results
from scraper.pipelines.ndjson import RAW_SCHEMA, to_raw_rows, write_partitions
from scraper.query import ALL, DateWindow, build_url, is_truncated

# Central de Abasto de Puebla over three days of July 2026: the same window the
# fixtures were captured from, so the window is known to hold prices.
PUEBLA = "210"
WINDOW = DateWindow(date(2026, 7, 1), date(2026, 7, 3))

TIMEOUT = 180


@pytest.fixture(scope="module")
def response() -> tuple[str, str]:
    url = build_url(ALL, WINDOW, destination_id=PUEBLA)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return url, resp.read().decode(charset, errors="replace")
    except OSError as error:  # 503 under load, DNS, timeouts
        pytest.skip(f"SNIIM no respondió: {error}")


def test_a_real_market_window_fits_in_one_page(response):
    """If it did not, the spider would split it instead of writing anything."""
    _, html = response

    assert not is_truncated(html)


def test_a_real_market_window_passes_contract_validation(response):
    url, html = response

    records = build_records(
        parse_results(html),
        QueryContext(
            product_id=ALL,
            origin_id=ALL,
            destination_id=PUEBLA,
            prices_per_id="2",
            window=WINDOW,
            source_url=url,
            fetched_at=datetime.now(UTC),
        ),
    )

    assert records
    assert len({record.natural_key for record in records}) == len(records)


def test_a_real_market_window_lands_as_valid_ndjson(response, tmp_path):
    url, html = response
    records = build_records(
        parse_results(html),
        QueryContext(
            product_id=ALL,
            origin_id=ALL,
            destination_id=PUEBLA,
            prices_per_id="2",
            window=WINDOW,
            source_url=url,
            fetched_at=datetime.now(UTC),
        ),
    )

    paths = write_partitions(to_raw_rows(records), tmp_path)

    lines = [
        json.loads(line)
        for path in paths
        for line in path.read_text("utf-8").splitlines()
    ]
    assert lines
    assert all(set(line) == set(RAW_SCHEMA) for line in lines)
    assert all(line["destino_id"] == 210 for line in lines)
    assert all(path.parent.name.startswith("fecha=2026-07-0") for path in paths)
