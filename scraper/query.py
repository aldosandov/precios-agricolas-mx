"""Build SNIIM result-page queries and size them by splitting the date window.

Pagination is off the table: the next page is an ASP.NET postback carrying
~213 KB of view state, and the results page is only stateless as a GET. So
volume is controlled from the other end — ask for a window, and if the
server says it truncated, halve the window and ask again.

Everything here is pure: no requests are made. The spider does the fetching
and calls `page_count` on whatever comes back.

See docs/consulta-sniim.md for the endpoint and the parameter semantics.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

RESULTS_URL = (
    "https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales"
    "/PreciosDeMercado/Agricolas/ResultadosConsultaFechaFrutasYHortalizas.aspx"
)

# "Todos" for product, origin and destination. Never all three at once.
ALL = "-1"

# Kilogram, so prices for non-unit presentations arrive already divided by
# the presentation weight. Not inferable from the response, so it is recorded
# on every row the spider emits.
PRICES_PER_KILOGRAM = "2"

# Well under the Int32 ceiling the server accepts: a 5000-row response is
# ~4 MB, and anything larger is better solved by a narrower window.
RECORDS_PER_PAGE = 5000

DATE_FORMAT = "%d/%m/%Y"

# The server rejects the fully unconstrained query with this text, in a 200.
REJECTION_MARKER = "No puede seleccionar todos los productos"

# Matches "Página  1 de  3" — the label carries doubled spaces, and entities
# must already be unescaped by the caller.
_PAGINATOR = re.compile(r"Página\s+(-?\d+)\s+de\s+(-?\d+)")


class QueryRejected(Exception):
    """The server refused the criteria combination (200 with no table)."""


class NoPaginator(Exception):
    """The response carries no page label, so truncation cannot be ruled out."""


class WindowExhausted(Exception):
    """A single day still overflows, and there is nothing left to split."""


@dataclass(frozen=True, order=True)
class DateWindow:
    """An inclusive date range, matching how SNIIM reads fechaInicio/fechaFinal."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window ends before it starts: {self.start}..{self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def split(self) -> tuple[DateWindow, DateWindow]:
        """Halve the window with no overlap and no gap.

        The two halves are adjacent by construction: the second starts the day
        after the first ends, so no date is queried twice and none is skipped.
        """
        if self.days < 2:
            raise WindowExhausted(f"cannot split a single day: {self.start:%Y-%m-%d}")
        midpoint = self.start + timedelta(days=(self.days + 1) // 2 - 1)
        return (
            DateWindow(self.start, midpoint),
            DateWindow(midpoint + timedelta(days=1), self.end),
        )


def quarter_start(day: date) -> date:
    """First day of the calendar quarter containing `day`."""
    return date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)


def next_quarter_start(day: date) -> date:
    start = quarter_start(day)
    return date(start.year + 1, 1, 1) if start.month == 10 else date(start.year, start.month + 3, 1)


def quarterly_windows(start: date, end: date) -> list[DateWindow]:
    """Split a span into calendar-quarter blocks, clipped to the span.

    Quarters are the starting granularity, not a hard rule: whichever block
    the server truncates gets halved from there.
    """
    if end < start:
        raise ValueError(f"span ends before it starts: {start}..{end}")

    windows = []
    cursor = start
    while cursor <= end:
        block_end = min(next_quarter_start(cursor) - timedelta(days=1), end)
        windows.append(DateWindow(cursor, block_end))
        cursor = block_end + timedelta(days=1)
    return windows


def build_url(
    product_id: str,
    window: DateWindow,
    *,
    origin_id: str = ALL,
    destination_id: str = ALL,
    prices_per_id: str = PRICES_PER_KILOGRAM,
    records_per_page: int = RECORDS_PER_PAGE,
) -> str:
    """Assemble the stateless GET for one product over one window."""
    if product_id == ALL and origin_id == ALL and destination_id == ALL:
        raise ValueError("product, origin and destination cannot all be 'Todos'")
    if records_per_page < 1:
        raise ValueError(f"records_per_page must be positive, got {records_per_page}")

    params = [
        ("fechaInicio", window.start.strftime(DATE_FORMAT)),
        ("fechaFinal", window.end.strftime(DATE_FORMAT)),
        ("ProductoId", product_id),
        ("OrigenId", origin_id),
        ("DestinoId", destination_id),
        ("PreciosPorId", prices_per_id),
        ("RegistrosPorPagina", str(records_per_page)),
    ]
    return f"{RESULTS_URL}?{urlencode(params)}"


def page_count(html: str) -> int:
    """Read N out of "Página 1 de N", unescaping entities first.

    SNIIM has served that label as `P&aacute;gina` before. Searching the raw
    markup would miss it and read a truncated response as complete, so the
    text is unescaped before matching — the one place where getting this
    wrong loses data silently instead of raising.
    """
    text = html_module.unescape(html)
    if REJECTION_MARKER in text:
        raise QueryRejected(REJECTION_MARKER)

    match = _PAGINATOR.search(text)
    if match is None:
        raise NoPaginator("response carries no 'Página X de N' label")

    total = int(match.group(2))
    if total < 1:
        # RegistrosPorPagina <= 0 produces "Página 1 de -2241" and one row.
        raise ValueError(f"nonsensical page count {total}; check RegistrosPorPagina")
    return total


def is_truncated(html: str) -> bool:
    """True when the server cut the result set and there are pages we won't ask for."""
    return page_count(html) > 1


def next_windows(window: DateWindow, html: str) -> list[DateWindow]:
    """What to query next: nothing if complete, two halves if truncated.

    Lets the spider stay a loop over a worklist instead of hand-rolling the
    subdivision rule.
    """
    return list(window.split()) if is_truncated(html) else []
