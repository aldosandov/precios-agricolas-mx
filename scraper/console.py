"""Show a long run as progress instead of as a log (ALD-53).

The backfill is ~9 700 blocks spread over several days of sessions on a laptop,
and during those hours the console is an interface, not a log: what matters is
how much is left and what broke, not the rows going by. Scrapy's own log at
INFO buries both.

So the log goes to a file and this extension owns the terminal — a live bar,
the block being worked on, and one line per failure. No tracebacks on screen:
that is what `LOG_FILE` is for.

Off by default. The backfill turns it on; the daily sweep leaves it off,
because in GitHub Actions a plain log reads better than a bar nobody watches.

A spider is free to know nothing about this. Two optional members are read if
they exist, and their absence only costs detail:

    failures: list[str]     lines to print, one per window the spider lost
    progress() -> Snapshot  how far along the work is, in the spider's own unit

Without `progress()` the bar has no total to fill: responses are counted, but
"how much is left" is a question only the spider can answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from scrapy import signals
from scrapy.exceptions import NotConfigured

# How often the status line is printed when nothing is watching it live. Long
# on purpose: a session redirected to a file is read afterwards, and a line per
# second would bury the failures among thousands of identical rows.
DEFAULT_INTERVAL = 30

# How often the live area is redrawn. The spinner has to move and the elapsed
# clock has to tick, and a second is below what an eye notices.
LIVE_INTERVAL = 1


@dataclass(frozen=True)
class Snapshot:
    """How far along a spider thinks it is, in whatever unit it counts in.

    `total` is optional because a spider may not know: a sweep that subdivides
    its windows discovers work as it goes. An unknown total shows as a bar with
    no end rather than as a made-up percentage.
    """

    done: int
    total: int | None = None
    label: str = ""


def _grouped(value: int) -> str:
    """9712 as '9 712'. Six digits of row counts are unreadable otherwise."""
    return f"{value:,}".replace(",", " ")


def _duration(seconds: float) -> str:
    """A rough span, for an estimate that is rough to begin with."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


