"""Window sizing and overflow detection (ALD-15).

Two things must not go wrong here. A gap or an overlap in the subdivision
silently loses or double-counts prices, and a missed truncation signal reads
a cut response as a complete one. Both fail quietly, so both are pinned.

Runs offline against the ALD-13 fixtures.
"""

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from scraper.fixtures import FIXTURES_DIR
from scraper.query import (
    ALL,
    DateWindow,
    NoPaginator,
    QueryRejected,
    RESULTS_URL,
    WindowExhausted,
    build_url,
    is_truncated,
    next_windows,
    page_count,
    quarterly_windows,
)


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / f"{name}.html").read_bytes().decode("utf-8")


def _days(window: DateWindow) -> list[date]:
    return [window.start + timedelta(days=offset) for offset in range(window.days)]


def _covers_exactly_once(windows, expected_start: date, expected_end: date) -> bool:
    covered = [day for window in windows for day in _days(window)]
    span = (expected_end - expected_start).days + 1
    return sorted(covered) == [expected_start + timedelta(days=i) for i in range(span)]


# --- splitting: no gap, no overlap ---


def test_split_is_adjacent():
    first, second = DateWindow(date(2026, 1, 1), date(2026, 3, 31)).split()
    assert second.start == first.end + timedelta(days=1)


@pytest.mark.parametrize("length", range(2, 40))
def test_split_covers_the_original_window_exactly_once(length):
    window = DateWindow(date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=length - 1))
    assert _covers_exactly_once(window.split(), window.start, window.end)


@pytest.mark.parametrize("length", range(2, 40))
def test_split_halves_are_never_empty(length):
    window = DateWindow(date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=length - 1))
    first, second = window.split()
    assert first.days >= 1 and second.days >= 1
    assert first.days + second.days == window.days


def test_repeated_subdivision_still_covers_everything():
    """Worst case: split all the way down to single days."""
    window = DateWindow(date(2026, 1, 1), date(2026, 3, 31))
    worklist = [window]
    leaves = []
    while worklist:
        current = worklist.pop()
        if current.days == 1:
            leaves.append(current)
        else:
            worklist.extend(current.split())
    assert _covers_exactly_once(leaves, window.start, window.end)


def test_a_single_day_cannot_be_split():
    with pytest.raises(WindowExhausted):
        DateWindow(date(2026, 1, 1), date(2026, 1, 1)).split()


def test_a_backwards_window_is_rejected():
    with pytest.raises(ValueError):
        DateWindow(date(2026, 3, 31), date(2026, 1, 1))


# --- quarterly blocks ---


def test_quarterly_blocks_cover_a_year_exactly_once():
    windows = quarterly_windows(date(2025, 1, 1), date(2025, 12, 31))
    assert len(windows) == 4
    assert _covers_exactly_once(windows, date(2025, 1, 1), date(2025, 12, 31))


def test_quarterly_blocks_are_clipped_to_the_span():
    """A span starting mid-quarter must not reach back before its start."""
    windows = quarterly_windows(date(2025, 2, 14), date(2025, 7, 3))
    assert windows[0].start == date(2025, 2, 14)
    assert windows[0].end == date(2025, 3, 31)
    assert windows[-1].end == date(2025, 7, 3)
    assert _covers_exactly_once(windows, date(2025, 2, 14), date(2025, 7, 3))


def test_quarterly_blocks_cross_the_year_boundary():
    windows = quarterly_windows(date(2025, 11, 5), date(2026, 2, 10))
    assert [w.end for w in windows] == [date(2025, 12, 31), date(2026, 2, 10)]
    assert _covers_exactly_once(windows, date(2025, 11, 5), date(2026, 2, 10))


def test_a_span_inside_one_quarter_is_a_single_block():
    windows = quarterly_windows(date(2026, 7, 1), date(2026, 7, 3))
    assert windows == [DateWindow(date(2026, 7, 1), date(2026, 7, 3))]


# --- overflow detection against real HTML ---


