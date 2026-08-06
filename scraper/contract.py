"""Assemble raw-layer records and refuse the ones that cannot be one.

A parsed row only carries what the response said. Everything the query pinned
to a single value is missing from it — that is the source's doing, not a bug
(see docs/consulta-sniim.md). The spider knows what it sent, so the record
keeps both planes: the labels the table served, and the ids the query used.

The contract is about identity, not quality: a row must be placeable in time,
attributable to a product, an origin and a destination, and tagged with the
price mode it was read under. Prices are not checked here. A missing price is
flagged in the intermediate layer, because the raw layer is immutable and
validation marks without correcting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from scraper.parsers.results import ParsedTable
from scraper.query import ALL, DATE_FORMAT, DateWindow


class ContractBreach(Exception):
    """The response cannot be turned into raw-layer records."""


class IncompleteRow(ContractBreach):
    """A row missing a field the raw layer cannot do without."""


class DuplicateNaturalKey(ContractBreach):
    """Two rows of one response claim the same key, so the key is not one."""


@dataclass(frozen=True)
class QueryContext:
    """What the spider sent, and when. None of it is inferable from the response.

    `prices_per_id` matters most: the server divides prices by the presentation
    weight under mode 2 and leaves them whole under mode 1, without changing the
    `Presentación` column either way.
    """

    product_id: str
    origin_id: str
    destination_id: str
    prices_per_id: str
    window: DateWindow
    source_url: str
    fetched_at: datetime


@dataclass(frozen=True)
class RawRecord:
    """One price observation, exactly as the source served it.

    Values stay strings. The labels are None whenever the query pinned that
    criterion, and the matching `*_id` carries what was sent instead.
    """

    date: str
    product: str | None
    quality: str | None
    presentation: str
    origin: str | None
    destination: str | None
    price_min: str | None
    price_max: str | None
    price_frequent: str | None
    obs: str | None
    category: str | None
    product_id: str
    origin_id: str
    destination_id: str
    prices_per_id: str
    natural_key: tuple[str, ...]
    source_url: str
    fetched_at: datetime


def _criterion_key(label: str | None, criterion_id: str) -> str | None:
    """Identify a criterion by the label the table gave, or by the id sent.

    None when neither is available: `-1` means "all of them", so it identifies
    nothing on its own.
    """
    if label is not None:
        return label
    return f"id:{criterion_id}" if criterion_id != ALL else None


def _row_date(row: dict[str, str | None], window: DateWindow) -> str | None:
    """The row's date, from the table or from a window that spans one day.

    `fechaInicio == fechaFinal` drops the Fecha column, and then the window is
    the only thing that can date the row. A wider window without that column
    leaves the row unattributable, which is a stop, not a guess.
    """
    if row.get("date") is not None:
        return row["date"]
    if window.days == 1:
        return window.start.strftime(DATE_FORMAT)
    return None


def build_records(table: ParsedTable, context: QueryContext) -> list[RawRecord]:
    """Merge one response with the query that produced it.

    Raises rather than skipping: a row that cannot be identified means the
    response changed shape, and dropping it would leave a hole that looks
    exactly like a day the market did not report.
    """
    if not context.prices_per_id:
        raise IncompleteRow("missing fields: prices_per_id")

    records = []
    seen: set[tuple[str, ...]] = set()
    for row in table.rows:
        product = row.get("product")
        quality = row.get("quality")
        # Matches the raw label in products.csv, which catalogs.py splits on
        # its last " - ", so the key joins back to the versioned catalog.
        product_label = f"{product} - {quality}" if product and quality else product
        row_date = _row_date(row, context.window)
        parts = {
            "date": row_date,
            "product": _criterion_key(product_label, context.product_id),
            "presentation": row.get("presentation"),
            "origin": _criterion_key(row.get("origin"), context.origin_id),
            "destination": _criterion_key(row.get("destination"), context.destination_id),
            "prices_per_id": context.prices_per_id,
        }
        missing = [name for name, value in parts.items() if not value]
        if missing:
            raise IncompleteRow(f"missing fields: {', '.join(missing)}")

        natural_key = tuple(parts.values())
        if natural_key in seen:
            raise DuplicateNaturalKey(f"repeated natural key: {natural_key}")
        seen.add(natural_key)

        records.append(
            RawRecord(
                date=row_date,
                product=product,
                quality=quality,
                presentation=row["presentation"],
                origin=row.get("origin"),
                destination=row.get("destination"),
                price_min=row.get("price_min"),
                price_max=row.get("price_max"),
                price_frequent=row.get("price_frequent"),
                obs=row.get("obs"),
                category=row["category"],
                product_id=context.product_id,
                origin_id=context.origin_id,
                destination_id=context.destination_id,
                prices_per_id=context.prices_per_id,
                natural_key=natural_key,
                source_url=context.source_url,
                fetched_at=context.fetched_at,
            )
        )
    return records
