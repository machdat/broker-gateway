"""Strukturelle Routing-Invariante der v1-API (Karte ``cefcb57a``).

Hintergrund: ``GET /v1/orders/stream`` war seit seiner Einführung
unerreichbar, weil die Platzhalter-Route ``GET /v1/orders/{order_id}``
vor der Stream-Route registriert war. Starlette nimmt den ersten Treffer
in Registrierungsreihenfolge, also landete jeder Stream-Aufruf in
``get_order`` mit ``order_id="stream"`` und bekam 404.

Dieses Modul findet **Kollisions-Kandidaten**: statische Pfade, die von
einem Platzhalter-Pfad unter derselben Methode ebenfalls getroffen
werden könnten. Jeder solche Kandidat braucht einen Verhaltenstest, der
beweist, wer tatsächlich gewinnt - Kandidat allein ist noch kein Defekt,
sondern eine Stelle, an der die Registrierungsreihenfolge über Leben und
Tod entscheidet.

**Warum ausschließlich ``app.openapi()``:** Die erste Fassung dieses
Moduls lief über ``app.routes`` und ``route.endpoint``. Das ist an
Framework-Interna gekoppelt und zerbrach prompt: lokal (FastAPI 0.135)
liegen die Routen flach in ``app.routes``, ab FastAPI 0.139 - der
Version, die in CI **und in Produktion** läuft - steckt stattdessen ein
``_IncludedRouter`` darin, der seine Routen nicht herausgibt. Die
Introspektion fand dort *null* Routen und der Sweep lief still leer
durch: grün, ohne irgendetwas zu prüfen. ``app.openapi()`` ist
öffentliche API und über beide Versionen formgleich.

Der Preis: openapi kennt nur die **Deklaration**, nicht die
Erreichbarkeit - exakt die Lücke, durch die der Bug gerutscht ist. Diese
Lücke schließen die Verhaltenstests in
``tests/test_orders_stream.py::TestOrdersStreamRouting``, die den
Endpunkt wirklich aufrufen. Die Arbeitsteilung ist Absicht: hier die
Vollständigkeit über alle Pfade, dort der Beweis am Einzelfall.
"""
from __future__ import annotations

import re

from broker_gateway.main import create_app


# Bekannte Kollisionen, die durch Verhaltenstests abgesichert sind.
# Jeder Eintrag: (statischer Pfad, Platzhalter-Pfad, Methode).
_VERIFIZIERTE_KOLLISIONEN = {
    # Der Bug dieser Karte. Die beiden Pfade stammen aus ZWEI Routern mit
    # identischem Prefix, aufgelöst allein durch die Reihenfolge der
    # include_router-Aufrufe in api/v1/__init__.py. Genau das ging schief.
    # Beweis: tests/test_orders_stream.py::TestOrdersStreamRouting.
    ("/v1/orders/stream", "/v1/orders/{order_id}", "GET"),
    # Dieselbe Konstellation, aber innerhalb EINER Datei: in
    # api/v1/instruments.py steht "/search" vor "/{conid}", damit gewinnt
    # der statische Pfad. Unauffällig, aber genauso reihenfolge-abhängig -
    # wer die beiden Dekoratoren vertauscht, tötet die Suche.
    # Beweis: tests/test_instruments.py::test_search_endpoint_with_admin_token
    # (erwartet 200 mit Trefferliste; verdeckt käme conid="search" an).
    ("/v1/instruments/search", "/v1/instruments/{conid}", "GET"),
}


def _declared_paths() -> dict[str, list[str]]:
    """Deklarierte Pfade der zusammengesetzten App: ``{pfad: [methoden]}``.

    ``create_app()`` ohne Argumente ist der Modul-Level-Pfad aus
    ``main.py`` und braucht kein Env - Verbindungen entstehen erst im
    Lifespan, der hier nicht startet.
    """
    spec = create_app().openapi()
    return {
        path: sorted(method.upper() for method in operations)
        for path, operations in spec["paths"].items()
    }


