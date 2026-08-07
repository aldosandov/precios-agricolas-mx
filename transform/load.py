"""Load the ingest's NDJSON into the raw layer, as an upsert on the row key.

Loading from files is free and streaming is not, so this runs apart from the
scraper: the spider drops partitions, this reads them (PRD §9.2).

The load is idempotent on the natural key (§8.8), which is why it goes through
a staging table and a MERGE instead of replacing whole partitions. The sweep
writes one market at a time, so truncating the day to load market 210 would
drop the 48 already in it — the simpler path is the one that loses data.

Every batch is one load job, however many files it holds: BigQuery allows 1 500
load jobs per table per day, and the backfill's 116 quarters at ~180 partitions
each would be ~21 000 of them. The batch is a **calendar year** (ALD-57): a
block never crosses one, so grouping by year splits no unit of work, and the
whole history costs 29 load jobs and 29 MERGEs.

Usage:
    uv run python -m transform.load                    # todo lo que haya en out/raw
    uv run python -m transform.load --dir out/raw --dry-run
    uv run python -m transform.load --table proyecto.dataset.tabla
    uv run python -m transform.load --desde 2011-07-01 --hasta 2011-09-30 --purge
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import uuid
from collections import defaultdict
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


def partition_date(path: Path) -> date:
    """The date a partition belongs to, read from its directory name.

    Raises on anything outside a `fecha=` directory: without a date there is
    nothing to prune the MERGE by, and an unpruned MERGE reads every partition
    of the whole history.
    """
    name = path.parent.name
    if not name.startswith("fecha="):
        raise ValueError(f"{path} no está en un directorio fecha=YYYY-MM-DD")
    return date.fromisoformat(name.removeprefix("fecha="))


def partition_files(
    root: Path = RAW_DIR, *, since: date | None = None, until: date | None = None
) -> list[Path]:
    """Every NDJSON partition under `root`, in date order, within the range.

    The backfill loads a year at a time so the MERGE prunes to that year's
    partitions instead of reading the whole history, and so the files can be
    dropped once they are in.
    """
    paths = sorted(root.rglob("*.ndjson"))
    if since is None and until is None:
        return paths
    return [
        path
        for path in paths
        if (since is None or partition_date(path) >= since)
        and (until is None or partition_date(path) <= until)
    ]


def partition_dates(paths: Sequence[Path]) -> list[date]:
    """The dates those partitions belong to, deduplicated and sorted."""
    return sorted({partition_date(path) for path in paths})


def by_year(paths: Sequence[Path]) -> dict[int, list[Path]]:
    """Those partitions grouped by calendar year, oldest first.

    The year is the batch the backfill loads: one job and one MERGE for it, 29
    for the whole history against 116 per quarter. A block never crosses the
    year boundary, so this splits no unit of work — a year loaded is a set of
    blocks that can be retired whole.

    It is still narrow enough to keep the MERGE pruned: the grid is
    chronological, so a year's range reads that year's partitions and not the
    history behind it.
    """
    years: dict[int, list[Path]] = defaultdict(list)
    for path in paths:
        years[partition_date(path).year].append(path)
    return {year: years[year] for year in sorted(years)}


def count_rows(paths: Sequence[Path]) -> int:
    """Lines across the given partitions — one row of the raw layer each."""
    return sum(
        sum(1 for line in path.open("rb") if line.strip()) for path in paths
    )


def purge(paths: Sequence[Path], report: LoadReport) -> int:
    """Delete partitions already in BigQuery, and only those.

    Counted before deleting, not trusted: a load job that skipped rows and a
    load job that took them all look the same from the outside, and the
    difference is the data this is about to remove from the only other place it
    exists.
    """
    staged = count_rows(paths)
    if staged != report.staged_rows:
        raise ValueError(
            f"no se purga: {staged} filas en disco contra {report.staged_rows} "
            f"en la tabla de paso"
        )

    for path in paths:
        path.unlink()
    # The date directory goes too once it is empty, so `out/raw` does not fill
    # up with thousands of husks across a backfill of 116 quarters.
    for parent in {path.parent for path in paths}:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    return len(paths)


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
    staging_dir: Path | None = None,
) -> LoadReport:
    """Stage every file in one job, upsert once, drop the staging table."""
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
        # One load job for the whole batch, not one per file. BigQuery allows
        # 1 500 load jobs per table per day, and a year of the backfill is
        # ~700 partitions: per file, the history would be ~30 000 jobs and
        # would hit the quota on the first day. Concatenating costs one pass
        # over the year's ~870 MB and a temporary copy of it, which is why the
        # batch is a year and not the whole disk.
        with tempfile.TemporaryFile(dir=staging_dir) as batch:
            for path in paths:
                with path.open("rb") as handle:
                    shutil.copyfileobj(handle, batch)
            batch.seek(0)
            client.load_table_from_file(
                batch, staging.reference, job_config=job_config
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
    parser.add_argument("--desde", type=date.fromisoformat, help="primera fecha a cargar")
    parser.add_argument("--hasta", type=date.fromisoformat, help="última fecha a cargar")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="borrar las particiones cargadas, tras cotejar filas contra la "
        "tabla de paso. Ojo: purgar por aquí no marca los bloques como "
        "cargados, así que el backfill los volvería a pedir. Para eso está "
        "`python -m scraper.spiders.backfill --solo-cargar`",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="listar qué se cargaría, sin tocar BigQuery",
    )
    args = parser.parse_args(argv)

    paths = partition_files(args.dir, since=args.desde, until=args.hasta)
    if not paths:
        print(f"no hay particiones en {args.dir}", file=sys.stderr)
        return 1

    dates = partition_dates(paths)
    years = by_year(paths)
    print(
        f"{len(paths)} archivo(s), {len(dates)} fecha(s): {dates[0]}..{dates[-1]}"
        f" · {len(years)} año(s), un job de carga cada uno"
    )
    if args.dry_run:
        for path in paths:
            print(f"  {path}")
        return 0

    for year, batch in years.items():
        report = load(batch, target=args.table)
        print(
            f"{year}: {report.files} archivo(s), {report.staged_rows} filas en paso -> "
            f"{report.affected_rows} insertadas o actualizadas en {report.table}"
        )
        if args.purge:
            print(f"  purgadas {purge(batch, report)} partición(es) de {args.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
