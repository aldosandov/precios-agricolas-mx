"""Integrity of the versioned canonical catalogs (ALD-10).

Everything downstream keys off these ids, so a duplicate, a stale entry or a
mangled label is a data bug that would only surface much later as missing
prices. Regenerate with: uv run python -m scraper.catalogs
"""

import csv
from pathlib import Path

import pytest

from scraper.catalogs import (
    CATALOGS_DIR,
    build_products,
    fetch_form_html,
    split_product_label,
)

PRODUCTS_CSV = CATALOGS_DIR / "products.csv"

# SNIIM listed 222 products on 2026-08-05. This is a drift tripwire, not a
# law: if it fails, regenerate the catalog and update the number.
EXPECTED_PRODUCT_COUNT = 222


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def products() -> list[dict[str, str]]:
    return _read(PRODUCTS_CSV)


def test_product_count(products):
    assert len(products) == EXPECTED_PRODUCT_COUNT


def test_product_ids_are_unique_and_numeric(products):
    ids = [row["product_id"] for row in products]
    assert len(set(ids)) == len(ids)
    assert all(pid.isdigit() for pid in ids)


def test_product_labels_are_unique(products):
    labels = [row["label"] for row in products]
    assert len(set(labels)) == len(labels)


def test_name_and_quality_reconstruct_the_label(products):
    """The split is derived data; it must stay lossless against the source."""
    for row in products:
        assert f"{row['name']} - {row['quality']}" == row["label"]
        assert row["name"] and row["quality"]


def test_rows_are_sorted_by_id(products):
    """Keeps regeneration diffs meaningful."""
    ids = [int(row["product_id"]) for row in products]
    assert ids == sorted(ids)


def test_the_all_sentinel_is_not_a_product(products):
    assert "-1" not in {row["product_id"] for row in products}


def test_multi_dash_labels_split_on_the_last_separator():
    assert split_product_label("Nuez - Western - Primera") == ("Nuez - Western", "Primera")


def test_catalog_matches_the_live_form():
    """Live drift check: SNIIM added, removed or renamed something.

    Not a code failure — regenerate the catalog and review the diff.
    """
    live = {row["product_id"]: row["label"] for row in build_products(fetch_form_html())}
    stored = {row["product_id"]: row["label"] for row in _read(PRODUCTS_CSV)}
    assert live == stored, "products.csv is stale; run: uv run python -m scraper.catalogs"
