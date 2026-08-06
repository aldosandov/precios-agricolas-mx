"""Which day the ingest asks for, when the machine asking is not in Mexico.

GitHub's runners are on UTC and the SNIIM publishes on Mexico City time. After
18:00 in Mexico the two calendars disagree, so a run that used the machine's
date would ask for a day the source has not published yet: zero rows, zero
partitions, and a green job with nothing to show.

This is not hypothetical — it is what the first production dispatch did.
"""

from datetime import UTC, date, datetime

from scraper.query import SOURCE_TIMEZONE, source_date
from scraper.spiders.daily import DailySpider


def test_late_evening_in_mexico_is_still_the_same_day():
    """02:00 UTC is 20:00 of the previous day in Mexico, and the source's day
    is the one that matters."""
    assert source_date(datetime(2026, 8, 6, 2, 0, tzinfo=UTC)) == date(2026, 8, 5)


def test_midday_agrees_with_utc():
    assert source_date(datetime(2026, 8, 5, 18, 0, tzinfo=UTC)) == date(2026, 8, 5)


def test_the_scheduled_hour_lands_on_the_business_day_it_meant_to():
    """The cron fires at 23:00 UTC, which is 17:00 in Mexico of the same day."""
    assert source_date(datetime(2026, 8, 5, 23, 0, tzinfo=UTC)) == date(2026, 8, 5)


def test_a_late_run_does_not_slip_into_tomorrow():
    """Actions delays scheduled jobs routinely; an hour late used to move the
    window to a day the SNIIM has not published."""
    assert source_date(datetime(2026, 8, 6, 0, 30, tzinfo=UTC)) == date(2026, 8, 5)


def test_the_timezone_is_the_source_s_and_not_the_runner_s():
    assert str(SOURCE_TIMEZONE) == "America/Mexico_City"


def test_the_sweep_window_ends_on_the_sources_today():
    spider = DailySpider(days=1, destinations=["210"])

    assert spider.window.end == source_date()
