"""Scrapy settings for the ingest.

Only what correctness depends on lives here for now. Throttling, concurrency,
the HTTP cache the backfill resumes from, and the institutional user agent are
ALD-20's job.
"""

BOT_NAME = "precios-agricolas-mx"

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

# SNIIM answers 503 under load and the daily job is small; retrying the few
# that fail is cheaper than losing a market for the day.
RETRY_ENABLED = True
RETRY_TIMES = 4
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

# Responses of a whole quarter for one market run into megabytes.
DOWNLOAD_TIMEOUT = 180

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"
