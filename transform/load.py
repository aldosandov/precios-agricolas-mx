"""Load the ingest's NDJSON into the raw layer, as an upsert on the row key.

Loading from files is free and streaming is not, so this runs apart from the
scraper: the spider drops partitions, this reads them (PRD §9.2).

The load is idempotent on the natural key (§8.8), which is why it goes through
a staging table and a MERGE instead of replacing whole partitions. The sweep
writes one market at a time, so truncating the day to load market 210 would
drop the 48 already in it — the simpler path is the one that loses data.

Usage:
    uv run python -m transform.load                    # todo lo que haya en out/raw
    uv run python -m transform.load --dir out/raw --dry-run
    uv run python -m transform.load --table proyecto.dataset.tabla
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from scraper.pipelines.ndjson import RAW_DIR, RAW_SCHEMA

RAW_TABLE = "precios-agricolas-mx.crudo.precios"

# Staging lives beside the table it feeds, and cleans itself up if a run dies
# before dropping it.
STAGING_PREFIX = "_paso_"
STAGING_TTL_HOURS = 6


@dataclass(frozen=True)
class LoadReport:
    files: int
    staged_rows: int
    affected_rows: int
    table: str
    dates: tuple[date, ...]


def partition_files(root: Path = RAW_DIR) -> list[Path]:
    """Every NDJSON partition under `root`, in date order."""
    return sorted(root.rglob("*.ndjson"))


def partition_dates(paths: Sequence[Path]) -> list[date]:
    """The dates those partitions belong to, read from their directory names.

    Raises on anything outside a `fecha=` directory: without a date there is
    nothing to prune the MERGE by, and an unpruned MERGE reads every partition
    of the whole history.
    """
    dates = set()
    for path in paths:
        name = path.parent.name
        if not name.startswith("fecha="):
            raise ValueError(f"{path} no está en un directorio fecha=YYYY-MM-DD")
        dates.add(date.fromisoformat(name.removeprefix("fecha=")))
    return sorted(dates)


def build_merge_sql(*, target: str, staging: str, start: date, end: date) -> str:
    """Upsert staging into the raw table on the row key, pruned to the dates loaded.

    The date range sits in the ON clause on purpose: that is what lets BigQuery
    read only the partitions being loaded instead of all of them.
    """
    columns = list(RAW_SCHEMA)
    updates = ",\n    ".join(f"{column} = S.{column}" for column in columns)
    names = ", ".join(columns)
    values = ", ".join(f"S.{column}" for column in columns)
    return f"""MERGE `{target}` T
USING `{staging}` S
ON T.llave_fila = S.llave_fila
   AND T.fecha = S.fecha
   AND T.fecha BETWEEN DATE '{start:%Y-%m-%d}' AND DATE '{end:%Y-%m-%d}'
WHEN MATCHED THEN UPDATE SET
    {updates}
WHEN NOT MATCHED THEN INSERT ({names})
VALUES ({values})"""


def _staging_table(client, target: str):
    """A throwaway copy of the target's schema, expiring on its own."""
    from google.cloud import bigquery

    project, dataset, table = target.split(".")
    staging_id = f"{project}.{dataset}.{STAGING_PREFIX}{uuid.uuid4().hex[:12]}"
    staging = bigquery.Table(staging_id, schema=client.get_table(target).schema)
    staging.expires = datetime.now(UTC) + timedelta(hours=STAGING_TTL_HOURS)
    return client.create_table(staging)


def load(
    paths: Sequence[Path],
    *,
    target: str = RAW_TABLE,
    client=None,
) -> LoadReport:
    """Stage every file, upsert once, drop the staging table."""
    from google.cloud import bigquery

    if not paths:
        raise ValueError("no hay particiones que cargar")

    dates = partition_dates(paths)
    client = client or bigquery.Client(project=target.split(".")[0])
    staging = _staging_table(client, target)
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=client.get_table(target).schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )

    try:
        for path in paths:
            with path.open("rb") as handle:
                client.load_table_from_file(
                    handle, staging.reference, job_config=job_config
                ).result()

        staged_rows = client.get_table(staging.reference).num_rows
        merge = client.query(
            build_merge_sql(
                target=target,
                staging=str(staging.reference),
                start=dates[0],
                end=dates[-1],
            )
        )
        merge.result()
        affected = merge.num_dml_affected_rows
    finally:
        client.delete_table(staging.reference, not_found_ok=True)

    return LoadReport(
        files=len(paths),
        staged_rows=staged_rows,
        affected_rows=affected,
        table=target,
        dates=tuple(dates),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=RAW_DIR)
    parser.add_argument("--table", default=RAW_TABLE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="listar qué se cargaría, sin tocar BigQuery",
    )
    args = parser.parse_args(argv)

    paths = partition_files(args.dir)
    if not paths:
        print(f"no hay particiones en {args.dir}", file=sys.stderr)
        return 1

    dates = partition_dates(paths)
    print(f"{len(paths)} archivo(s), {len(dates)} fecha(s): {dates[0]}..{dates[-1]}")
    if args.dry_run:
        for path in paths:
            print(f"  {path}")
        return 0

    report = load(paths, target=args.table)
    print(
        f"{report.files} archivo(s), {report.staged_rows} filas en paso -> "
        f"{report.affected_rows} insertadas o actualizadas en {report.table}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
