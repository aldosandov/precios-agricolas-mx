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
import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path

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

logger = logging.getLogger(__name__)

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
            PRICE_FIELDS,
            (record.price_min, record.price_max, record.price_frequent),
            strict=True,
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


def _dated(path: Path) -> date:
    return date.fromisoformat(path.parent.name.removeprefix("fecha="))


# How many partitions may be open at once. A backfill quarter spans ~90 dates
# across 47 markets and two price modes: 8 460 files, which is EMFILE on any
# machine with the usual 1024 descriptors. Reopening a partition costs one
# open() and the writes go to the same place, so the cap is free.
MAX_OPEN_PARTITIONS = 512


class PartitionWriter:
    """Write rows as they arrive, one file per date, market and price mode.

    The spider hands rows over one at a time and a backfill produces millions
    of them, so nothing is buffered. Each partition is truncated the first
    time this run touches it and appended to afterwards: a rerun replaces the
    day instead of doubling it, without holding it in memory to find out.

    That first-touch rule is also what lets a partition be closed and reopened
    mid-run, which is what keeps a quarter of the backfill from asking the
    kernel for thousands of descriptors at once.
    """

    def __init__(self, out_dir: Path = RAW_DIR, max_open: int = MAX_OPEN_PARTITIONS):
        self.out_dir = out_dir
        self.paths: list[Path] = []
        self.max_open = max_open
        # Rows taken in, by date. Whoever hands a finished date range to the
        # loader needs to know the pipeline has caught up with the spider:
        # Scrapy processes items after the callback's generator has already
        # ended, so "every block resolved" does not mean "every row written".
        self.rows: Counter[date] = Counter()
        # Insertion-ordered and moved on use, so the one evicted is the one
        # written to longest ago.
        self._open: dict[Path, object] = {}
        self._touched: set[Path] = set()

    def write(self, row: dict) -> Path:
        path = self.out_dir / f"fecha={row['fecha']}" / _partition_name(row)
        handle = self._open.get(path)
        if handle is None:
            handle = self._reopen(path)
        else:
            self._open[path] = self._open.pop(path)
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.rows[date.fromisoformat(row["fecha"])] += 1
        return path

    def rows_between(self, start: date, end: date) -> int:
        return sum(count for day, count in self.rows.items() if start <= day <= end)

    def _reopen(self, path: Path):
        while len(self._open) >= self.max_open:
            self._open.pop(next(iter(self._open))).close()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate the first time this run touches the partition, append after:
        # the rerun still replaces the day instead of doubling it.
        first = path not in self._touched
        handle = self._open[path] = path.open("w" if first else "a", encoding="utf-8")
        if first:
            self._touched.add(path)
            self.paths.append(path)
        return handle

    def close_dated(self, start, end) -> None:
        """Close the partitions of a date range, so what is on disk is all of it.

        Rows are buffered by the file object until it is closed, so anything
        reading a partition while the run is still going sees a file missing
        its last few kilobytes. Measured: a quarter handed over to the loader
        mid-run counted 7 770 rows on disk and put 7 249 into BigQuery.

        Reopening later appends, by the same first-touch rule as the eviction
        above, so a block of this range retried afterwards is not lost.
        """
        for path in [p for p in self._open if start <= _dated(p) <= end]:
            self._open.pop(path).close()

    def close(self) -> list[Path]:
        for handle in self._open.values():
            handle.close()
        self._open.clear()
        return self.paths


def write_partitions(rows: Sequence[dict], out_dir: Path = RAW_DIR) -> list[Path]:
    """Write a batch at once. Same files a streaming run would leave behind."""
    writer = PartitionWriter(out_dir)
    for row in sorted(rows, key=lambda row: (row["fecha"], _partition_name(row))):
        writer.write(row)
    return writer.close()


class NdjsonPartitionPipeline:
    """Scrapy item pipeline: every row the spiders emit lands in its partition."""

    def __init__(self, out_dir: Path = RAW_DIR):
        self.writer = PartitionWriter(out_dir)

    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls(Path(crawler.settings.get("RAW_OUTPUT_DIR", RAW_DIR)))
        # So whoever loads a finished date range can make sure it is all on
        # disk first. A pipeline is otherwise unreachable from outside the
        # item chain, and buffered rows read as missing ones.
        crawler.raw_writer = pipeline.writer
        return pipeline

    def process_item(self, item):
        self.writer.write(item)
        return item

    def close_spider(self):
        paths = self.writer.close()
        logger.info(f"escritas {len(paths)} particiones en {self.writer.out_dir}")
