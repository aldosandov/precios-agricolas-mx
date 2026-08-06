"""Report to Sentry what each run captured, and what it did not.

An ingest that quietly stops capturing looks exactly like a source that
stopped publishing, and both look like a green job. So every run says out loud
what it got — rows, markets, dates — and what is missing: a market gone
silent, an empty window, a source that has not published in days.

The check-in also covers the case no log can: a job that never ran at all.
Sentry knows the schedule and complains when a run does not arrive.

Usage:
    uv run python -m transform.monitor --days 7            # tras el barrido
    uv run python -m transform.monitor --days 7 --outcome error
    uv run python -m transform.monitor --days 7 --dry-run  # imprime, no envía
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from scraper.query import source_date
from scraper.spiders.daily import FAILURES_PATH
from transform.load import RAW_TABLE

COVERAGE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "catalogs" / "market_coverage.csv"
)

# A market that reported at some point in the last two years is expected to
# keep reporting. Below that it is one of the seven the coverage scan found
# dead or retired (ALD-14), and alerting on those daily would only teach us to
# ignore the alert.
ACTIVE_SINCE_YEARS = 2

# Two business days behind is a late publication; three is a source that
# stopped. Mexican holidays are not modelled — the threshold absorbs them.
STALE_AFTER_BUSINESS_DAYS = 3

# Sentry needs the schedule to know when a run is missing, and it has to match
# .github/workflows/daily.yml.
MONITOR_SLUG = "ingesta-diaria-sniim"
MONITOR_SCHEDULE = "0 23 * * 1-5"
MONITOR_TIMEZONE = "UTC"


@dataclass(frozen=True)
class RunReport:
    """What one run of the daily job left behind, read back from BigQuery."""

    start: date
    end: date
    rows_by_market: dict[str, int]
    dates_with_data: tuple[date, ...]
    failed_windows: tuple[str, ...] = ()

    @property
    def rows(self) -> int:
        return sum(self.rows_by_market.values())

    @property
    def last_date(self) -> date | None:
        return max(self.dates_with_data) if self.dates_with_data else None


@dataclass(frozen=True)
class Alert:
    kind: str
    level: str
    message: str
    context: dict = field(default_factory=dict)


def active_markets(path: Path = COVERAGE_PATH, today: date | None = None) -> tuple[str, ...]:
    """Markets that should be reporting, per the coverage scan of ALD-14.

    Being in the catalog does not mean having data: two markets never had any
    and five stopped years ago.
    """
    floor = (today or source_date()).year - ACTIVE_SINCE_YEARS
    with path.open(encoding="utf-8", newline="") as handle:
        return tuple(
            row["destination_id"]
            for row in csv.DictReader(handle)
            if row["last_year"] and int(row["last_year"]) > floor
        )


def business_days_between(start: date, end: date) -> int:
    """Business days strictly after `start` and up to `end`.

    The source publishes Monday to Friday, so a weekend must not age the data.
    """
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def evaluate(
    report: RunReport,
    *,
    expected: Sequence[str],
    today: date,
    stale_after: int = STALE_AFTER_BUSINESS_DAYS,
) -> list[Alert]:
    """Turn a run into what is worth saying about it, most routine first."""
    alerts = [
        Alert(
            kind="resumen",
            level="info",
            message=(
                f"Ingesta {report.start:%Y-%m-%d}..{report.end:%Y-%m-%d}: "
                f"{report.rows} filas de {len(report.rows_by_market)} mercados"
            ),
            context={
                "ventana": f"{report.start:%Y-%m-%d}..{report.end:%Y-%m-%d}",
                "filas": report.rows,
                "mercados_con_datos": len(report.rows_by_market),
                "mercados_esperados": len(expected),
                "fechas_con_datos": len(report.dates_with_data),
                "ultima_fecha": report.last_date.isoformat() if report.last_date else None,
                "filas_por_mercado": dict(sorted(report.rows_by_market.items())),
            },
        )
    ]

    if not report.rows_by_market:
        # Naming all 43 markets here would bury the one thing worth reading.
        alerts.append(
            Alert(
                kind="sin_datos",
                level="error",
                message=(
                    f"Ventana {report.start:%Y-%m-%d}..{report.end:%Y-%m-%d} "
                    "sin una sola fila"
                ),
                context={"mercados_esperados": len(expected)},
            )
        )
    else:
        missing = sorted(set(expected) - set(report.rows_by_market))
        if missing:
            alerts.append(
                Alert(
                    kind="mercados_sin_datos",
                    level="warning",
                    message=f"{len(missing)} mercado(s) activos sin datos en la ventana",
                    context={"mercados": missing},
                )
            )

    stale = (
        business_days_between(report.last_date, today)
        if report.last_date
        else business_days_between(report.start, today)
    )
    if stale >= stale_after:
        alerts.append(
            Alert(
                kind="datos_atrasados",
                level="error",
                message=f"Sin datos nuevos desde hace {stale} días hábiles",
                context={
                    "dias_habiles": stale,
                    "ultima_fecha": report.last_date.isoformat() if report.last_date else None,
                },
            )
        )

    if report.failed_windows:
        # A crash and a gap need different fixes, so they travel apart.
        alerts.append(
            Alert(
                kind="mercados_fallidos",
                level="error",
                message=f"{len(report.failed_windows)} ventana(s) fallaron en el barrido",
                context={"ventanas": list(report.failed_windows)},
            )
        )
    return alerts


def emit(alerts: Sequence[Alert], *, outcome: str = "ok", dsn: str | None = None) -> None:
    """Send the alerts and check in, so a run that never happened is noticed."""
    import sentry_sdk
    from sentry_sdk.crons import capture_checkin

    if dsn:
        sentry_sdk.init(dsn=dsn, traces_sample_rate=0)

    capture_checkin(
        monitor_slug=MONITOR_SLUG,
        status=outcome,
        monitor_config={
            "schedule": {"type": "crontab", "value": MONITOR_SCHEDULE},
            "timezone": MONITOR_TIMEZONE,
        },
    )

    for alert in alerts:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("alerta", alert.kind)
            scope.set_context("ingesta", alert.context)
            sentry_sdk.capture_message(alert.message, level=alert.level)

    sentry_sdk.flush(timeout=10)


def read_failures(path: Path = FAILURES_PATH) -> tuple[str, ...]:
    """The windows the sweep could not read, as it wrote them down."""
    if not path.exists():
        return ()
    return tuple(line for line in path.read_text(encoding="utf-8").splitlines() if line)


def read_run(client, *, table: str, start: date, end: date) -> RunReport:
    """Ask BigQuery what actually landed, instead of trusting the spider's log."""
    query = f"""
    SELECT destino_id, fecha, COUNT(*) filas
    FROM `{table}`
    WHERE fecha BETWEEN DATE '{start:%Y-%m-%d}' AND DATE '{end:%Y-%m-%d}'
    GROUP BY destino_id, fecha
    """
    rows_by_market: dict[str, int] = {}
    dates = set()
    for row in client.query(query).result():
        key = str(row.destino_id)
        rows_by_market[key] = rows_by_market.get(key, 0) + row.filas
        dates.add(row.fecha)
    return RunReport(
        start=start,
        end=end,
        rows_by_market=rows_by_market,
        dates_with_data=tuple(sorted(dates)),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--table", default=RAW_TABLE)
    parser.add_argument(
        "--outcome",
        choices=("ok", "error"),
        default="ok",
        help="cómo terminó el barrido; 'error' marca el check-in en rojo",
    )
    parser.add_argument(
        "--failures-file",
        type=Path,
        default=FAILURES_PATH,
        help="archivo con las ventanas que falló el barrido, una por línea",
    )
    parser.add_argument("--dry-run", action="store_true", help="imprimir sin enviar")
    args = parser.parse_args(argv)

    from google.cloud import bigquery

    today = source_date()
    start = today - timedelta(days=args.days - 1)
    client = bigquery.Client(project=args.table.split(".")[0])
    report = read_run(client, table=args.table, start=start, end=today)
    report = RunReport(
        start=report.start,
        end=report.end,
        rows_by_market=report.rows_by_market,
        dates_with_data=report.dates_with_data,
        failed_windows=read_failures(args.failures_file),
    )

    alerts = evaluate(report, expected=active_markets(today=today), today=today)
    for alert in alerts:
        print(f"[{alert.level}] {alert.message}")
        print(f"        {json.dumps(alert.context, ensure_ascii=False)[:300]}")

    if args.dry_run:
        return 0
    if not os.environ.get("SENTRY_DSN"):
        # Sin DSN no hay a dónde reportar, y callarlo dejaría la corrida sin
        # rastro justo cuando algo falta.
        print("SENTRY_DSN no configurado: nada se envió", file=sys.stderr)
        return 1

    emit(alerts, outcome=args.outcome, dsn=os.environ["SENTRY_DSN"])
    print(f"{len(alerts)} alerta(s) enviadas a Sentry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
