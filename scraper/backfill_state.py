"""What the backfill has already covered, so a session can pick up where it left (ALD-26).

The backfill is ~9 600 blocks run by hand on a laptop, in whatever hours are
free, across several days. Stopping and starting is the normal way it runs, not
an accident, so "what is left" has to survive the process rather than live in
it.

That is all this is: a grid of work, a SQLite file recording which cells are
closed, and the subtraction between them. `out/cobertura.sqlite` is not
versioned — it is the state of one machine's sessions, not of the project.

The unit of work is a market, a price mode and a **calendar quarter**. Quarters
subdivide in flight when the server truncates a response (see
`scraper.query.next_windows`), but the quarter is what gets recorded: a block
is done when every window it split into came back readable, and failed if any
of them did not. Redoing a quarter costs requests and nothing else, because the
load is idempotent on the row key.

A block leaves the queue when it is `hecho` **and** `cargado_en` is set, which
are two different things since ALD-57: the load runs after the sweep, so
between them the NDJSON on disk is the only copy. Losing it before it is loaded
puts those blocks back in the queue instead of losing the data.

The grid comes from the ALD-14 probe (`data/catalogs/market_coverage_by_year.csv`),
never from the calendar. Years the source never had are not asked for, which is
what keeps two dead markets, fifteen late starts and two internal gaps out of
the queue.

Usage:
    uv run python -m scraper.backfill_state          # cuánto falta y qué falló
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from scraper.coverage import BY_YEAR_PATH
from scraper.pipelines.ndjson import PRICE_MODES, RAW_DIR
from scraper.query import DateWindow, quarterly_windows, source_date

# Beside the NDJSON the same sessions produce, and ignored by git for the same
# reason: it is the state of one laptop's work, not of the project.
DB_PATH = RAW_DIR.parent / "cobertura.sqlite"

# The table records the readable name, the one `crudo.precios.tipo_precio`
# carries, so auditing coverage against BigQuery (ALD-28) is a join and not a
# translation. The id is what the query needs and is derived when asked for.
PRICES_PER_ID = {name: mode_id for mode_id, name in PRICE_MODES.items()}

OPEN, DONE, FAILED = "abierto", "hecho", "fallido"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cobertura (
    destino_id      TEXT    NOT NULL,
    tipo_precio     TEXT    NOT NULL,
    bloque_inicio   TEXT    NOT NULL,
    bloque_fin      TEXT    NOT NULL,
    estado          TEXT    NOT NULL,
    filas           INTEGER,
    peticiones      INTEGER,
    intentos        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    abierto_en      TEXT,
    cerrado_en      TEXT,
    cargado_en      TEXT,
    PRIMARY KEY (destino_id, tipo_precio, bloque_inicio)
);
"""


@dataclass(frozen=True, order=True)
class Block:
    """One quarter of one market at one price mode.

    Field order is the sort order, and the sort order is the plan: every market
    of 1998-Q1, then every market of 1998-Q2, and so on. Sweeping by market
    instead would make each load's MERGE span the whole history, and would
    leave an abandoned run with a few complete markets rather than a complete
    history up to some date.
    """

    start: date
    end: date
    destination_id: str
    price_mode: str

    @property
    def window(self) -> DateWindow:
        return DateWindow(self.start, self.end)

    @property
    def prices_per_id(self) -> str:
        return PRICES_PER_ID[self.price_mode]

    @property
    def quarter(self) -> str:
        return f"{self.start.year}-Q{(self.start.month - 1) // 3 + 1}"

    @property
    def label(self) -> str:
        """What the progress console shows while this block is being worked."""
        return f"{self.quarter} · destino {self.destination_id} · {self.price_mode}"


@dataclass(frozen=True)
class Counts:
    done: int
    failed: int
    open: int
    # Swept but not yet in BigQuery: their NDJSON is still on disk and is the
    # only copy of it.
    unloaded: int = 0


