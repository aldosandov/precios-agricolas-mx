"""Probe how far back each destination market has data, one year at a time.

The historical depth of the source was only ever spot-checked for the Central
de Abasto de Iztapalapa, and it does vary by market. Phase 2's backfill needs
a per-market floor so it does not spend thousands of requests on years that
never had a single price.

The probe never downloads results. `RegistrosPorPagina=-1` makes the server
report `Página 1 de -N`, where N is the exact row count of the full result
set, while sending back a single row — a ~14 KB response instead of tens of
megabytes. That value is a probing trick and nothing else: the ingest must
never send it, because it silently returns one row where the caller expects
the page (see docs/consulta-sniim.md).

Usage:
    uv run python -m scraper.coverage            # probe every market
    uv run python -m scraper.coverage 210 100    # just these destination ids

Writes two versioned files:
    data/catalogs/market_coverage_by_year.csv   row count per market and year
    data/catalogs/market_coverage.csv           first/last year per market

Roughly 1 800 requests; the empty years answer in milliseconds, the ones with
data take seconds. Expect half an hour.
"""

from __future__ import annotations

import csv
import html as html_module
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

RESULTS_URL = (
    "https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales"
    "/PreciosDeMercado/Agricolas/ResultadosConsultaFechaFrutasYHortalizas.aspx"
)

CATALOGS_DIR = Path(__file__).resolve().parent.parent / "data" / "catalogs"
DESTINATIONS_PATH = CATALOGS_DIR / "destinations.csv"
BY_YEAR_PATH = CATALOGS_DIR / "market_coverage_by_year.csv"
SUMMARY_PATH = CATALOGS_DIR / "market_coverage.csv"
# Written as the scan advances and deleted when it finishes; not versioned.
CHECKPOINT_PATH = CATALOGS_DIR / "market_coverage.partial.csv"

# Deliberately older than any plausible start of the series, so the floor is
# measured and not assumed. Everything before 1998 came back empty in ALD-14.
FIRST_YEAR = 1990

# See the module docstring: probing only, never for ingest.
PROBE_RECORDS_PER_PAGE = -1

EMPTY_MARKER = "NO HAY REGISTROS"
REJECTION_MARKER = "No puede seleccionar todos los productos"

_PAGINATOR = re.compile(r"Página\s+(-?\d+)\s+de\s+(-?\d+)")

# The site answers in seconds and returns 503 when pushed. Four workers is
# survivable only because a rejected probe backs off and the scan resumes
# from its checkpoint; retrying immediately just earns another 503.
ATTEMPTS = 5
BACKOFF_SECONDS = (5, 15, 45, 120)
TIMEOUT = 180
WORKERS = 4


class ProbeFailed(Exception):
    """The response was neither a row count nor an explicit "no records"."""


@dataclass(frozen=True)
class Destination:
    destination_id: str
    label: str
    state: str
    market: str


def read_destinations(path: Path = DESTINATIONS_PATH) -> list[Destination]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [Destination(**row) for row in csv.DictReader(handle)]


def build_probe_url(destination_id: str, year: int) -> str:
    """One market, every product and origin, one whole calendar year."""
    params = [
        ("fechaInicio", f"01/01/{year}"),
        ("fechaFinal", f"31/12/{year}"),
        ("ProductoId", "-1"),
        ("OrigenId", "-1"),
        ("DestinoId", destination_id),
        ("PreciosPorId", "2"),
        ("RegistrosPorPagina", str(PROBE_RECORDS_PER_PAGE)),
    ]
    return f"{RESULTS_URL}?{urlencode(params)}"


def read_row_count(html: str) -> int:
    """Turn a probe response into the size of the result set it stands for.

    Raises instead of returning zero on anything unrecognised: a probe read as
    "no data" when the server actually said something else would shorten the
    backfill window and lose years without a trace.
    """
    text = html_module.unescape(html)
    if REJECTION_MARKER in text:
        raise ProbeFailed(REJECTION_MARKER)
    if EMPTY_MARKER in text:
        return 0

    match = _PAGINATOR.search(text)
    if match is None:
        raise ProbeFailed("response carries neither a page label nor 'NO HAY REGISTROS'")

    denominator = int(match.group(2))
    if denominator >= 0:
        # ceil(total / -1) == -total, so a non-negative denominator means the
        # server ignored the probe value and the count cannot be trusted.
        raise ProbeFailed(f"expected a negative page denominator, got {denominator}")
    return -denominator


