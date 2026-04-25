# docs/research/

Externe Referenz-Snapshots, die wir lokal vorhalten, damit der Service
nicht von einer Live-Doku-Verfuegbarkeit abhaengt.

## ibkr-cpapi-doc.json

Swagger 2.0 Snapshot der IBKR Client Portal Web API,
gefetcht von <https://www.interactivebrokers.com/api/doc.json> am
2026-04-25 im Rahmen von AP-02 #04.

`basePath: /v1/api`, `host: localhost:5000` — die Doku reflektiert das
self-hosted CP-Gateway. Verwendet als Quelle der Wahrheit fuer den
Diff-Report in `docs/runbooks/recording-session-happy-path.md`.

Re-fetch:

```bash
curl -A "Mozilla/5.0" -o docs/research/ibkr-cpapi-doc.json \
    https://www.interactivebrokers.com/api/doc.json
```
