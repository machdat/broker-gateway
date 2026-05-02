# IBKR Order-Warnings (whatif + Place-Order Reply-Loop)

Stand der Recherche, Mai 2026 (AP-09). Konsolidiert die spaerlich
verstreute Doku zu Order-Warnings, die IBKRs CP-Gateway als Pre-Trade-
Risk-Check liefert. **Achtung:** IBKR dokumentiert die Warning-Codes
nicht zentral; diese Datei sammelt das, was empirisch beobachtet oder
in IBKR-Knowledgebase-Artikeln auffindbar ist.

## 1. Quellen

| Quelle | Pfad |
|--------|------|
| **IBKR-CP-API-OpenAPI** (autoritativ fuer den Roundtrip-Mechanismus, NICHT fuer einzelne Codes) | [`docs/research/ibkr-cpapi-doc.json`](ibkr-cpapi-doc.json) — Beschreibung von `POST /iserver/account/{accountId}/orders` und dem `reply/{id}`-Loop. |
| **broker-gateway-Adapter** | [`src/broker_gateway/cp/orders.py`](../../src/broker_gateway/cp/orders.py) — implementiert den Reply-Loop (Module-Docstring). |
| **Live-Recordings** | `tests/fixtures/recorded/live/**/*.json*` — Stand 2026-05-02: 1× Warning-Code "1" beobachtet. |
| **Auto-Memory-Notiz** (lokal) | "IBKR Feld 6509 Availability-Code" + bisherige PSM-Bootstrap-Notes — nennen Warning 4 + 21 als haeufigste. |
| **K4-WS-Findings** | [`docs/research/ibkr-cpapi-websockets-findings.md`](ibkr-cpapi-websockets-findings.md) Sektion 2.4 — beschreibt IBKR-Confirmation-Pattern bei `sor`-Frame-Test. |