def _placeholder_to_regex(path: str) -> re.Pattern[str]:
    """Baut aus ``/v1/orders/{order_id}`` ein Muster, das ein Segment frisst.

    Bewusst konservativ: ein Platzhalter matcht alles außer ``/``, genau
    wie Starlettes Default-Konverter. Pfade mit typisierten Konvertern
    (``{id:int}``) gibt es in dieser API nicht; kämen sie, wäre dieses
    Muster zu weit und meldete einen Kandidaten zu viel - ein
    Fehlalarm, der auffällt, kein übersehener Defekt.
    """
    pattern = re.sub(r"\{[^}]+\}", "[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
    return re.compile(rf"^{pattern}$")


def _kollisionen() -> set[tuple[str, str, str]]:
    paths = _declared_paths()
    statisch = [p for p in paths if "{" not in p]
    platzhalter = [p for p in paths if "{" in p]

    treffer: set[tuple[str, str, str]] = set()
    for ph in platzhalter:
        muster = _placeholder_to_regex(ph)
        for s in statisch:
            if not muster.match(s):
                continue
            for methode in set(paths[s]) & set(paths[ph]):
                treffer.add((s, ph, methode))
    return treffer


def test_openapi_liefert_die_erwarteten_pfade() -> None:
    """Nicht-Leerlauf-Wächter für die Tests unten.

    Ohne diesen Test wäre der Sweep still wertlos, sobald die
    Pfad-Ermittlung bricht - genau das ist der ersten Fassung dieses
    Moduls passiert: sie fand null Routen und meldete trotzdem grün.
    Ein Struktur-Test, der nichts findet, muss laut scheitern.
    """
    paths = _declared_paths()

    assert len(paths) > 20, (
        f"Nur {len(paths)} Pfade aus app.openapi() - die Ermittlung ist "
        "kaputt, nicht die API."
    )
    assert "/v1/orders/stream" in paths, "Stream-Pfad ist nicht deklariert"
    assert "/v1/orders/{order_id}" in paths, "Platzhalter-Pfad fehlt"
    assert "GET" in paths["/v1/orders/stream"]
    assert "GET" in paths["/v1/orders/{order_id}"]


def test_jede_pfad_kollision_ist_bekannt_und_verhaltensgetestet() -> None:
    """Neue Kollisionen müssen bewusst abgenommen werden.

    Taucht ein neuer statischer Pfad hinter einem Platzhalter auf, ist
    das nicht automatisch ein Defekt - aber eine Stelle, an der allein
    die Registrierungsreihenfolge entscheidet, ob der Endpunkt lebt.
    Genau diese Stelle war jahrelang unbemerkt kaputt. Deshalb: neuer
    Kandidat -> Verhaltenstest schreiben, dann hier eintragen.
    """
    gefunden = _kollisionen()

    neu = gefunden - _VERIFIZIERTE_KOLLISIONEN
    assert not neu, (
        "Neue Pfad-Kollision(en) gefunden. Ein statischer Pfad wird von "
        "einem Platzhalter-Pfad unter derselben Methode ebenfalls "
        "getroffen - welcher gewinnt, entscheidet die Reihenfolge der "
        "include_router-Aufrufe in api/v1/__init__.py.\n"
        "Bitte einen Verhaltenstest ergänzen (Vorbild: "
        "tests/test_orders_stream.py::TestOrdersStreamRouting) und den "
        "Fall dann in _VERIFIZIERTE_KOLLISIONEN eintragen:\n  "
        + "\n  ".join(
            f"{methode} {s} kollidiert mit {ph}" for s, ph, methode in sorted(neu)
        )
    )

    verschwunden = _VERIFIZIERTE_KOLLISIONEN - gefunden
    assert not verschwunden, (
        "Eine als verifiziert eingetragene Kollision existiert nicht mehr. "
        "Wenn der Pfad absichtlich weg ist, den Eintrag aus "
        "_VERIFIZIERTE_KOLLISIONEN entfernen. Wenn nicht, ist die "
        "Kollisions-Erkennung kaputt und dieser Test wertlos:\n  "
        + "\n  ".join(
            f"{methode} {s} vs {ph}" for s, ph, methode in sorted(verschwunden)
        )
    )


def test_post_whatif_kollidiert_nicht_mit_order_id() -> None:
    """Gegenprobe: die Methoden-Prüfung darf nicht zu grob sein.

    ``POST /v1/orders/whatif`` liegt pfadgleich hinter
    ``GET /v1/orders/{order_id}``, kollidiert aber nicht - es gibt kein
    ``POST /v1/orders/{order_id}``. Meldete die Kollisions-Erkennung das
    trotzdem, wäre sie zu grob und ihre Ergebnisse wertlos.
    """
    kollisionen = _kollisionen()
    whatif = {
        (s, ph, m) for s, ph, m in kollisionen if s == "/v1/orders/whatif"
    }

    assert not whatif, (
        f"POST /v1/orders/whatif fälschlich als Kollision gemeldet: {whatif}"
    )
