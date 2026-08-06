"""The backfill's grid of work and its checkpoint (ALD-26).

The backfill runs by hand over several days, so stopping and starting is how it
normally runs. What is pinned here is what makes that safe: the grid is derived
from what the source actually has and not from the calendar, a block only
leaves the queue when it is genuinely covered, and a quarter that broke
anywhere is retried whole rather than recorded as covered.

Runs offline against the versioned ALD-14 tables and an in-memory database.
"""

from datetime import date

import pytest

from scraper.backfill_state import (
    BlockLedger,
    Counts,
    CoverageStore,
    plan_grid,
    years_with_data,
)

TODAY = date(2026, 8, 6)
PUEBLA = "210"

# Measured in ALD-14: two markets never published a row.
NEVER_REPORTED = {"71", "122"}


@pytest.fixture
def store() -> CoverageStore:
    with CoverageStore(":memory:") as opened:
        yield opened


@pytest.fixture
def grid() -> list:
    return plan_grid(today=TODAY)


def _one_market(today: date = TODAY) -> list:
    return plan_grid(today=today, destinations=[PUEBLA])


# --- the grid: what the source has, not what the calendar has ---


def test_the_grid_is_every_market_quarter_and_price_mode_with_data(grid):
    assert len(grid) == 9628


def test_markets_that_never_published_are_not_in_the_grid(grid):
    """Being in the catalog is not having data. Two of the 49 never did."""
    assert {block.destination_id for block in grid}.isdisjoint(NEVER_REPORTED)
    assert len({block.destination_id for block in grid}) == 47


def test_years_the_source_never_had_are_not_asked_for(grid):
    """The floor is per market: 32 reach 1998, fifteen start later. Asking
    every market from 1998 would spend thousands of requests on nothing."""
    tijuana = [block for block in grid if block.destination_id == "33"]

    assert min(block.start.year for block in tijuana) == 2002


def test_a_hole_in_the_middle_of_a_market_is_skipped_too(grid):
    """Villahermosa reported, went quiet 1999-2001 and came back. An empty
    window there is the source, not a failure, so it is never queried."""
    villahermosa = {block.start.year for block in grid if block.destination_id == "270"}

    assert villahermosa.isdisjoint({1999, 2000, 2001})
    assert 1998 in villahermosa and 2002 in villahermosa


def test_the_running_quarter_stops_at_today_and_the_rest_of_the_year_is_not_asked(grid):
    """Dates the source has not reached cost a request and return nothing."""
    latest = max(block.end for block in grid)

    assert latest == TODAY


def test_the_grid_is_chronological_across_every_market(grid):
    """Sweeping by market would make each load's MERGE span the whole history,
    and would leave an abandoned run with a few complete markets instead of a
    complete history up to some date."""
    assert grid == sorted(grid)
    assert [block.start for block in grid] == sorted(block.start for block in grid)


def test_every_quarter_is_asked_once_per_price_mode():
    """The response does not say which mode answered, so both are recorded."""
    quarter = [block for block in _one_market() if block.quarter == "2011-Q3"]

    assert {block.price_mode for block in quarter} == {
        "kilogramo_calculado",
        "presentacion_comercial",
    }
    assert len(quarter) == 2


def test_a_block_carries_what_the_query_and_the_console_need():
    block = next(b for b in _one_market() if b.quarter == "2011-Q3")

    assert block.window.start == date(2011, 7, 1)
    assert block.window.end == date(2011, 9, 30)
    assert block.prices_per_id in {"1", "2"}
    assert "2011-Q3" in block.label and PUEBLA in block.label


def test_the_years_come_from_the_probe_and_not_from_a_guess():
    years = years_with_data()

    assert set(years).isdisjoint(NEVER_REPORTED)
    assert min(min(found) for found in years.values()) == 1998


# --- resuming: only genuinely covered blocks leave the queue ---


def test_an_untouched_grid_is_entirely_pending(store, grid):
    assert store.pending(grid) == grid


def test_a_finished_block_does_not_come_back(store, grid):
    block = grid[0]
    store.open_block(block)
    store.close_block(block, rows=1200, requests=1)

    assert block not in store.pending(grid)
    assert len(store.pending(grid)) == len(grid) - 1


def test_an_interrupted_block_comes_back(store, grid):
    """A session killed mid-block leaves it open. Open is not covered."""
    block = grid[0]
    store.open_block(block)

    assert block in store.pending(grid)


