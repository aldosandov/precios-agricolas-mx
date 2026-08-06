"""Read the SNIIM results table by its header row, never by column position.

The number and order of columns depend on the query: every criterion pinned
to a single value disappears from the table and is shown in the summary block
instead. `fechaInicio == fechaFinal` drops `Fecha`; `ProductoId=-1` adds
`Producto` and `Calidad`. A parser that assumed fixed positions would not
raise on those responses, it would quietly write presentations into the date
column (see docs/consulta-sniim.md).

So the column map is read from every response, an unknown header stops the
ingest instead of being dropped, and values come back as the strings the
source served. Typing and validation belong to the intermediate BigQuery
layer, which flags and never corrects.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass

from parsel import Selector

from scraper.query import REJECTION_MARKER, QueryRejected

# Every header the source is known to serve, across all six query shapes.
# Values are the canonical field names the rest of the pipeline uses.
COLUMNS = {
    "Fecha": "date",
    "Producto": "product",
    "Calidad": "quality",
    "Presentación": "presentation",
    "Origen": "origin",
    "Destino": "destination",
    "Precio Mín": "price_min",
    "Precio Max": "price_max",
    "Precio Frec": "price_frequent",
    "Obs.": "obs",
}

# A valid query whose range holds no prices; not the same as a rejected one.
EMPTY_MARKER = "NO HAY REGISTROS"

_WHITESPACE = re.compile(r"\s+")


class ParseError(Exception):
    """The response could not be read as a results table."""


class UnknownColumn(ParseError):
    """A header outside the known vocabulary; the ingest stops here."""


class UnexpectedResponse(ParseError):
    """Neither a table, nor a rejection, nor an explicit "no records"."""


@dataclass(frozen=True)
class ParsedTable:
    """What one response said, and nothing the query knew.

    `columns` holds the canonical fields in the order the source served them.
    Each row maps those fields plus `category`, the separator row it fell
    under. Fields the query pinned are absent here on purpose: the spider
    knows what it sent, the response does not.
    """

    columns: tuple[str, ...]
    rows: tuple[dict[str, str | None], ...]


def _text(cell: Selector) -> str | None:
    """Cell text as the source meant it, or None when it is blank."""
    joined = "".join(cell.css("::text").getall())
    collapsed = _WHITESPACE.sub(" ", html_module.unescape(joined)).strip()
    return collapsed or None


def _map_headers(cells: list[Selector]) -> tuple[str, ...]:
    columns = []
    for cell in cells:
        label = _text(cell)
        if label not in COLUMNS:
            raise UnknownColumn(f"unknown column header: {label!r}")
        columns.append(COLUMNS[label])
    return tuple(columns)


def parse_results(html: str) -> ParsedTable:
    """Turn a results page into its columns and rows.

    Raises on anything that is not one of the two legitimate outcomes, so a
    change in the source shows up as a stopped ingest and not as a window
    that looks like it never had prices.
    """
    text = html_module.unescape(html)
    if REJECTION_MARKER in text:
        raise QueryRejected(REJECTION_MARKER)
    if EMPTY_MARKER in text:
        return ParsedTable((), ())

    rows = Selector(text=html).css("#tblResultados tr")
    if not rows:
        raise UnexpectedResponse("response carries no results table")

    columns = _map_headers(rows[0].css("td"))

    parsed = []
    category = None
    for row in rows[1:]:
        cells = row.css("td")
        if len(cells) == 1:
            # Category separator (Frutas, Hortalizas, ...). It appears in every
            # query shape, even when a single product was asked for.
            category = _text(cells[0])
            continue
        if len(cells) != len(columns):
            # Not dropped and not zipped short: a row that lost a cell would
            # otherwise come out looking like a complete one.
            raise UnexpectedResponse(
                f"row has {len(cells)} cells, header declares {len(columns)}"
            )
        parsed.append(
            {
                "category": category,
                **dict(zip(columns, (_text(cell) for cell in cells), strict=True)),
            }
        )
    return ParsedTable(columns, tuple(parsed))
