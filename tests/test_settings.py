"""Politeness settings of the crawl (ALD-20).

The source is a government department with one server, and it answers 503 when
pushed. These values are the difference between an ingest that takes longer and
one that gets blocked, so they are pinned here: a careless edit that raises the
concurrency or drops the delay fails the suite instead of the backfill.
"""

import pytest
from scrapy.settings import Settings

from scraper.settings import USER_AGENT


@pytest.fixture(scope="module")
def settings() -> Settings:
    loaded = Settings()
    loaded.setmodule("scraper.settings")
    return loaded


# --- politeness ---


def test_autothrottle_is_on(settings):
    assert settings.getbool("AUTOTHROTTLE_ENABLED")


def test_no_more_than_two_requests_at_a_time(settings):
    assert settings.getint("CONCURRENT_REQUESTS") == 2
    assert settings.getint("CONCURRENT_REQUESTS_PER_DOMAIN") == 2


def test_at_least_three_seconds_between_requests(settings):
    assert settings.getfloat("DOWNLOAD_DELAY") >= 3
    assert settings.getfloat("AUTOTHROTTLE_START_DELAY") >= 3


def test_autothrottle_backs_off_far_enough_for_a_503(settings):
    """SNIIM returns 503 under load and needs minutes, not seconds, to recover."""
    assert settings.getfloat("AUTOTHROTTLE_MAX_DELAY") >= 60


def test_the_crawl_stays_sequential_on_average(settings):
    assert settings.getfloat("AUTOTHROTTLE_TARGET_CONCURRENCY") <= 1.0


# --- identity ---


def test_the_user_agent_says_who_is_asking_and_where_to_complain():
    assert "precios-agricolas-mx" in USER_AGENT
    assert "https://github.com/aldosandov/precios-agricolas-mx" in USER_AGENT


def test_the_user_agent_is_not_scrapys_default(settings):
    assert "Scrapy" not in settings.get("USER_AGENT")


# --- the cache the backfill resumes from ---


def test_the_http_cache_is_configured_but_off_by_default(settings):
    """The daily run must see today's prices, not yesterday's copy of them.
    The backfill turns it on for itself (PRD §8.6)."""
    assert not settings.getbool("HTTPCACHE_ENABLED")
    assert settings.get("HTTPCACHE_DIR")


def test_failed_responses_are_never_cached(settings):
    """A cached 503 would replay as a lost window on every resume."""
    ignored = set(settings.getlist("HTTPCACHE_IGNORE_HTTP_CODES"))

    assert {"500", "502", "503", "504"} <= {str(code) for code in ignored}


def test_the_cache_never_expires_on_its_own(settings):
    """A backfill runs for hours; prices of 2009 do not change while it does."""
    assert settings.getint("HTTPCACHE_EXPIRATION_SECS") == 0


# --- what ALD-19 already depended on ---


def test_robots_is_obeyed_and_cookies_stay_off(settings):
    assert settings.getbool("ROBOTSTXT_OBEY")
    assert not settings.getbool("COOKIES_ENABLED")
