"""Live robots.txt guard: fails loud if SNIIM ever disallows our query path."""

import urllib.request

from protego import Protego

BASE_URL = "https://www.economia-sniim.gob.mx"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
QUERY_PATH = f"{BASE_URL}/nuevo/Consultas/"


def _fetch_robots() -> Protego:
    with urllib.request.urlopen(ROBOTS_URL, timeout=10) as resp:
        return Protego.parse(resp.read().decode("utf-8"))


def test_query_path_is_allowed():
    robots = _fetch_robots()
    assert robots.can_fetch(QUERY_PATH, "*")


def test_known_disallowed_paths_stay_disallowed():
    robots = _fetch_robots()
    assert not robots.can_fetch(f"{BASE_URL}/Analisis/reporte.asp", "*")
    assert not robots.can_fetch(f"{BASE_URL}/Sicia/consulta.asp", "*")
    assert not robots.can_fetch(f"{BASE_URL}/reporte.xls", "*")
