"""Traceability of each run: what was captured and what is missing (ALD-25).

An ingest that quietly stops capturing looks exactly like a source that
stopped publishing, and both look like a green job. So every run reports what
it got — rows, markets, dates — and says out loud what it did not get: a
market that went silent, a window with nothing in it, a source that has not
published in days.

The rules are pure and tested here. The Sentry wiring is tested with a fake
transport, so nothing leaves the machine.
"""

from datetime import date

import pytest

from transform.monitor import (
    STALE_AFTER_BUSINESS_DAYS,
    RunReport,
    active_markets,
    business_days_between,
    evaluate,
)

TODAY = date(2026, 8, 11)  # martes


def _report(**overrides) -> RunReport:
    defaults = dict(
        start=date(2026, 8, 5),
        end=TODAY,
        rows_by_market={"210": 120, "100": 98},
        dates_with_data=(date(2026, 8, 10), TODAY),
        failed_windows=(),
    )
    return RunReport(**{**defaults, **overrides})


def _kinds(alerts) -> set[str]:
    return {alert.kind for alert in alerts}


def _find(alerts, kind):
    return next(alert for alert in alerts if alert.kind == kind)


# --- who is expected to report ---


def test_markets_that_never_had_data_are_not_expected():
    """Tapachula and Chilpancingo are in the catalog and have never published;
    alerting on them daily would train the alert to be ignored."""
    expected = active_markets()

    assert "71" not in expected
    assert "122" not in expected


def test_markets_that_stopped_reporting_years_ago_are_not_expected():
    for retired in ("111", "112", "231"):  # Irapuato, Celaya, Cancún
        assert retired not in active_markets()


def test_the_markets_still_reporting_are_expected():
    expected = active_markets()

    assert "210" in expected  # Central de Abasto de Puebla
    assert len(expected) == 43


# --- counting business days, because the source only publishes on them ---


def test_a_weekend_does_not_age_the_data():
    assert business_days_between(date(2026, 8, 7), date(2026, 8, 9)) == 0  # vie -> dom


def test_monday_is_one_business_day_after_friday():
    assert business_days_between(date(2026, 8, 7), date(2026, 8, 10)) == 1


def test_business_days_accumulate_across_a_week():
    assert business_days_between(date(2026, 8, 5), TODAY) == 4


# --- the run summary always goes out ---


def test_a_healthy_run_reports_a_summary_and_nothing_else():
    alerts = evaluate(_report(), expected=("210", "100"), today=TODAY)

    assert _kinds(alerts) == {"resumen"}
    assert _find(alerts, "resumen").level == "info"


def test_the_summary_carries_what_was_captured():
    alerts = evaluate(_report(), expected=("210", "100"), today=TODAY)

    context = _find(alerts, "resumen").context
    assert context["filas"] == 218
    assert context["mercados_con_datos"] == 2
    assert context["fechas_con_datos"] == 2
    assert context["ultima_fecha"] == "2026-08-11"


# --- what is missing ---


def test_a_market_that_reported_nothing_in_the_window_is_named():
    alerts = evaluate(_report(), expected=("210", "100", "20"), today=TODAY)

    assert _find(alerts, "mercados_sin_datos").level == "warning"
    assert _find(alerts, "mercados_sin_datos").context["mercados"] == ["20"]


def test_an_empty_window_is_an_error_and_not_a_list_of_43_markets():
    """With nothing loaded, naming every market buries the real message."""
    alerts = evaluate(
        _report(rows_by_market={}, dates_with_data=()),
        expected=("210", "100"),
        today=TODAY,
    )

    assert "sin_datos" in _kinds(alerts)
    assert "mercados_sin_datos" not in _kinds(alerts)
    assert _find(alerts, "sin_datos").level == "error"


def test_a_source_that_stopped_publishing_is_an_error():
    stale = _report(dates_with_data=(date(2026, 8, 4),))

    alerts = evaluate(stale, expected=("210", "100"), today=TODAY)

    assert _find(alerts, "datos_atrasados").level == "error"
    assert _find(alerts, "datos_atrasados").context["dias_habiles"] >= STALE_AFTER_BUSINESS_DAYS


def test_a_holiday_does_not_raise_a_stale_alert():
    """Two business days behind is a late publication, not a broken ingest."""
    alerts = evaluate(
        _report(dates_with_data=(date(2026, 8, 7),)), expected=("210",), today=TODAY
    )

    assert "datos_atrasados" not in _kinds(alerts)


def test_markets_that_broke_during_the_sweep_are_reported_apart():
    """Missing data and a crash are different problems with different fixes."""
    broken = _report(failed_windows=("destino=100 precio=2: UnknownColumn: 'Precio Modal'",))

    alerts = evaluate(broken, expected=("210", "100"), today=TODAY)

    assert _find(alerts, "mercados_fallidos").level == "error"
    assert "Precio Modal" in str(_find(alerts, "mercados_fallidos").context["ventanas"])


# --- what reaches Sentry ---


@pytest.fixture
def captured():
    """Initialise Sentry with a transport that keeps envelopes in memory.

    Check-ins only travel as envelope items, so a transport that looks at
    events alone would report success while nothing checked in.
    """
    import sentry_sdk
    from sentry_sdk.transport import Transport

    sent = []

    class Recording(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                payload = dict(item.payload.json or {})
                payload.setdefault("type", item.type)
                sent.append(payload)

    client = sentry_sdk.Client(
        dsn="https://a@example.test/1", transport=Recording(), traces_sample_rate=0
    )
    sentry_sdk.get_global_scope().set_client(client)
    yield sent
    sentry_sdk.get_global_scope().set_client(None)


def test_every_alert_becomes_a_sentry_event(captured):
    from transform.monitor import emit

    emit(evaluate(_report(), expected=("210", "20"), today=TODAY), outcome="ok")

    kinds = {event["tags"]["alerta"] for event in captured if "tags" in event}
    assert {"resumen", "mercados_sin_datos"} <= kinds


def test_the_event_carries_its_level_and_context(captured):
    from transform.monitor import emit

    emit(evaluate(_report(), expected=("210", "100"), today=TODAY), outcome="ok")

    summary = next(e for e in captured if e.get("tags", {}).get("alerta") == "resumen")
    assert summary["level"] == "info"
    assert summary["contexts"]["ingesta"]["filas"] == 218


def test_the_run_checks_in_so_a_job_that_never_ran_is_noticed(captured):
    from transform.monitor import emit

    emit([], outcome="ok")

    check_ins = [e for e in captured if e.get("type") == "check_in"]
    assert check_ins
    assert check_ins[0]["status"] == "ok"
    assert check_ins[0]["monitor_config"]["schedule"]["value"] == "0 23 * * 1-5"


def test_a_failed_sweep_checks_in_as_an_error(captured):
    from transform.monitor import emit

    emit([], outcome="error")

    assert [e for e in captured if e.get("type") == "check_in"][0]["status"] == "error"
