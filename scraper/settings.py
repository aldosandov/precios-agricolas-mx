"""Scrapy settings for the ingest.

The source is one server of a government department, it answers 503 when
pushed, and the project has no deadline that a slower backfill would miss. So
the crawl is deliberately slow and identifiable: two requests at a time, three
seconds apart, autothrottle widening the gap whenever the server takes longer,
and a user agent that says who is asking and where to complain.

The HTTP cache exists for the backfill, which runs for hours and has to resume
without downloading again. It is off here on purpose: the daily run has to see
today's prices, not a copy of yesterday's.
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

# The cache is for the backfill: hours of requests that must not be repeated
# after a crash. The backfill spider turns it on for itself; leaving it on here
# would make the daily run replay yesterday's copy of today's prices.
HTTPCACHE_ENABLED = False
HTTPCACHE_DIR = "httpcache"
# Prices of 2009 do not change while a backfill runs, and a stale entry is
# better than asking the source twice for the same window.
HTTPCACHE_EXPIRATION_SECS = 0
# Never cache a failure: a stored 503 would replay as a lost window on every
# resume, which is the one way this cache could lose data.
HTTPCACHE_IGNORE_HTTP_CODES = [500, 502, 503, 504, 408, 429]

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"
