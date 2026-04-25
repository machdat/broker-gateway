# tests/fixtures/recorded/seed/

Handgeschriebene Default-Recordings, die das frueher hartcodierte
Mock-Verhalten 1:1 reproduzieren. Diese Dateien sind die **Brueckenloesung**,
bis Karte AP-02 #04 / #05 echte Live-Recordings aus dem CP-Gateway in
``../live/`` ablegt.

## Konvention

| Verzeichnis | Quelle | Vorrang | Lebensdauer |
|-------------|--------|---------|-------------|
| `live/`     | echte CP-Gateway-Antworten via `scripts/recording_session.py` | hoch | dauerhaft |
| `seed/`     | handgeschrieben in diesem Verzeichnis | nur als Fallback | wird in spaeterer Karte archiviert/geloescht, sobald alle Endpunkte eine `live/`-Variante haben |

`tests/cp_mock/loader.py::load_recording` sucht erst in ``live/``,
dann in ``seed/``. Sobald ein Endpunkt eine `live/`-Datei hat,
ueberschreibt sie die `seed/`-Variante automatisch.

## Welche Endpunkte sind als Seed enthalten

Aktuell deckt seed/ ausschliesslich die **statischen** Endpunkte ab,
die der frueher hartcodierte Mock 1:1 lieferte:

- `GET /iserver/auth/status` (happy path - der `auth_lost`-Pfad
  mutiert das Recording im Code).
- `POST /tickle` (happy path - dito fuer auth_lost).
- `POST /reauthenticate`.
- `GET /iserver/secdef/search` mit `symbol={AAPL,MSFT,SAP}`.
- `GET /iserver/secdef/info` mit `conid={265598,272093,104747}`.
- `GET /iserver/account/U25235077/{portfolio,positions,ledger}`.

**Stateful Endpunkte** (snapshot mit First-Call-Prime und
Subscription-State, orders mit Lifecycle PendingSubmit -> Submitted ->
Filled, trades mit `days`-Schleife, Reply-Confirmation-Loop,
Unsubscribe) generieren ihre Bodies weiterhin **im Code** in
``tests/cp_mock/replay.py``. Sie bekommen eigene Recordings erst,
wenn die Live-Recording-Sessions in AP-02 Karte 04/05 die
tatsaechlichen IBKR-Antworten festhalten - dann sind ID-Counter und
Lifecycle-Stages konkret abbildbar.

## Was NICHT in seed-Dateien gehoert

- Konkrete IBKR-Live-Tokens, Cookies, Session-IDs.
- Echte Order-IDs aus dem Live-Account.
- Echte Timestamps aus dem Live-Recording-Lauf - die landen automatisch
  in ``live/`` und durchlaufen dort den Normalizer.

`seed/`-Dateien tragen `"normalized": false` und konkrete Werte (z.B.
`session: "mock-session-id"`), weil sie KEINE Live-Bodies sind. Beim
Replace durch ein `live/`-File springen die Werte automatisch auf die
Recorder-Output-Form mit Platzhaltern - die Tests muessen damit klar
kommen oder die Live-Recording-Karte fuegt entsprechende Anpassungen ein.