def test_truncated_fixture_is_detected():
    assert page_count(_fixture("paginated_overflow")) == 3
    assert is_truncated(_fixture("paginated_overflow"))


def test_complete_fixture_is_not_truncated():
    assert page_count(_fixture("range_product_fixed")) == 1
    assert not is_truncated(_fixture("range_product_fixed"))


def test_entities_are_unescaped_before_looking_for_the_label():
    """SNIIM has served the label as `P&aacute;gina`.

    Searching raw markup would find nothing, and a NoPaginator on a truncated
    response is the one failure that silently discards rows.
    """
    escaped = "<span>P&aacute;gina&nbsp; 1 de&nbsp; 4</span>"
    assert page_count(escaped) == 4
    assert is_truncated(escaped)


def test_doubled_spaces_in_the_label_are_tolerated():
    """The real label is "Página  1 de  3", with two spaces."""
    assert page_count("<span>Página  1 de  12</span>") == 12


def test_a_negative_page_count_is_an_error_not_a_completion():
    """RegistrosPorPagina <= 0 yields "Página 1 de -2241" plus a single row.

    Read as "not greater than 1", that looks like a complete result set.
    """
    with pytest.raises(ValueError):
        page_count("<span>Página 1 de -2241</span>")


def test_a_missing_label_is_an_error():
    with pytest.raises(NoPaginator):
        page_count("<html><body>nada</body></html>")


def test_the_rejection_page_raises_instead_of_looking_empty():
    with pytest.raises(QueryRejected):
        page_count(_fixture("rejected_all_criteria"))


# --- what to do next ---


def test_truncated_response_yields_two_halves():
    window = DateWindow(date(2026, 7, 1), date(2026, 7, 3))
    following = next_windows(window, _fixture("paginated_overflow"))
    assert len(following) == 2
    assert _covers_exactly_once(following, window.start, window.end)


def test_complete_response_yields_nothing_more():
    window = DateWindow(date(2026, 7, 1), date(2026, 7, 3))
    assert next_windows(window, _fixture("range_product_fixed")) == []


# --- url construction ---


def test_url_carries_every_parameter_the_endpoint_needs():
    url = build_url("740", DateWindow(date(2026, 7, 1), date(2026, 7, 3)))
    assert url.startswith(RESULTS_URL + "?")
    params = parse_qs(urlparse(url).query)
    assert params["fechaInicio"] == ["01/07/2026"]
    assert params["fechaFinal"] == ["03/07/2026"]
    assert params["ProductoId"] == ["740"]
    assert params["OrigenId"] == [ALL]
    assert params["DestinoId"] == [ALL]
    assert params["PreciosPorId"] == ["2"]
    assert params["RegistrosPorPagina"] == ["5000"]


def test_dates_use_the_day_first_format_the_server_expects():
    url = build_url("740", DateWindow(date(2026, 1, 2), date(2026, 2, 1)))
    params = parse_qs(urlparse(url).query)
    assert params["fechaInicio"] == ["02/01/2026"]
    assert params["fechaFinal"] == ["01/02/2026"]


def test_the_url_matches_a_captured_fixture():
    """Guards against drift between what we build and what we know works."""
    import json

    manifest = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
    built = build_url("740", DateWindow(date(2026, 7, 1), date(2026, 7, 3)))
    assert parse_qs(urlparse(built).query) == parse_qs(
        urlparse(manifest["range_product_fixed"]["url"]).query
    )


def test_selecting_everything_is_refused_before_the_request_is_made():
    """The server answers 200 with no data; no point spending a request."""
    with pytest.raises(ValueError):
        build_url(ALL, DateWindow(date(2026, 7, 1), date(2026, 7, 3)))


def test_a_nonpositive_page_size_is_refused():
    """It would come back as one row and a negative paginator."""
    with pytest.raises(ValueError):
        build_url("740", DateWindow(date(2026, 7, 1), date(2026, 7, 3)), records_per_page=0)
