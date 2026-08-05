"""Integrity of the versioned canonical catalogs (ALD-10, ALD-11).

Everything downstream keys off these ids, so a duplicate, a stale entry or a
mangled label is a data bug that would only surface much later as missing
prices. Regenerate with: uv run python -m scraper.catalogs
"""

import csv
from pathlib import Path

import pytest

from scraper.catalogs import (
    CATALOGS_DIR,
    build_origins,
    build_products,
    fetch_form_html,
    split_product_label,
)

# Counts observed on 2026-08-05. Drift tripwires, not laws: if one fails,
# regenerate the catalog and update the number.
CATALOG_SPECS = {
    "products": ("products.csv", "product_id", build_products, 222),
    "origins": ("origins.csv", "origin_id", build_origins, 35),
}


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _rows(name: str) -> list[dict[str, str]]:
    return _read(CATALOGS_DIR / CATALOG_SPECS[name][0])


@pytest.fixture(scope="module")
def products() -> list[dict[str, str]]:
    return _rows("products")


@pytest.fixture(scope="module")
def origins() -> list[dict[str, str]]:
    return _rows("origins")


@pytest.fixture(scope="module")
def form_html() -> str:
    return fetch_form_html()


# --- invariants every catalog must hold ---


@pytest.mark.parametrize("catalog", CATALOG_SPECS)
def test_entry_count(catalog):
    _, _, _, expected = CATALOG_SPECS[catalog]
    assert len(_rows(catalog)) == expected


@pytest.mark.parametrize("catalog", CATALOG_SPECS)
def test_ids_are_unique_and_numeric(catalog):
    _, id_field, _, _ = CATALOG_SPECS[catalog]
    ids = [row[id_field] for row in _rows(catalog)]
    assert len(set(ids)) == len(ids)
    assert all(entry_id.isdigit() for entry_id in ids)


@pytest.mark.parametrize("catalog", CATALOG_SPECS)
def test_labels_are_unique(catalog):
    labels = [row["label"] for row in _rows(catalog)]
    assert len(set(labels)) == len(labels)


@pytest.mark.parametrize("catalog", CATALOG_SPECS)
def test_rows_are_sorted_by_id(catalog):
    """Keeps regeneration diffs meaningful."""
    _, id_field, _, _ = CATALOG_SPECS[catalog]
    ids = [int(row[id_field]) for row in _rows(catalog)]
    assert ids == sorted(ids)


@pytest.mark.parametrize("catalog", CATALOG_SPECS)
def test_the_all_sentinel_is_not_an_entry(catalog):
    """-1 means "Todos", a query modifier. 0 ("Sin Especificar") is real."""
    _, id_field, _, _ = CATALOG_SPECS[catalog]
    assert "-1" not in {row[id_field] for row in _rows(catalog)}


@pytest.mark.parametrize("catalog", CATALOG_SPECS)
def test_catalog_matches_the_live_form(catalog, form_html):
    """Live drift check: SNIIM added, removed or renamed something.

    Not a code failure — regenerate the catalog and review the diff.
    """
    _, id_field, builder, _ = CATALOG_SPECS[catalog]
    live = {row[id_field]: row["label"] for row in builder(form_html)}
    stored = {row[id_field]: row["label"] for row in _rows(catalog)}
    assert live == stored, f"{catalog}.csv is stale; run: uv run python -m scraper.catalogs"


# --- products ---


def test_name_and_quality_reconstruct_the_label(products):
    """The split is derived data; it must stay lossless against the source."""
    for row in products:
        assert f"{row['name']} - {row['quality']}" == row["label"]
        assert row["name"] and row["quality"]


def test_multi_dash_labels_split_on_the_last_separator():
    assert split_product_label("Nuez - Western - Primera") == ("Nuez - Western", "Primera")


# --- origins ---


def test_origin_kinds_are_known(origins):
    assert {row["kind"] for row in origins} == {"state", "national", "import", "unspecified"}


def test_origins_cover_the_32_federal_entities(origins):
    """31 states plus Mexico City, which SNIIM still calls "Distrito Federal"."""
    states = [row for row in origins if row["kind"] == "state"]
    assert len(states) == 32
    assert "Distrito Federal" in {row["label"] for row in states}


def test_non_geographic_origins_are_flagged(origins):
    """Importación, Nacional and Sin Especificar are not places.

    Anything that maps origins onto a map must skip these.
    """
    by_label = {row["label"]: row["kind"] for row in origins}
    assert by_label["Importación"] == "import"
    assert by_label["Nacional"] == "national"
    assert by_label["Sin Especificar"] == "unspecified"
