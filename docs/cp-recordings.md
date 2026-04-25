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
2. ENV setzen: `export BG_CP_RECORD_DIR=tests/fixtures/recorded` -
   damit aktiviert sich der Recorder im naechsten `CPGatewayClient`-
   Konstruktor.
3. Recording-Session-Skript aufrufen:

   ```bash
   python scripts/recording_session.py \
       --record-dir tests/fixtures/recorded \
       --base-url http://localhost:5000/v1/api \
       --account-id U25235077 \
       --scenario happy
   ```

   Aktuell druckt das Skript nur die Konfiguration. Die eigentliche
   Endpunkt-Sequenz wird in AP-02 #04 (happy-path) und #05 (error-path)
   eingebaut.
4. `git status tests/fixtures/recorded/` zeigt die neuen Dateien. Vor dem
   Commit prufen, dass kein Geheimnis durchgerutscht ist (siehe unten).

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