> **Limitation:** Die IBKR-Knowledgebase ist online verfuegbar
> (https://ibkrcampus.com etc.), aber nicht im Repo gespiegelt. Wer
> einen unbekannten Warning-Code im Live-Lauf sieht, sollte die
> Original-Message gegen die Knowledgebase-Suche pruefen und dann
> den Eintrag hier ergaenzen.

## 2. Roundtrip-Mechanismus

CP-Gateway-Endpoint: `POST /iserver/account/{accountId}/orders` (oder
`POST /iserver/account/{accountId}/orders/whatif` fuer reine
Vorschau).

Antwort-Schema:
- **Warnings**: `[{ "id": "<reply-uuid>", "message": ["..."] }]` — der
  Adapter muss per `POST /iserver/reply/{id}` mit
  `{"confirmed": true}` quittieren, sonst wird die Order nicht
  submittiert.
- **Order**: `[{ "order_id": ..., "order_status": "...", ... }]` —
  endgueltige Antwort.

`broker-gateway` (siehe `cp/orders.py`) loopt bis zu `MAX_REPLY_ROUNDS`
durch, sammelt unbestätigte Warnings und liefert sie als `warnings`-
Liste im Public-API-Order-Objekt aus.

## 3. Bekannte Warning-Codes

| Code | Message-Praefix (IBKR) | Severity | Beobachtung | Empfohlene Service-Reaktion |
|------|------------------------|----------|-------------|------------------------------|
| **1** | "You are trying to submit an order without having market data for this instrument" | warn | Live-Recording 2026-05-02 (1× in 2 Files); identisch zur "Blind-Trading"-Warning (siehe Code 21). | **Auto-confirmen** im whatif-Pfad fuer Paper-Konten; im Live-Konto **als Hinweis durchreichen** und Consumer entscheidet. |
| **4** | "Percentage price check cannot be performed" | warn | Memory-Notiz (PSM-Bootstrap); tritt auf, wenn Realtime-Marktdaten fuer das Instrument nicht freigeschaltet sind und IBKR den 3 %-Preis-Check nicht ausführen kann. | **Auto-confirmen**; ist eine reine Diagnose-Warnung, kein Risiko. |
| **21** | "blind trading without market data" / "You are trying to submit an order without having market data" | warn | Memory-Notiz (PSM-Bootstrap); semantisch identisch zur Warning 1, beobachtet bei DPB-Daten ausserhalb RTH. | wie Code 1. |
| **(Mandatory-Cap-Price)** | "There is no last, bid, or ask data available for this contract..." | warn (Mandatory-Confirm) | K4-WS-Findings Sektion 2.4 — IBKR fragt **drei** Confirmations bei einer Test-Order (price-cap 3 %, no-market-data, mandatory-cap-price). | **Auto-confirmen** im PIC-Pfad (place + immediate cancel), **manuell pruefen** bei echten Trades. |

## 4. Severity-Klassen und Service-Reaktion

`broker-gateway` kennt heute keine harte Per-Code-Logik — alle
Warnings werden gleich behandelt: Auto-Confirm im Reply-Loop bis
`MAX_REPLY_ROUNDS`, dann harte Order-Submit-Antwort plus Warning-
Liste im Body.

Empfohlene Severity-Klassifikation fuer Folge-Hardening:

| Klasse | Codes | Reaktion |
|--------|-------|----------|
| **info** | (keine derzeit) | Body-Field, kein Service-Eingriff. |
| **warn** | 1, 4, 21, "Mandatory-Cap-Price" | Auto-Confirm im whatif-/Place-Loop; Warning im Public-API-Body als `warnings: [{ code, message }]`. |
| **error** | (noch keine bekannten — kommen typisch als 4xx-Status, nicht als Warning) | Order-Reject mit Error-Envelope. |

> Ein Code, der *zwingend* zu einer harten Ablehnung fuehren muss
> (z.B. "insufficient margin"), erscheint in der Praxis als 4xx-
> Statuscode und nicht als Warning im 200-Body. Wer einen solchen
> Code im Warning-Stream beobachtet, eskaliert auf eine Bugfix-Karte
> fuer `cp/orders.py`.

## 5. Empirie aus Live-Recordings

Auswertung von `tests/fixtures/recorded/live/**/*.json*` (Stand
2026-05-02, AP-09 Recherche-Skript):

| Code | Vorkommen | Message |
|------|-----------|---------|
| `1` | 2 | "You are trying to submit an order without having market data for this instrument" |

Der Bestand ist klein, weil Order-Roundtrips selten in Recordings
gespiegelt werden (Trades sind destruktiv, Recordings primaer auf
Read-Only-Endpunkten gemacht). Nach AP-08 L2/L3 (place + immediate
cancel) wachsen die Recordings — der Bestand sollte dann auf 5-15
Codes wachsen.

## 6. Restunsicherheiten

- **Vollstaendige Tabelle 1-99** ist nicht publik dokumentiert. IBKR-
  Support kann auf Anfrage Listen schicken; wir haben das bisher nicht
  angefragt.
- **Warning-Code-Stabilitaet** ueber CP-Gateway-Versionen: nicht
  garantiert. Bei Version-Bump des CP-Gateway-Tarballs koennten neue
  Codes hinzukommen oder bestehende ihre Bedeutung aendern.
- **Reply-Loop-Tiefe**: bei manchen Orders kommen mehrere Warnings
  hintereinander (siehe K4-Mandatory-Cap-Price-Beispiel mit drei
  Stufen). Der Adapter `MAX_REPLY_ROUNDS` setzt das Limit — wir
  kennen nicht die maximale IBKR-seitige Tiefe.

## 7. Wartungs-Pflicht

Wer einen neuen Warning-Code in einem Recording oder Live-Smoke
beobachtet, traegt ihn in Sektion 3 mit Code, Message-Praefix,
Severity-Einschaetzung und empfohlener Reaktion ein. Diese Datei lebt
mit dem Service.
