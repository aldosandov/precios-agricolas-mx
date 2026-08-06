"""Scrapy settings for the ingest.

The source is one server of a government department, it answers 503 when
pushed, and the project has no deadline that a slower backfill would miss. So
the crawl is deliberately slow and identifiable: two requests at a time, three
seconds apart, autothrottle widening the gap whenever the server takes longer,
and a user agent that says who is asking and where to complain.

The HTTP cache is off, and stays off in both spiders. It was put in for the
backfill, to resume without downloading again; the coverage table of ALD-26
does that for a few hundred KB, where caching 9 600 multi-megabyte responses
would cost 20+ GB on the laptop that runs it. What is left of the cache is a
convenience for repeating one run by hand (`--cache`), which is why the daily
sweep must not have it on: it has to see today's prices, not a copy.
"""

BOT_NAME = "precios-agricolas-mx"

# Says who is asking and points at the issue tracker of the project. The source
# is a government department, not an anonymous target.
USER_AGENT = (
    "precios-agricolas-mx/0.1 "
    "(+https://github.com/aldosandov/precios-agricolas-mx)"
)

SPIDER_MODULES = ["scraper.spiders"]
NEWSPIDER_MODULE = "scraper.spiders"

ROBOTSTXT_OBEY = True

# The results page is stateless: every criterion travels in the URL. The site
# sends an ASP.NET session cookie it never requires (docs/consulta-sniim.md),
# and keeping it would only invite the server to behave differently per run.
COOKIES_ENABLED = False

ITEM_PIPELINES = {
    "scraper.pipelines.ndjson.NdjsonPartitionPipeline": 100,
}

# The progress console, off unless a run asks for it. The backfill turns it on
# because it runs for days on a laptop somebody is looking at; the daily sweep
# leaves it off, because in GitHub Actions a plain log reads better than a bar
# nobody watches. Turning it on without also setting LOG_FILE puts Scrapy's log
# and the live area on the same terminal, and neither survives.
EXTENSIONS = {
    "scraper.console.ProgressConsole": 100,
}
PROGRESS_CONSOLE_ENABLED = False
# Seconds between status lines when the output is not a terminal. A session
# redirected to a file is read afterwards, so a line per second would bury the
# failures among thousands of identical rows.
PROGRESS_CONSOLE_INTERVAL = 30

# Where the NDJSON partitions land. Not versioned.
RAW_OUTPUT_DIR = "out/raw"

# Two at a time and three seconds apart. The backfill is ~7 450 requests and
# would finish hours earlier if pushed, but the source is a public service and
# a block would cost the whole project.
CONCURRENT_REQUESTS = 2
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_DELAY = 3

# Autothrottle adapts the delay to how slow the server is answering: a quarter
# of one market takes seconds to build, and hammering it while it works is
# exactly how the 503s start.
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 3
# A 503 needs minutes to clear, not seconds. Same order as the backoff that
# scraper/coverage.py needed to survive its 1 800 probes.
AUTOTHROTTLE_MAX_DELAY = 120
# One request in flight on average; the concurrency of 2 is the ceiling, not
# the goal.
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0

# SNIIM answers 503 under load and the daily job is small; retrying the few
# that fail is cheaper than losing a market for the day.
RETRY_ENABLED = True
RETRY_TIMES = 4
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

# Responses of a whole quarter for one market run into megabytes.
DOWNLOAD_TIMEOUT = 180

# Off, and not turned on by either spider. Resuming the backfill is what
# `out/cobertura.sqlite` is for; this is only for replaying one run by hand
# without asking the source again (`--cache`), and leaving it on would make the
# daily run serve yesterday's copy of today's prices.
HTTPCACHE_ENABLED = False
HTTPCACHE_DIR = "httpcache"
# Prices of a window already fetched do not change while it is being replayed,
# and a stale entry is better than asking the source twice for the same window.
HTTPCACHE_EXPIRATION_SECS = 0
# Never cache a failure: a stored 503 would replay as a lost window on every
# resume, which is the one way this cache could lose data.
HTTPCACHE_IGNORE_HTTP_CODES = [500, 502, 503, 504, 408, 429]

TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
