"""Write the raw layer to NDJSON partitioned by date, ready for `bq load`.

Loading from files is free and streaming inserts are not, so the ingest never
talks to BigQuery: it drops files, and a separate step loads them (PRD §9.2).

This is where the source's strings become the raw-layer row of §10 — the date
as ISO, the prices as numbers, the price mode as a name instead of the id that
was sent. Nothing is repaired on the way: a price that is zero, missing or
unreadable travels with a flag, because the raw layer is immutable and the
validation marks without correcting.

One file per date, market and price mode. A window is queried whole, so a date
lands in exactly one file, and rerunning a window rewrites that file instead of
appending a second copy of the same day.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from scraper.contract import RawRecord
from scraper.coverage import read_destinations
from scraper.query import ALL, DATE_FORMAT

# Field order of the raw-layer row, PRD §10. It becomes the column list of the
# raw BigQuery table, so nothing is added or renamed here on a whim.
RAW_SCHEMA = (
    "fecha",
    "producto_raw",
    "calidad_raw",
    "presentacion_raw",
    "origen_raw",
    "destino_raw",
    "destino_id",
    "grupo_raw",
    "tipo_precio",
    "precio_min",
    "precio_max",
    "precio_frecuente",
    "observaciones",
    "banderas_calidad",
    "extraido_en",
    "url_consulta",
    "llave_fila",
)

PRICE_MODES = {"1": "presentacion_comercial", "2": "kilogramo_calculado"}

PRICE_FIELDS = ("precio_min", "precio_max", "precio_frecuente")

# Where the ingest drops files. Not versioned: the backfill writes millions of
# rows and BigQuery is where they live.
RAW_DIR = Path(__file__).resolve().parents[2] / "out" / "raw"


class UnwritableRow(Exception):
    """A record that cannot become a raw-layer row, so the ingest stops."""


def _iso_date(value: str) -> str:
    """dd/mm/aaaa as the source writes it, to the ISO date BigQuery partitions by."""
    try:
        return datetime.strptime(value, DATE_FORMAT).date().isoformat()
    except ValueError as error:
        # Without a date the row cannot be partitioned or placed in the series,
        # so this is a stop and not a flag.
        raise UnwritableRow(f"unreadable date: {value!r}") from error


def _price(value: str | None) -> tuple[float | None, str | None]:
    """Parse one price, returning what it is worth and what is wrong with it."""
    if value is None:
        return None, "precio_ausente"
    try:
        # The source prints thousands separators once prices pass 1000.
        number = float(value.replace(",", ""))
    except ValueError:
        return None, "precio_no_numerico"
    return number, "precio_cero" if number == 0 else None


def _quality_flags(prices: dict[str, float | None], found: list[str]) -> list[str]:
    """What is wrong with the row's measures. Order is stable for diffing."""
    flags = list(dict.fromkeys(found))
    minimum, maximum, frequent = (prices[field] for field in PRICE_FIELDS)
    if None not in (minimum, maximum, frequent) and not minimum <= frequent <= maximum:
        # The source defines frequent as the mode of the sample, so it has to
        # sit between the two ends of that same sample.
        flags.append("precios_incoherentes")
    return flags


def _destination_labels() -> dict[str, str]:
    return {dest.destination_id: dest.label for dest in read_destinations()}


def _row_key(natural_key: Sequence[str]) -> str:
    return hashlib.sha256("|".join(natural_key).encode("utf-8")).hexdigest()


def to_raw_rows(records: Iterable[RawRecord]) -> list[dict]:
    """Turn contract records into raw-layer rows, one dict per line of NDJSON."""
    labels = _destination_labels()
    rows = []
    for record in records:
        if record.prices_per_id not in PRICE_MODES:
            raise UnwritableRow(f"unknown price mode: {record.prices_per_id!r}")

        prices: dict[str, float | None] = {}
        found: list[str] = []
        for field, value in zip(
            PRICE_FIELDS, (record.price_min, record.price_max, record.price_frequent)
        ):
            prices[field], flag = _price(value)
            if flag:
                found.append(flag)

        pinned = record.destination_id != ALL
        rows.append(
            {
                "fecha": _iso_date(record.date),
                "producto_raw": record.product,
                "calidad_raw": record.quality,
                "presentacion_raw": record.presentation,
                "origen_raw": record.origin,
                # The query erases the pinned criterion from the table; the
                # catalog holds the same string the source would have served.
                "destino_raw": record.destination
                or (labels.get(record.destination_id) if pinned else None),
                "destino_id": int(record.destination_id) if pinned else None,
                "grupo_raw": record.category,
                "tipo_precio": PRICE_MODES[record.prices_per_id],
                **prices,
                "observaciones": record.obs,
                "banderas_calidad": _quality_flags(prices, found),
                "extraido_en": record.fetched_at.isoformat(),
                "url_consulta": record.source_url,
                "llave_fila": _row_key(record.natural_key),
            }
        )
    return rows


def _partition_name(row: dict) -> str:
    destination = row["destino_id"] if row["destino_id"] is not None else "todos"
    return f"destino={destination}_precio={row['tipo_precio']}.ndjson"


def write_partitions(rows: Sequence[dict], out_dir: Path = RAW_DIR) -> list[Path]:
    """Write one file per date, market and price mode. Rewrites, never appends."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["fecha"], _partition_name(row))].append(row)

    written = []
    for (day, name), partition in sorted(grouped.items()):
        directory = out_dir / f"fecha={day}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with path.open("w", encoding="utf-8") as handle:
            for row in partition:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        written.append(path)
    return written
