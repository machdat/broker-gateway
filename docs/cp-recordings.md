# CP-Gateway-Recordings

Live-Antworten des internen IBKR Client Portal Gateways werden ueber den
:class:`broker_gateway.cp.recorder.CPRecorder` als deterministische
JSON-Fixtures abgelegt. Diese Fixtures sind ab AP-02 #03 die **Single
Source of Truth** fuer die pytest-Mock-Fixture - handgeschriebene
Beispiele werden schrittweise abgeloest.

## Wofuer ist das gut

- **Mock-vs-Realitaet:** Mocks zerfallen, sobald IBKR ein Schema-Detail
  aendert. Aufgezeichnete Live-Antworten sind die einzige Quelle, die
  diese Drift sichtbar macht.
- **Vertrag-Tests:** Die gleichen Fixtures speisen den Mock UND einen
  Drift-Detection-Smoke-Test (AP-02 #06). Wenn die Live-Antwort vom
  Recording abweicht, schlaegt der Smoke-Test an.
- **Reproduzierbarkeit:** Order-Lifecycle-Tests (Reply-Confirmation,
  PendingSubmit -> Submitted -> Filled) brauchen exakt die Antwort-
  Sequenz, die das echte CP-Gateway gibt - nicht das, was wir uns
  beim Schreiben des Mocks vorgestellt haben.

## Wie ein Recording entsteht

1. CP-Gateway live im Compose-Stack hochfahren (siehe
   `docs/runbooks/cpgateway-login.md` fuer den Browser-2FA-Login).
2. Recording-Session-Skript aufrufen (Subkommando `happy-path` ist seit
   v1.3.0 voll implementiert):

   ```bash
   python scripts/recording_session.py happy-path \
       --record-dir tests/fixtures/recorded/live \
       --base-url http://localhost:5000/v1/api \
       --account-id U25235077 \
       --symbols AAPL MSFT SAP
   ```

   Das Skript prueft zuerst `/iserver/auth/status`, dann ruft es alle
   v1-Endpunkte sequenziell ab. Order-Schritt ist standardmaessig die
   Preview-Variante (`/orders/whatif`); mit `--with-place-cancel` zusaetzlich
   eine echte Limit-Order weit ausserhalb des Marktes, die sofort wieder
   gecancelt wird (siehe `docs/runbooks/recording-session-happy-path.md`).
3. `git status tests/fixtures/recorded/live/` zeigt die neuen Dateien. Vor
   dem Commit pruefen, dass kein Geheimnis durchgerutscht ist (siehe unten).

## Was wird gespeichert

Pro HTTP-Roundtrip eine Datei:

```
<sanitized_path>__<METHOD>__<query_hash>_<NN>.json
```

Inhalt:

```jsonc
{
  "request":  {
    "method": "GET",
    "url": "/iserver/marketdata/snapshot",
    "query": {"conids": "265598", "fields": "31,84,86,6509"},
    "headers": {"User-Agent": "..."},   // ohne Authorization/Cookie/X-API-Key
    "body_json": null,
    "body_text": null
  },
  "response": {
    "status_code": 200,
    "headers": {"content-type": "application/json"},
    "body_json": [{"conid": 265598, "_updated": "<TIMESTAMP>", "31": "150.50"}],
    "body_text": null
  },
  "recorded_at": "<ISO-8601-UTC>",
  "normalized": true
}
```

`body_json` und `body_text` sind exklusiv: JSON-Antworten landen in
`body_json` und werden durch `normalize_response` von Timestamps,
Order-/Execution-/Session-IDs befreit. Nicht-JSON-Antworten (z.B.
HTML-Error-Pages des CP-Gateways) landen 1:1 in `body_text` ohne
Normalisierung.

## Was NICHT gespeichert wird

| Bereich | Filter | Test |
|---------|--------|------|
| Authorization-Header | komplett entfernt | `test_secrets_in_headers_are_never_persisted` |
| Cookie / Set-Cookie | komplett entfernt | dito |
| X-API-Key, X-Auth-Token, Proxy-Authorization | komplett entfernt | dito |
| Timestamps in Bodies (`_at`, `_ts`, ISO-8601-Strings) | -> `<TIMESTAMP>` | `test_normalize_response_replaces_timestamps_and_ids` |
| `order_id`, `execution_id`, `reply_id` | -> `<KIND_NNN>` mit Counter | dito |
| `session`, `sessionId` | -> `<SESSION_ID>` | dito |
| Preise / MarketData (default) | bleiben erhalten | absichtlich - siehe unten |

Preise und Marktdaten bleiben default unangetastet, weil sie die
Realitaet darstellen, gegen die Tests laufen sollen. Wer fuer einen
bestimmten Endpunkt anders entscheidet, ruft den Recorder mit
`normalize_prices=True` auf bzw. setzt das Flag im Recording-Skript.

## Diff-Bewertung

Wenn `git diff tests/fixtures/recorded/...` Aenderungen zeigt, gibt es
drei Klassen:

1. **Erwartete Aenderung im Schema:** IBKR hat ein Feld umbenannt /
   ergaenzt. Die Aenderung ins Recording uebernehmen UND in den
   Service-Code einarbeiten (Karte aufmachen).
2. **Neue Endpunkte:** Recording bringt eine zusaetzliche Datei mit.
   Pruefen, dass die Datei zur Endpunkt-Liste in
   `scripts/recording_session.py` passt - sonst war der Lauf
   versehentlich breiter.
3. **Drift in Werten, die normalisiert sein sollten:** Counter-Werte
   schwanken (`<ORDER_ID_001>` vs `<ORDER_ID_002>`), wenn die
   Aufruf-Reihenfolge sich aendert. Das ist OK, solange die internen
   Referenzen konsistent bleiben (gleiche Order-ID -> gleicher
   Platzhalter innerhalb derselben Datei).

Vor dem Commit immer noch eine Sichtkontrolle: `grep -ri "Bearer "
tests/fixtures/recorded/` muss leer sein. Sollte ein Geheimnis
durchgerutscht sein, betroffene Datei loeschen und Recorder-Filter
erweitern (`_REDACTED_HEADERS_LOWER` in
`src/broker_gateway/cp/recorder.py`).

## Replay-Modus (AP-02 #03)

Die pytest-Mock-Fixture ``cp_gateway_mock`` (definiert in
`tests/conftest.py`) nutzt seit Karte AP-02 #03 die Klasse
``ReplayCPGatewayMock`` aus ``tests/cp_mock/replay.py``. Statische
Endpunkt-Antworten werden ueber den Loader
``tests/cp_mock/loader.py::load_recording`` aus dem Recording-Ordner
geladen, statt im Code hartcodiert zu sein.

### Suchreihenfolge

```
tests/fixtures/recorded/live/<sanitized_path>__<METHOD>__<qhash>_<NN>.json
                              ^ Vorrang
tests/fixtures/recorded/seed/<sanitized_path>__<METHOD>__<qhash>_<NN>.json
                              ^ Fallback (handgeschriebener Default)
```

Sobald ein Endpunkt eine `live/`-Datei hat, ueberschreibt sie das
seed-Pendant automatisch - es gibt keinen manuellen Override.

### seed vs. live

- **seed/** = handgeschrieben in diesem Repo. Reproduziert das
  Verhalten, das die hartcodierten Mocks vor dem Refactoring lieferten.
  Konkrete Werte (z.B. `session: "mock-session-id"`), `normalized: false`.
  Wird mit jedem live-Recording schrittweise abgeloest.
- **live/** = aus echtem CP-Gateway via ``scripts/recording_session.py``.
  Wert-felder duerchlaufen ``normalize_response``, daher Platzhalter
  fuer Timestamps/IDs. Authoritative Quelle.

### Stateful-Ausnahme

Endpunkte mit Laufzeit-State werden **nicht** aus Recordings geladen,
sondern bleiben Code-generiert in ``ReplayCPGatewayMock``:

| Endpunkt | Warum Code statt Recording |
|----------|---------------------------|
| `/iserver/marketdata/snapshot` | First-Call-Prime und variable conids/fields-Combos pro Test |
| `/iserver/marketdata/{cid}/unsubscribe` | mutiert ``subscriptions``-Set |
| `POST /iserver/account/{acct}/orders` | order_id aus Counter ``_next_order_id`` |
| `POST /iserver/reply/{id}` | Reply-Confirmation-Loop ueber ``_pending_replies`` |
| `GET /iserver/account/orders/{id}` | Lifecycle PendingSubmit -> Submitted -> Filled |
| `DELETE /iserver/account/{acct}/order/{id}` | mutiert ``orders``-Dict |
| `GET /iserver/account/trades` | dynamische ``days``-Schleife |

In Karte AP-02 #04 / #05 werden diese Endpunkte durch Live-Recordings
mit echten IBKR-Antworten ergaenzt; danach kann der Code Lifecycle und
Counter zur Laufzeit aus Templates substituieren.

### Loader-API in Tests

```python
from tests.cp_mock import load_recording, RecordingNotFoundError

response = load_recording(
    "/iserver/auth/status",
    method="GET",
    query=None,
    call_index=1,
)
assert response["status_code"] == 200
assert response["body_json"]["authenticated"] is True
```

`base_dir` kann ueberschrieben werden (in Tests fuer tmp_path-basierte
Szenarien). Bei fehlendem Recording wird ``RecordingNotFoundError``
geworfen - Tests duerfen das fangen, der Mock-Code soll es nicht.

## Drift Detection (AP-02 #06, ab v1.6.0)

Der Replay-Mock sieht nur die heutigen Fixtures - er merkt nicht, wenn
IBKR ein Feld ergaenzt oder umbenennt. Dafuer gibt es das Skript
`scripts/check_mock_drift.py`, das Live-Antworten gegen die eingecheckten
Fixtures vergleicht und einen Markdown-Bericht unter
`reports/drift/<YYYY-MM-DD>.md` ablegt.

Klassifikation pro Endpunkt (Single Source of Truth: `tests/cp_mock/diff.py`):

| Klasse | Bedeutung | Reaktion |
|--------|-----------|----------|
| no drift | identisch (oder nur ignorierte Felder geaendert) | nichts zu tun |
| minor drift (additive) | nur neue Felder hinzu, Schema bleibt rueckwaerts-kompatibel | Karte 'Schema in <Endpunkt> erweitert' anlegen |
| value drift | Skalar-Wert geaendert, Schema unveraendert | Sichtkontrolle, Karte falls semantisch relevant |
| **breaking drift** | Felder entfernt, Typaenderung oder Wert-zu-null | sofort Karte mit `blocked=true` |

Exit-Code: `0` wenn kein breaking drift, `1` wenn mindestens ein breaking
drift gefunden wurde, `2` bei I/O-Fehlern, `3` wenn `/iserver/auth/status`
nicht authentifiziert ist (Login fehlt - vorher
`docs/runbooks/cpgateway-login.md`).

Order-Endpunkte (alles mit `/orders`/`/order/`), `/logout` und
`/reauthenticate` werden uebersprungen - sie haben Side Effects oder sind
dokumentarische Beweis-Recordings.

Voller Workflow: `docs/runbooks/mock-drift-check.md`.

### Refresh einer einzelnen Fixture

Wenn der Drift-Bericht eine erwartete Aenderung zeigt (z.B. additive
Feld-Erweiterung von IBKR), wird die Fixture **nicht automatisch**
ueberschrieben. Dafuer gibt es das `refresh`-Subkommando:

```bash
python scripts/recording_session.py refresh \
    tests/fixtures/recorded/live/iserver_accounts__GET__noquery_01.json
```

Das Skript zeigt erst einen Diff (gleiche Logik wie `check_mock_drift.py`),
fragt nach Bestaetigung und ersetzt die Datei nur danach. Mit `--yes` wird
die Bestaetigung uebersprungen (CI-Modus).
