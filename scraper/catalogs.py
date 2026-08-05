"""Extract the SNIIM query form's dropdown catalogs into versioned CSV files.

The form's <select> elements are the only public source for the internal ids
the results endpoint expects (ProductoId, OrigenId, DestinoId). Those ids are
not stable knowledge — they must be re-extracted and diffed, never guessed —
so this module is a one-shot regenerator, not something the spider imports at
runtime.

Usage:
    uv run python -m scraper.catalogs           # regenerate every catalog
    uv run python -m scraper.catalogs products  # just one

See docs/consulta-sniim.md for the endpoint these ids feed.
"""

from __future__ import annotations

import csv
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from parsel import Selector

FORM_URL = (
    "https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales"
    "/PreciosDeMercado/Agricolas/ConsultaFrutasYHortalizas.aspx?SubOpcion=4|0"
)

CATALOGS_DIR = Path(__file__).resolve().parent.parent / "data" / "catalogs"

# The "Todos" sentinel is a query modifier, not a catalog member.
ALL_SENTINEL = "-1"


@dataclass(frozen=True)
class Option:
    """One <option> of a form dropdown: internal id plus its visible label."""

    id: str
    label: str


def fetch_form_html(url: str = FORM_URL) -> str:
    """Fetch the query form. Cookieless, like every other request we make."""
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_select(html: str, select_name: str) -> list[Option]:
    """Read one dropdown, dropping the "Todos" sentinel.

    Raises on anything unexpected rather than returning a short catalog: a
    silently truncated catalog would silently truncate the whole ingest.
    """
    selector = Selector(text=html)
    options = selector.css(f'select[name="{select_name}"] option')
    if not options:
        raise ValueError(f"dropdown {select_name!r} not found in form page")

    entries = []
    for option in options:
        value = option.attrib.get("value", "").strip()
        label = " ".join((option.css("::text").get() or "").split())
        if value == ALL_SENTINEL:
            continue
        if not value.isdigit():
            raise ValueError(f"{select_name}: non-numeric id {value!r} ({label!r})")
        if not label:
            raise ValueError(f"{select_name}: empty label for id {value!r}")
        entries.append(Option(id=value, label=label))

    if not entries:
        raise ValueError(f"dropdown {select_name!r} has no entries besides 'Todos'")

    ids = [entry.id for entry in entries]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{select_name}: duplicate ids in source dropdown")
    return entries


def split_product_label(label: str) -> tuple[str, str]:
    """Split "Aguacate Hass - Primera" into name and quality.

    Splits on the last separator only: "Nuez - Western - Primera" is the
    Western variety of walnut, first quality.
    """
    name, separator, quality = label.rpartition(" - ")
    if not separator:
        raise ValueError(f"product label without quality suffix: {label!r}")
    return name.strip(), quality.strip()


def build_products(html: str) -> list[dict[str, str]]:
    rows = []
    for option in parse_select(html, "ddlProducto"):
        name, quality = split_product_label(option.label)
        rows.append(
            {
                "product_id": option.id,
                "label": option.label,
                "name": name,
                "quality": quality,
            }
        )
    return rows


# Origins are federal entities except for these three. They are not places,
# so nothing downstream may treat them as geography. Keyed by label rather
# than by id: a renamed entry must fail loudly instead of being classified
# as a state, and the ids are SNIIM's to change.
NON_STATE_ORIGINS = {
    "Sin Especificar": "unspecified",
    "Nacional": "national",
    "Importación": "import",
}

# The 31 states plus Mexico City, which SNIIM still lists as "Distrito Federal".
EXPECTED_STATE_COUNT = 32


def build_origins(html: str) -> list[dict[str, str]]:
    """Origin = where the produce is *marketed from*, never where it was grown."""
    rows = [
        {
            "origin_id": option.id,
            "label": option.label,
            "kind": NON_STATE_ORIGINS.get(option.label, "state"),
        }
        for option in parse_select(html, "ddlOrigen")
    ]

    found = {row["kind"] for row in rows}
    missing = set(NON_STATE_ORIGINS.values()) - found
    if missing:
        raise ValueError(f"ddlOrigen: non-state origins renamed or gone: {missing}")

    states = sum(1 for row in rows if row["kind"] == "state")
    if states != EXPECTED_STATE_COUNT:
        raise ValueError(f"ddlOrigen: expected {EXPECTED_STATE_COUNT} states, got {states}")
    return rows


# Destination labels abbreviate one state differently than the origin
# dropdown does. Spelled out so both catalogs name the same entity the same
# way and can be joined on `state`.
DESTINATION_STATE_ALIASES = {
    "DF": "Distrito Federal",
}


def split_destination_label(label: str) -> tuple[str, str]:
    """Split "Puebla: Central de Abasto de Puebla" into state and market.

    Only the state is reliably separable. The city is sometimes the market
    name ("Chiapas: Tapachula"), sometimes a suffix after a comma, and
    sometimes absent — pulling it out would be guesswork, so the market name
    is kept whole.
    """
    state, separator, market = label.partition(":")
    if not separator:
        raise ValueError(f"destination label without state prefix: {label!r}")
    state = state.strip()
    return DESTINATION_STATE_ALIASES.get(state, state), market.strip()


def build_destinations(html: str) -> list[dict[str, str]]:
    """Destination = the wholesale market where the price was surveyed."""
    rows = []
    for option in parse_select(html, "ddlDestino"):
        state, market = split_destination_label(option.label)
        if not market:
            raise ValueError(f"destination {option.id} has no market name: {option.label!r}")
        rows.append(
            {
                "destination_id": option.id,
                "label": option.label,
                "state": state,
                "market": market,
            }
        )

    # The two dropdowns must agree on how the country is divided up.
    states = {row["label"] for row in build_origins(html) if row["kind"] == "state"}
    unknown = {row["state"] for row in rows} - states
    if unknown:
        raise ValueError(f"ddlDestino: states absent from ddlOrigen: {sorted(unknown)}")
    return rows


CATALOGS = {
    "products": ("products.csv", build_products),
    "origins": ("origins.csv", build_origins),
    "destinations": ("destinations.csv", build_destinations),
}


def write_catalog(rows: list[dict[str, str]], path: Path) -> None:
    """Write sorted by numeric id so regeneration diffs show real changes only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    ordered = sorted(rows, key=lambda row: int(row[fieldnames[0]]))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ordered)


def main(argv: list[str]) -> int:
    wanted = argv or list(CATALOGS)
    unknown = [name for name in wanted if name not in CATALOGS]
    if unknown:
        print(f"unknown catalog(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(CATALOGS)}", file=sys.stderr)
        return 2

    html = fetch_form_html()
    for name in wanted:
        filename, builder = CATALOGS[name]
        rows = builder(html)
        destination = CATALOGS_DIR / filename
        write_catalog(rows, destination)
        print(f"{name}: {len(rows)} entries -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