def fetch(url: str) -> str:
    """Fetch cookieless and decode by the Content-Type header.

    The results page declares no charset in the markup, so the header is the
    only correct source (docs/consulta-sniim.md).
    """
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def probe(destination_id: str, year: int) -> int:
    """Row count for one market and one year, backing off on transport errors.

    The 503s SNIIM returns under load are not permanent, but retrying them
    immediately just earns another one — hence the widening pauses.
    """
    url = build_probe_url(destination_id, year)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return read_row_count(fetch(url))
        except (urllib.error.URLError, TimeoutError, ProbeFailed) as error:
            if attempt == ATTEMPTS:
                raise ProbeFailed(f"{url}: {error}") from error
            time.sleep(BACKOFF_SECONDS[attempt - 1])
    raise AssertionError("unreachable")


def read_counts(path: Path) -> dict[tuple[str, int], int]:
    """Load whatever an earlier run already probed, so a crash costs minutes."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            (row["destination_id"], int(row["year"])): int(row["rows"])
            for row in csv.DictReader(handle)
        }


def scan(
    destinations: list[Destination],
    years: range,
    known: dict[tuple[str, int], int],
    checkpoint: Path | None = None,
) -> dict[tuple[str, int], int]:
    """Probe the market x year grid, skipping and recording as it goes.

    The grid takes about half an hour and the source drops requests, so every
    result is appended to the checkpoint before the next one is asked for. A
    run that dies resumes where it stopped instead of starting over.
    """
    jobs = [
        (dest.destination_id, year)
        for dest in destinations
        for year in years
        if (dest.destination_id, year) not in known
    ]
    counts = dict(known)
    handle = checkpoint.open("a", encoding="utf-8", newline="") if checkpoint else None
    try:
        writer = csv.writer(handle) if handle else None
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for job, rows in zip(jobs, pool.map(lambda job: probe(*job), jobs)):
                counts[job] = rows
                if writer:
                    writer.writerow([job[0], job[1], rows])
                    handle.flush()
                if rows:
                    print(f"{job[0]} {job[1]}: {rows} rows", flush=True)
    finally:
        if handle:
            handle.close()
    return counts


def build_by_year(
    destinations: list[Destination], years: range, counts: dict[tuple[str, int], int]
) -> list[dict[str, str]]:
    return [
        {
            "destination_id": dest.destination_id,
            "year": str(year),
            "rows": str(counts[(dest.destination_id, year)]),
        }
        for dest in destinations
        for year in years
    ]


def build_summary(
    destinations: list[Destination], years: range, counts: dict[tuple[str, int], int]
) -> list[dict[str, str]]:
    """One row per market: the floor for the backfill, plus what is missing.

    `gap_years` matters as much as `first_year`: a market that reported in
    2004, went quiet, and came back in 2009 must not be backfilled as if the
    whole span were continuous.
    """
    rows = []
    for dest in destinations:
        with_data = [year for year in years if counts[(dest.destination_id, year)]]
        gaps = [] if not with_data else [
            year for year in range(with_data[0], with_data[-1] + 1) if year not in with_data
        ]
        rows.append(
            {
                "destination_id": dest.destination_id,
                "label": dest.label,
                "state": dest.state,
                "market": dest.market,
                "first_year": str(with_data[0]) if with_data else "",
                "last_year": str(with_data[-1]) if with_data else "",
                "years_with_data": str(len(with_data)),
                "total_rows": str(sum(counts[(dest.destination_id, year)] for year in years)),
                "gap_years": " ".join(str(year) for year in gaps),
            }
        )
    return rows


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str]) -> int:
    destinations = read_destinations()
    if argv:
        wanted = set(argv)
        unknown = wanted - {dest.destination_id for dest in destinations}
        if unknown:
            print(f"unknown destination id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        destinations = [dest for dest in destinations if dest.destination_id in wanted]

    years = range(FIRST_YEAR, date.today().year + 1)

    if argv:
        # A partial run would write a partial table over the full one. Probing
        # a few markets is for checking the source, not for regenerating.
        scan(destinations, years, known={})
        print("partial run: nothing written", file=sys.stderr)
        return 0

    known = read_counts(CHECKPOINT_PATH)
    if not CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CHECKPOINT_PATH.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(["destination_id", "year", "rows"])
    print(
        f"probing {len(destinations)} markets x {len(years)} years"
        f" ({len(known)} already done)",
        flush=True,
    )

    counts = scan(destinations, years, known, checkpoint=CHECKPOINT_PATH)

    write_csv(build_by_year(destinations, years, counts), BY_YEAR_PATH)
    write_csv(build_summary(destinations, years, counts), SUMMARY_PATH)
    CHECKPOINT_PATH.unlink()
    print(f"-> {BY_YEAR_PATH}")
    print(f"-> {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