class ProgressConsole:
    """Scrapy extension: the run's progress, and nothing else, on the terminal."""

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        console=None,
        clock=time.monotonic,
    ):
        from rich.console import Console

        self.console = console if console is not None else Console()
        self.interval = interval
        self.clock = clock
        self.started = clock()
        self.items = 0
        self.responses = 0
        self.spider = None
        # How many of the spider's failures have already reached the screen, so
        # a list that only grows is not reprinted from the top on every tick.
        self.reported = 0
        # Elapsed seconds at the last plain line, so the interval is measured
        # from what was printed and not from what the reactor felt like ticking.
        self._last_plain: float | None = None
        self._live = None
        self._progress = None
        self._task = None
        self._loop = None

    @classmethod
    def from_crawler(cls, crawler):
        if not crawler.settings.getbool("PROGRESS_CONSOLE_ENABLED"):
            raise NotConfigured("PROGRESS_CONSOLE_ENABLED is off")

        extension = cls(interval=crawler.settings.getfloat("PROGRESS_CONSOLE_INTERVAL"))
        extension.crawler = crawler
        crawler.signals.connect(extension.spider_opened, signals.spider_opened)
        crawler.signals.connect(extension.spider_closed, signals.spider_closed)
        crawler.signals.connect(extension.item_scraped, signals.item_scraped)
        crawler.signals.connect(extension.response_received, signals.response_received)
        return extension

    # --- what the crawl tells us ---

    def item_scraped(self):
        self.items += 1

    def response_received(self):
        self.responses += 1

    def spider_opened(self, spider):
        from twisted.internet.task import LoopingCall

        self.spider = spider
        self.started = self.clock()
        if self.console.is_terminal:
            self._open_live()
        self._loop = LoopingCall(self.tick)
        self._loop.start(LIVE_INTERVAL if self.console.is_terminal else self.interval)

    def spider_closed(self):
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._drain_failures()
        if self._live is not None:
            self._live.stop()
            self._live = None
        # The last word is a plain line in both modes: the live area is gone by
        # now, and a session read back from a file has to end with a number.
        self._say(self._plain_line())

    # --- the tick ---

    def tick(self) -> None:
        """Print what is new and redraw what changed. Safe to call at any time."""
        self._drain_failures()
        if self._live is not None:
            self._refresh_live()
        elif self._due():
            self._say(self._plain_line())

    def _say(self, line: str) -> None:
        # Highlighting off: rich would colour every digit of a status line one
        # by one, which in a file is a smear of escape codes around the numbers
        # that are the only thing worth reading.
        self.console.print(line, highlight=False)

    def _due(self) -> bool:
        elapsed = self.clock() - self.started
        # The first tick fires immediately so a redirected run says something
        # before the first interval has gone by.
        due = self._last_plain is None or elapsed - self._last_plain >= self.interval
        if due:
            self._last_plain = elapsed
        return due

    def _drain_failures(self) -> None:
        """One line per failure, the moment it appears, above everything else."""
        failures = list(getattr(self.spider, "failures", []))
        for failure in failures[self.reported :]:
            self.console.print(f"[bold red]✗[/] {failure}", highlight=False)
        self.reported = len(failures)

    # --- what we know ---

    def snapshot(self) -> Snapshot:
        """What the spider says, or what can be counted without asking it."""
        report = getattr(self.spider, "progress", None)
        if callable(report):
            return report()
        return Snapshot(done=self.responses)

    def rows_per_minute(self) -> float:
        elapsed = self.clock() - self.started
        return self.items * 60 / elapsed if elapsed > 0 else 0.0

    def eta_seconds(self, snapshot: Snapshot) -> float | None:
        """Seconds left at the pace so far, or None when the total is unknown."""
        elapsed = self.clock() - self.started
        if snapshot.total is None or snapshot.done <= 0 or elapsed <= 0:
            return None
        remaining = max(snapshot.total - snapshot.done, 0)
        return remaining * elapsed / snapshot.done

    def retries(self) -> int:
        stats = getattr(getattr(self, "crawler", None), "stats", None)
        return stats.get_value("retry/count", 0) if stats is not None else 0

    # --- rendering ---

    def _plain_line(self) -> str:
        """One self-contained line, for a run nobody is watching live.

        Nothing is overwritten and no escape code is emitted: redirected to a
        file this stays a readable history of the session.
        """
        snapshot = self.snapshot()
        stamp = time.strftime("%H:%M:%S")
        parts = [stamp]
        if snapshot.total:
            share = snapshot.done * 100 // snapshot.total
            parts.append(f"{_grouped(snapshot.done)}/{_grouped(snapshot.total)}  {share}%")
        else:
            parts.append(f"{_grouped(snapshot.done)} bloques")
        parts.append(f"{_grouped(round(self.rows_per_minute()))} filas/min")
        eta = self.eta_seconds(snapshot)
        if eta is not None:
            parts.append(f"ETA {_duration(eta)}")
        parts.append(f"fallos {len(getattr(self.spider, 'failures', []))}")
        return "  ".join(parts)

    def _open_live(self) -> None:
        from rich.live import Live
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            # Counts and estimate come from this extension, not from Progress:
            # its own columns group nothing and estimate from a short window of
            # samples, which for a run measured in days swings by hours.
            TextColumn("{task.fields[counts]}", style="green"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TextColumn("←"),
            TextColumn("{task.fields[eta]}", style="cyan"),
            console=self.console,
            # Driven by the Live below; letting Progress refresh itself too
            # would have two threads drawing over each other.
            auto_refresh=False,
        )
        self._task = self._progress.add_task(
            "Backfill", total=None, counts="", eta=""
        )
        self._live = Live(self._renderable(), console=self.console, refresh_per_second=4)
        self._live.start()

    def _refresh_live(self) -> None:
        snapshot = self.snapshot()
        eta = self.eta_seconds(snapshot)
        self._progress.update(
            self._task,
            completed=snapshot.done,
            total=snapshot.total,
            counts=(
                f"{_grouped(snapshot.done)}/{_grouped(snapshot.total)}"
                if snapshot.total
                else _grouped(snapshot.done)
            ),
            eta=_duration(eta) if eta is not None else "—",
        )
        # Drawn now rather than left to Live's own refresh thread: the numbers
        # change once a second and should appear then, not a quarter of a
        # second later on whichever redraw happens to come next.
        self._live.update(self._renderable(), refresh=True)

    def _renderable(self):
        from rich.console import Group
        from rich.text import Text

        snapshot = self.snapshot()
        detail = Text(no_wrap=True)
        detail.append("  ")
        if snapshot.label:
            detail.append(snapshot.label, style="cyan")
            detail.append(" · ")
        detail.append(f"{_grouped(round(self.rows_per_minute()))} filas/min", style="dim")

        failures = len(getattr(self.spider, "failures", []))
        counters = Text("  ", no_wrap=True)
        counters.append(f"hechos {_grouped(snapshot.done)}", style="green")
        counters.append(" · ")
        counters.append(f"fallidos {failures}", style="red" if failures else "dim")
        counters.append(" · ")
        counters.append(f"reintentos {self.retries()}", style="dim")
        return Group(self._progress, detail, counters)
