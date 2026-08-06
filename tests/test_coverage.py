"""Offline checks for the historical-depth probe and the table it produces.

What can actually break here: reading a response as "no data" when it is
something else (that would shorten the backfill and lose years), and a
versioned coverage table that no longer agrees with the per-year evidence
next to it.
"""

import csv
from pathlib import Path

import pytest

from scraper.coverage import (
    BY_YEAR_PATH,
    SUMMARY_PATH,
    ProbeFailed,
    read_row_count,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sniim"


def load_fixture(name: str) -> str:
    # Decoded as UTF-8 because that is what the Content-Type header says; the
    # fixtures are stored as raw bytes.
    return (FIXTURES / f"{name}.html").read_bytes().decode("utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_probe_reports_the_full_result_set_size():
    rows = read_row_count(load_fixture("probe_row_count"))
    assert rows > 1, "RegistrosPorPagina=-1 must report the total, not the page"


def test_year_without_data_reads_as_zero():
    assert read_row_count(load_fixture("probe_no_records")) == 0


def test_rejected_query_is_not_read_as_an_empty_year():
    with pytest.raises(ProbeFailed):
        read_row_count(load_fixture("rejected_all_criteria"))


def test_ordinary_response_is_not_read_as_a_count():
    # A positive denominator means the probe value never reached the server,
    # so the number in the label is a page count, not a row count.
    with pytest.raises(ProbeFailed):
        read_row_count(load_fixture("range_product_fixed"))


def test_every_market_has_a_coverage_row():
    destinations = read_csv(Path("data/catalogs/destinations.csv"))
    summary = read_csv(SUMMARY_PATH)
    assert [row["destination_id"] for row in summary] == [
        row["destination_id"] for row in destinations
    ]


def test_summary_matches_the_per_year_evidence():
    by_year: dict[str, dict[int, int]] = {}
    for row in read_csv(BY_YEAR_PATH):
        by_year.setdefault(row["destination_id"], {})[int(row["year"])] = int(row["rows"])

    for row in read_csv(SUMMARY_PATH):
        years = by_year[row["destination_id"]]
        with_data = sorted(year for year, rows in years.items() if rows)
        assert row["years_with_data"] == str(len(with_data))
        assert row["total_rows"] == str(sum(years.values()))
        if not with_data:
            assert row["first_year"] == "" and row["last_year"] == ""
            assert row["gap_years"] == ""
            continue
        assert row["first_year"] == str(with_data[0])
        assert row["last_year"] == str(with_data[-1])
        gaps = [year for year in range(with_data[0], with_data[-1] + 1) if year not in with_data]
        assert row["gap_years"] == " ".join(str(year) for year in gaps)


def test_the_scan_reached_back_past_the_first_year_with_data():
    """The floor must be measured, not clipped by where the scan started."""
    years = {int(row["year"]) for row in read_csv(BY_YEAR_PATH)}
    first_years = {
        int(row["first_year"]) for row in read_csv(SUMMARY_PATH) if row["first_year"]
    }
    assert min(years) < min(first_years)