def test_a_failed_block_comes_back(store, grid):
    block = grid[0]
    store.open_block(block)
    store.fail_block(block, "503 tras 4 intentos")

    assert block in store.pending(grid)
    assert store.counts().failed == 1


def test_a_run_that_died_resumes_without_repeating_what_it_covered(store, grid):
    """The criterion the whole issue exists for."""
    for block in grid[:100]:
        store.open_block(block)
        store.close_block(block, rows=10, requests=1)
    store.open_block(grid[100])  # the one in flight when the laptop died

    resumed = store.pending(grid)

    assert resumed == grid[100:]
    assert store.counts().done == 100


def test_a_block_closed_for_a_shorter_window_is_asked_again(store):
    """The running quarter grows a day at a time. A block covered up to
    yesterday is not covered up to today, and says so without a special case."""
    yesterday = _one_market(date(2026, 8, 5))
    store.open_block(yesterday[-1])
    store.close_block(yesterday[-1], rows=10, requests=1)

    today = _one_market(TODAY)

    assert today[-1] in store.pending(today)


def test_retrying_a_block_counts_the_attempt_and_clears_the_last_error(store, grid):
    block = grid[0]
    store.open_block(block)
    store.fail_block(block, "503")
    store.open_block(block)

    row = store.connection.execute("SELECT * FROM cobertura").fetchone()
    assert row["intentos"] == 2
    assert row["error"] is None


def test_the_checkpoint_survives_the_process_that_wrote_it(tmp_path, grid):
    """Written on close, not on exit: a kill -9 costs the block in flight."""
    path = tmp_path / "cobertura.sqlite"
    first = CoverageStore(path)
    first.open_block(grid[0])
    first.close_block(grid[0], rows=5, requests=1)
    del first  # no close(), no flush, nothing tidy

    with CoverageStore(path) as second:
        assert grid[0] not in second.pending(grid)


# --- the ledger: a quarter is one unit of work but many requests ---


def test_a_block_that_never_split_closes_on_its_only_response(store, grid):
    ledger = BlockLedger(store)
    block = grid[0]

    ledger.opened(block)

    assert ledger.finished(block, rows=300) is True
    assert block not in store.pending(grid)


def test_a_block_split_into_four_closes_once_with_the_rows_of_all_four(store, grid):
    """Truncation halves the window, and a half may halve again. Nothing knows
    the block is done except the count of requests in flight reaching zero."""
    ledger = BlockLedger(store)
    block = grid[0]
    ledger.opened(block)

    ledger.split(block)  # the quarter became two halves
    ledger.split(block)  # one half became two sixths
    ledger.split(block)  # so did the other
    for rows in (100, 200, 300):
        assert ledger.finished(block, rows=rows) is False
    assert ledger.finished(block, rows=400) is True

    row = store.connection.execute("SELECT * FROM cobertura").fetchone()
    assert row["estado"] == "hecho"
    assert row["filas"] == 1000
    # One for the quarter plus two per split: what the SNIIM was actually asked.
    assert row["peticiones"] == 7


def test_one_unreadable_window_taints_the_whole_quarter(store, grid):
    """Recording a quarter as covered when part of it was never read is the one
    failure that leaves no trace. Retrying the quarter only costs requests."""
    ledger = BlockLedger(store)
    block = grid[0]
    ledger.opened(block)
    ledger.split(block)

    ledger.failed(block, "encabezado desconocido: Precio Modal")
    assert ledger.finished(block, rows=500) is True

    row = store.connection.execute("SELECT * FROM cobertura").fetchone()
    assert row["estado"] == "fallido"
    assert "Precio Modal" in row["error"]
    assert block in store.pending(grid)


def test_a_block_that_breaks_does_not_stop_the_others(store, grid):
    ledger = BlockLedger(store)
    broken, healthy = grid[0], grid[1]
    ledger.opened(broken)
    ledger.opened(healthy)

    ledger.failed(broken, "503 tras 4 intentos")
    ledger.finished(healthy, rows=42)

    assert store.counts() == Counts(done=1, failed=1, open=0)
    assert store.pending(grid)[0] == broken


def test_the_failures_read_as_lines_and_not_as_rows(store, grid):
    ledger = BlockLedger(store)
    ledger.opened(grid[0])
    ledger.failed(grid[0], "503 tras 4 intentos")

    line = store.failures()[0]

    assert grid[0].destination_id in line
    assert "503 tras 4 intentos" in line
