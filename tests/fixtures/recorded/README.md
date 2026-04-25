# tests/fixtures/recorded/

Inhalt dieses Ordners wird durch `scripts/recording_session.py` erzeugt
und ist die einzige zugelassene Datenquelle fuer den spaeter folgenden
Replay-Loader (siehe AP-02 #03).

**Nicht haendisch editieren.** Jede Aenderung muss aus einem dokumentierten
Live-Recording-Lauf kommen, sonst driftet der Mock von der Realitaet ab und
verliert seinen Zweck als Vertrag-Test.

## Naming

```
<sanitized_path>__<METHOD>__<query_hash>_<NN>.json
```

- `sanitized_path`: alle nicht-alphanumerischen Zeichen durch `_` ersetzt
  (z.B. `iserver_account_U25235077_orders`).
- `METHOD`: `GET`/`POST`/`DELETE`.
- `query_hash`: erste 8 Hex-Stellen eines SHA-256 ueber die sortierten
  Query-Parameter (`noquery` wenn leer).
- `NN`: monoton steigender Counter pro `(path, method, query_hash)` -
  Erstkontakt ist `_01`, Wiederholungen `_02`, `_03`, ... Notwendig fuer
  First-Call-Prime-Verhalten z.B. beim Snapshot-Endpoint.

## JSON-Schema (pro Datei)

```json
{
  "request":  { "method": "GET", "url": "/...", "query": {...}, "headers": {...}, "body_json": null, "body_text": null },
  "response": { "status_code": 200, "headers": {...}, "body_json": {...}, "body_text": null },
  "recorded_at": "<ISO-8601-UTC>",
  "normalized": true
}
```

`body_json` und `body_text` sind exklusiv: ein Body wird genau in einem der
beiden Felder abgelegt. JSON-Bodies durchlaufen
`broker_gateway.cp.normalize.normalize_response`, damit Timestamps und
Order-/Execution-/Session-IDs durch deterministische Platzhalter ersetzt
sind.

## Geheimnisse

`Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`,
`Proxy-Authorization`, `X-Auth-Token` werden vor dem Schreiben entfernt.
Tests pruefen das (`tests/test_cp_recorder.py::test_secrets_in_headers_are_never_persisted`).
Sollte versehentlich ein Geheimnis im Body landen (z.B. ein Token in
einem Error-Body), die betroffene Datei manuell loeschen und das Recording
neu durchlaufen lassen, nachdem die Quelle gefixed wurde.