def years_with_data(path: Path = BY_YEAR_PATH) -> dict[str, list[int]]:
    """Which years each market actually reported, as ALD-14 measured them.

    A market being in the catalog says nothing: two never published a row and
    five stopped years ago. Two more have holes in the middle. Asking the
    calendar instead of this file would spend thousands of requests on years
    that were never there.
    """
    found: dict[str, list[int]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if int(row["rows"]):
                found[row["destination_id"]].append(int(row["year"]))
    return {market: sorted(years) for market, years in found.items()}


def plan_grid(
    *,
    path: Path = BY_YEAR_PATH,
    today: date | None = None,
    destinations: Sequence[str] | None = None,
) -> list[Block]:
    """Every block the backfill has to cover, in the order it will cover them.

    The running quarter is clipped to today: asking for dates the source has
    not reached costs requests and returns nothing.
    """
    last_day = today or source_date()
    wanted = set(destinations) if destinations else None

    blocks = []
    for market, years in years_with_data(path).items():
        if wanted is not None and market not in wanted:
            continue
        for year in years:
            start, end = date(year, 1, 1), min(date(year, 12, 31), last_day)
            if start > end:
                continue
            for window in quarterly_windows(start, end):
                blocks.extend(
                    Block(window.start, window.end, market, mode) for mode in PRICES_PER_ID
                )
    return sorted(blocks)


class CoverageStore:
    """The SQLite file that remembers which blocks are closed.

    One row per block, written the moment it closes. A `kill -9` costs the
    block in flight and nothing before it.
    """

    def __init__(self, path: Path | str = DB_PATH):
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # No implicit transactions: every write is its own commit, which is the
        # point — a checkpoint that batches is not a checkpoint.
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        # The whole value of this file is surviving a run that dies, so the
        # commit waits for the disk. One fsync per block, one block per second
        # or so: unmeasurable next to a request to the SNIIM.
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(SCHEMA)
        self._add_cargado_en()

    def _add_cargado_en(self) -> None:
        """Bring a database written before ALD-57 up to the current schema.

        Back then the load ran inside the sweep and a block was purged from
        disk the moment it closed, so `hecho` did mean "in BigQuery". Those
        rows are backfilled to say so; without it the migration would put the
        whole covered history back in the queue.
        """
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(cobertura)")
        }
        if "cargado_en" in columns:
            return
        self.connection.execute("ALTER TABLE cobertura ADD COLUMN cargado_en TEXT")
        self.connection.execute(
            "UPDATE cobertura SET cargado_en = cerrado_en WHERE estado = ?", (DONE,)
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> CoverageStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- what is left ---

    def pending(self, grid: Iterable[Block]) -> list[Block]:
        """The grid minus what is already covered, in grid order.

        A block left `abierto` by an interrupted session comes back, and so
        does one that failed. So does one swept but not loaded: since the load
        was moved out of the sweep (ALD-57), `hecho` only means the rows are on
        this laptop's disk, and that is not somewhere they are safe.

        Covered means covered *to the same date*. The running quarter grows a
        day at a time, so a block closed last week no longer matches the grid
        and returns on its own — no special case for "today".
        """
        covered = {
            (row["destino_id"], row["tipo_precio"], row["bloque_inicio"]): row["bloque_fin"]
            for row in self.connection.execute(
                "SELECT destino_id, tipo_precio, bloque_inicio, bloque_fin"
                " FROM cobertura WHERE estado = ? AND cargado_en IS NOT NULL",
                (DONE,),
            )
        }
        return [
            block
            for block in grid
            if covered.get(
                (block.destination_id, block.price_mode, block.start.isoformat())
            )
            != block.end.isoformat()
        ]

    def counts(self) -> Counts:
        tally = dict.fromkeys((DONE, FAILED, OPEN), 0)
        for row in self.connection.execute(
            "SELECT estado, COUNT(*) n FROM cobertura GROUP BY estado"
        ):
            tally[row["estado"]] = row["n"]
        unloaded = self.connection.execute(
            "SELECT COUNT(*) n FROM cobertura WHERE estado = ? AND cargado_en IS NULL",
            (DONE,),
        ).fetchone()["n"]
        return Counts(
            done=tally[DONE], failed=tally[FAILED], open=tally[OPEN], unloaded=unloaded
        )

    def failures(self) -> list[str]:
        """The blocks that broke, as lines to read rather than rows to parse."""
        return [
            f"{row['bloque_inicio']}..{row['bloque_fin']} destino={row['destino_id']}"
            f" precio={row['tipo_precio']}: {row['error']}"
            for row in self.connection.execute(
                "SELECT * FROM cobertura WHERE estado = ? ORDER BY bloque_inicio,"
                " destino_id, tipo_precio",
                (FAILED,),
            )
        ]

    # --- writing a block down ---

    def open_block(self, block: Block) -> None:
        """Mark a block as being worked on, counting the attempt.

        Reopening clears whatever the last attempt left behind: a block that
        failed and is being retried must not keep its old error, and one that
        stays `abierto` after the session dies must look unfinished, which it
        is.
        """
        self.connection.execute(
            """
            INSERT INTO cobertura (
                destino_id, tipo_precio, bloque_inicio, bloque_fin,
                estado, intentos, abierto_en
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(destino_id, tipo_precio, bloque_inicio) DO UPDATE SET
                bloque_fin = excluded.bloque_fin,
                estado     = excluded.estado,
                intentos   = intentos + 1,
                filas      = NULL,
                peticiones = NULL,
                error      = NULL,
                abierto_en = excluded.abierto_en,
                cerrado_en = NULL,
                cargado_en = NULL
            """,
            (*self._key(block), block.end.isoformat(), OPEN, _now()),
        )

    def close_block(self, block: Block, *, rows: int, requests: int) -> None:
        # A block that captured nothing wrote no partition, so there is nothing
        # on disk that could be lost before the load: it is safe the moment it
        # closes. Every other block waits for `mark_loaded`.
        self._settle(
            block,
            DONE,
            rows=rows,
            requests=requests,
            error=None,
            loaded=_now() if rows == 0 else None,
        )

    def fail_block(self, block: Block, error: str, *, requests: int = 0) -> None:
        self._settle(block, FAILED, rows=None, requests=requests, error=error)

    def _settle(self, block, state, *, rows, requests, error, loaded=None) -> None:
        self.connection.execute(
            "UPDATE cobertura SET estado = ?, filas = ?, peticiones = ?, error = ?,"
            " cerrado_en = ?, cargado_en = ?"
            " WHERE destino_id = ? AND tipo_precio = ? AND bloque_inicio = ?",
            (state, rows, requests, error, _now(), loaded, *self._key(block)),
        )

    def mark_loaded(self, start: date, end: date) -> int:
        """Record that these blocks' rows are in BigQuery, so they can retire.

        Called only after a load returned and the partitions were purged
        against a row count. Until then the blocks stay in the queue: asking
        the SNIIM for a quarter again costs requests, and it is the only cost
        of being wrong in this direction.
        """
        cursor = self.connection.execute(
            "UPDATE cobertura SET cargado_en = ?"
            " WHERE estado = ? AND cargado_en IS NULL"
            " AND bloque_inicio >= ? AND bloque_fin <= ?",
            (_now(), DONE, start.isoformat(), end.isoformat()),
        )
        return cursor.rowcount

    @staticmethod
    def _key(block: Block) -> tuple[str, str, str]:
        return block.destination_id, block.price_mode, block.start.isoformat()


class BlockLedger:
    """Counts the requests still in flight per block, and closes it at zero.

    A quarter is one unit of work but many requests: the server truncates, the
    window halves, and each half may halve again. Nothing knows a block is done
    except this count reaching zero.

    A single unreadable window taints the whole block. Retrying the quarter
    rather than the leaf costs requests and nothing else — and the alternative,
    recording a quarter as covered when part of it was never read, is the one
    failure that leaves no trace.
    """

    def __init__(self, store: CoverageStore):
        self.store = store
        self._flight: dict[Block, int] = {}
        self._rows: dict[Block, int] = defaultdict(int)
        self._requests: dict[Block, int] = defaultdict(int)
        self._errors: dict[Block, str] = {}

    def opened(self, block: Block) -> None:
        """The block's first request is on its way."""
        self._flight[block] = 1
        self._requests[block] = 1
        self._rows[block] = 0
        self._errors.pop(block, None)
        self.store.open_block(block)

    def split(self, block: Block, into: int = 2) -> None:
        """One response was truncated and became `into` narrower requests."""
        self._flight[block] += into - 1
        self._requests[block] += into

    def finished(self, block: Block, rows: int = 0) -> bool:
        """One request came back readable. True if that was the last one."""
        self._rows[block] += rows
        return self._settle(block)

    def failed(self, block: Block, error: str) -> bool:
        """One request could not be read. True if that was the last one."""
        self._errors.setdefault(block, error)
        return self._settle(block)

    def in_flight(self, block: Block) -> int:
        return self._flight.get(block, 0)

    def open_blocks(self) -> int:
        """How many blocks are still waiting on at least one request."""
        return len(self._flight)

    def _settle(self, block: Block) -> bool:
        self._flight[block] = self._flight.get(block, 1) - 1
        if self._flight[block] > 0:
            return False

        requests = self._requests.pop(block, 0)
        rows = self._rows.pop(block, 0)
        error = self._errors.pop(block, None)
        del self._flight[block]
        if error is None:
            self.store.close_block(block, rows=rows, requests=requests)
        else:
            self.store.fail_block(block, error, requests=requests)
        return True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> int:
    """How much is left, and what broke. Read between sessions, not during."""
    grid = plan_grid()
    with CoverageStore() as store:
        pending = store.pending(grid)
        counts = store.counts()
        failures = store.failures()

    share = (len(grid) - len(pending)) * 100 // len(grid) if grid else 0
    print(f"rejilla: {len(grid)} bloques")
    print(f"cubiertos: {len(grid) - len(pending)} ({share}%)")
    print(f"pendientes: {len(pending)}")
    print(f"estados: hechos {counts.done}, fallidos {counts.failed}, abiertos {counts.open}")
    if counts.unloaded:
        print(
            f"sin cargar: {counts.unloaded} bloque(s) barridos cuyo NDJSON sigue "
            f"solo en disco; `--solo-cargar` los mete a BigQuery"
        )
    if pending:
        print(f"siguiente: {pending[0].label}")
    for failure in failures:
        print(f"  ✗ {failure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
