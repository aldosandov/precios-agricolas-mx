"""The progress console for long runs (ALD-53).

The backfill runs for days on a laptop, so the console stops being a log and
becomes an interface. What is pinned here is what makes it usable rather than
pretty: it stays off unless asked for, a failure is announced once and not
again on every tick, and a session redirected to a file degrades into plain
readable lines instead of a smear of escape codes.

Runs offline. The reactor never starts and no spider is crawled.
"""

import io
import re

import pytest
from rich.console import Console
from scrapy.exceptions import NotConfigured
from scrapy.settings import Settings
from scrapy.signalmanager import SignalManager

from scraper.console import ProgressConsole, Snapshot

ANSI = re.compile(r"\x1b\[")


class FakeSpider:
    """A spider that reports its own progress, as the backfill will."""

    def __init__(self, done=0, total=None, label="", failures=None):
        self.snapshot = Snapshot(done=done, total=total, label=label)
        self.failures = failures if failures is not None else []

    def progress(self) -> Snapshot:
        return self.snapshot


class FakeCrawler:
    def __init__(self, **settings):
        self.settings = Settings(settings)
        self.signals = SignalManager(self)
        self.stats = None


class FakeClock:
    """Elapsed time we control, so the rate and the ETA are checkable numbers."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sink() -> io.StringIO:
    return io.StringIO()


def _console(sink, *, interval=30, clock=None, spider=None):
    extension = ProgressConsole(
        interval=interval,
        console=Console(file=sink, width=100, force_terminal=False),
        clock=clock or FakeClock(),
    )
    extension.spider = spider
    return extension


# --- off unless asked for ---


def test_the_console_stays_out_of_the_way_unless_a_run_asks_for_it():
    """Actions runs the daily sweep and reads a plain log; a bar there is noise."""
    with pytest.raises(NotConfigured):
        ProgressConsole.from_crawler(FakeCrawler())


def test_a_run_that_asks_for_it_gets_it():
    extension = ProgressConsole.from_crawler(
        FakeCrawler(PROGRESS_CONSOLE_ENABLED=True, PROGRESS_CONSOLE_INTERVAL=5)
    )

    assert extension.interval == 5


def test_the_extension_is_registered_and_off_in_the_project_settings():
    settings = Settings()
    settings.setmodule("scraper.settings")

    assert "scraper.console.ProgressConsole" in settings.getdict("EXTENSIONS")
    assert not settings.getbool("PROGRESS_CONSOLE_ENABLED")


# --- what Scrapy actually calls ---


def test_scrapy_delivers_its_signals_to_handlers_that_ignore_the_arguments():
    """The handlers take no `spider`/`item`; a signal that silently reaches
    nobody would leave the bar frozen at zero for the whole run."""
    crawler = FakeCrawler(PROGRESS_CONSOLE_ENABLED=True, PROGRESS_CONSOLE_INTERVAL=30)
    extension = ProgressConsole.from_crawler(crawler)
    spider = FakeSpider()

    from scrapy import signals

    crawler.signals.send_catch_log(signals.item_scraped, item={}, response=None, spider=spider)
    crawler.signals.send_catch_log(signals.response_received, response=None, request=None,
                                   spider=spider)

    assert extension.items == 1
    assert extension.responses == 1


# --- failures: once each, never again ---


def test_every_failure_is_announced_the_moment_it_appears(sink):
    spider = FakeSpider()
    extension = _console(sink, spider=spider)

    spider.failures.append("destino=71 ventana=2011-04-01..2011-06-30: 503")
    extension.tick()

    assert "destino=71" in sink.getvalue()


def test_a_failure_is_not_reprinted_on_every_tick(sink):
    """A list that only grows, redrawn each second, would push everything else
    off the screen within a minute."""
    spider = FakeSpider(failures=["destino=71: 503"])
    extension = _console(sink, spider=spider)

    extension.tick()
    spider.failures.append("destino=122: NoPaginator")
    extension.tick()
    extension.tick()

    assert sink.getvalue().count("destino=71") == 1
    assert sink.getvalue().count("destino=122") == 1


# --- degrading when nobody is watching ---


def test_a_redirected_run_prints_plain_lines_with_no_escape_codes(sink):
    clock = FakeClock()
    spider = FakeSpider(done=4218, total=9712, failures=["destino=71: 503"])
    extension = _console(sink, interval=30, clock=clock, spider=spider)

    extension.tick()
    clock.now = 31
    extension.tick()

    assert not ANSI.search(sink.getvalue())
    assert "4 218/9 712" in sink.getvalue()


def test_the_status_line_waits_out_its_interval(sink):
    """Redirected to a file, a line per tick is thousands of identical rows."""
    clock = FakeClock()
    extension = _console(sink, interval=30, clock=clock, spider=FakeSpider(done=1, total=10))

    extension.tick()
    clock.now = 5
    extension.tick()
    clock.now = 40
    extension.tick()

    assert sink.getvalue().count("bloques") + sink.getvalue().count("/10") == 2


def test_the_first_line_does_not_wait_for_the_interval(sink):
    """A run that says nothing for half a minute looks like a run that hung."""
    extension = _console(sink, interval=30, spider=FakeSpider(done=0, total=10))

    extension.tick()

    assert sink.getvalue().strip()


# --- the live area, when somebody is watching ---


def test_the_live_area_shows_the_block_being_worked_on(sink):
    clock = FakeClock()
    spider = FakeSpider(done=4218, total=9712, label="2011-Q3 · destino 210")
    extension = ProgressConsole(
        console=Console(file=sink, width=110, force_terminal=True), clock=clock
    )
    extension.spider = spider
    extension.items = 1412

    extension._open_live()
    clock.now = 60
    extension.tick()

    drawn = sink.getvalue()
    assert "2011-Q3 · destino 210" in drawn
    # Grouped in the bar too, not just in the plain line: six-digit block counts
    # run together otherwise.
    assert "4 218/9 712" in drawn
    assert "1 412 filas/min" in drawn


def test_the_live_estimate_comes_from_the_whole_run_not_a_recent_window(sink):
    """Rich estimates from a short sample window; over a run measured in days
    that swings by hours between one redraw and the next."""
    clock = FakeClock()
    spider = FakeSpider(done=2428, total=9712)
    extension = ProgressConsole(
        console=Console(file=sink, width=110, force_terminal=True), clock=clock
    )
    extension.spider = spider

    extension._open_live()
    clock.now = 3600
    extension.tick()

    # A quarter done in an hour: three hours left.
    assert "3h00m" in sink.getvalue()


# --- the numbers ---


def test_the_line_carries_progress_rate_and_eta(sink):
    clock = FakeClock()
    spider = FakeSpider(done=25, total=100)
    extension = _console(sink, clock=clock, spider=spider)
    extension.items = 600
    clock.now = 60

    line = extension._plain_line()

    assert "25/100" in line
    assert "25%" in line
    # A quarter done in a minute: three minutes left, 600 rows in that minute.
    assert "600 filas/min" in line
    assert "ETA 3m00s" in line


def test_an_unknown_total_reports_what_is_done_and_no_percentage(sink):
    """A spider that discovers work as it subdivides cannot be asked for a
    denominator, and inventing one would make the bar lie."""
    spider = FakeSpider(done=42)
    extension = _console(sink, spider=spider)

    line = extension._plain_line()

    assert "42 bloques" in line
    assert "ETA" not in line
    assert extension.eta_seconds(spider.progress()) is None


def test_a_spider_that_reports_nothing_falls_back_to_counting_responses(sink):
    class Mute:
        failures = []

    extension = _console(sink, spider=Mute())
    extension.responses = 7

    assert extension.snapshot() == Snapshot(done=7)


def test_six_digit_counts_stay_readable(sink):
    extension = _console(sink, spider=FakeSpider(done=1234567, total=9712000))

    assert "1 234 567" in extension._plain_line()
