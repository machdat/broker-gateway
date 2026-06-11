# Runbook: Recording-Session Error-Path (AP-02 #05)

Provoziert gezielt CP-Gateway-Fehlerantworten und legt sie als
Fixtures unter `tests/fixtures/recorded/live/errors/` ab. Erweitert
das Recording-Schema um die Realitaet der IBKR-Fehlerklassen.

> **Konto-Hinweis:** Die bestehenden Error-Recordings wurden gegen
> `U25235077` aufgenommen (historischer Live-Account, bis Cutover
> 2026-06-08 — siehe [`account-cutover.md`](account-cutover.md)).
> Die ``--account-id``-Beispiele unten geben diese Laeufe wieder.

## Voraussetzungen

Identisch zum [Happy-Path-Runbook](recording-session-happy-path.md):

- `cpgateway`-Container laeuft, Browser-2FA-Login frisch.
- SSH-Reverse-Tunnel offen (`ssh -L 5000:localhost:5000 cma@cma-pi-1`)
  PLUS socat-Helper auf der Pi (siehe Happy-Path-Runbook Schritt 2).
- Lokales venv aktiviert mit dev-Dependencies installiert.

`curl http://localhost:5000/v1/api/iserver/auth/status | jq` muss
`authenticated: true` zeigen.

## Lauf

### Variante A: ohne Reauth-Fail (sicher, behaelt Session)

```bash
python scripts/recording_session.py error-path \
    --record-dir tests/fixtures/recorded/live/errors \
    --base-url http://localhost:5000/v1/api \
    --account-id U25235077 \
    --yes
```

Provoziert vier Cases:
1. **Pacing**: 60 Snapshot-Calls in <1s — bricht ab, sobald HTTP 429 kommt.
2. **Ungueltige conid** (`/iserver/secdef/info?conid=999999999`).
3. **Ungueltige Order-Quantity** (`whatif` mit `quantity=0`, fragt vorher).
4. **Nicht-existente Order** (`/iserver/account/order/status/<bogus>`).

### Variante B: mit Reauth-Fail (zerstoert Session!)

```bash
python scripts/recording_session.py error-path \
    --record-dir tests/fixtures/recorded/live/errors \
    --base-url http://localhost:5000/v1/api \
    --account-id U25235077 \
    --with-reauth-fail \
    --yes
```

Zusaetzlicher Case:
5. **`POST /logout`**, dann `POST /reauthenticate`, dann `GET /iserver/auth/status` — zeichnet das Verhalten auf, wenn die Session weg ist. Die Session ist danach nicht mehr authenticated; der naechste Lauf braucht einen neuen Browser-2FA-Login.

## Reset nach Reauth-Fail

1. SSH-Tunnel weiter offen halten.
2. Browser auf `http://localhost:5000` (NICHT `https`).
3. Username + Passwort von U25235077, 2FA bestaetigen.
4. `curl http://localhost:5000/v1/api/iserver/auth/status` muss wieder
   `authenticated: true` zeigen, bevor weitere Recordings oder produktive
   Calls erfolgen.

## Beobachtungen aus dem ersten Lauf (2026-04-25)

| Case | Erwartet | Live-Antwort | Bewertung |
|------|----------|--------------|-----------|
| Pacing 60 Calls | HTTP 429 + `Retry-After` | **Alle 60 Calls 200 OK** | IBKR-Pacing griff im Test nicht. Moegliche Ursachen: IBKR-Wartung, Tunnel-Latenz puffert die Calls, IP-Counter zaehlt anders. Doku verspricht 10/s — re-test sobald Wartung vorbei ist. |
| Ungueltige conid | 404 / 400 | **HTTP 500** mit `{error: "Details currently unavailable. Please try again later..."}` | IBKR liefert generisches 500 statt 4xx. Service-Code-Mapping muss aus dem Body-Inhalt auf `not_found` schliessen, nicht aus dem HTTP-Status. |
| `qty=0` (whatif) | 400 / 422 | **HTTP 500** mit `{error: "Order size 0 is not valid. Please enter size greater than 0."}` | Wieder 500. `error`-Feld im Body ist eine Klartext-Message — Service kann z.B. `code = "invalid_input"` ableiten, sobald die Message `not valid` enthaelt. |
| Nicht-existente Order-ID | 404 | **HTTP 503** mit `{error: "Order ... is not found", "statusCode": 503}` | IBKR markiert sogar `not_found` als 503. Body enthaelt `statusCode: 503` als zusaetzliches Feld. Wieder: Mapping muss aus dem Body kommen. |
| `POST /logout` | 200 | **HTTP 200** `{status: true}` | Sauberer Pfad, keine Auffaelligkeit. |
| `POST /reauthenticate` (nach logout) | irgendeine 4xx/5xx | **HTTP 404** mit HTML `<h1>Resource not found</h1>` | IBKR liefert eine HTML-Error-Seite ohne JSON-Body. Der Recorder schreibt das in `body_text` statt `body_json`. Service-Code muss damit umgehen koennen. |
| `GET /iserver/auth/status` (nach logout) | `authenticated: false` | **HTTP 200** mit `{authenticated: false, established: false, competing: false, connected: false, MAC: null}` | Wichtig: Status-Endpoint bleibt erreichbar (200), nur die Booleans flippen. **Genau das Signal, das `cp/lifecycle.py` lesen muss**, um in `AuthStatus.AUTH_LOST` zu kippen — `connected: false` plus `authenticated: false` ist die zuverlaessigste Erkennung. |
| `GET /iserver/secdef/info` (im 2. Lauf) | wie zuvor | **HTTP 503** mit `{error: "Service Unavailable", statusCode: 503}` | Anders als beim ersten Lauf (`HTTP 500 + "Details currently unavailable"`). Bestaetigt die These, dass IBKR's generische Server-Errors zwischen 500 und 503 schwanken — Mapping muss tolerant sein. |

→ **Folgekarte 813fed62 muss den `cp_upstream_error`-Mapper schlauer machen**, sodass der Service IBKR's generische 500/503-Antworten nach `not_found` / `invalid_input` / `cp_upstream_error` aufschluesselt — abhaengig vom Body-Inhalt.

## Recording-Diff seed vs. live errors

Aktuelle Files unter `tests/fixtures/recorded/live/errors/`:

```
iserver_account_U25235077_orders_whatif__POST__noquery_01.json   HTTP 500 (qty=0 invalid)
iserver_account_order_status_999999999999__GET__noquery_01.json  HTTP 503 (unknown order)
iserver_auth_status__GET__noquery_01.json                         HTTP 200 (authenticated: false nach logout)
iserver_marketdata_snapshot__GET__e6536a99_01.json                HTTP 200 (kein Pacing-Treffer)
iserver_secdef_info__GET__63adaf00_01.json                        HTTP 503 (Service Unavailable - 2. Lauf)
logout__POST__noquery_01.json                                     HTTP 200 ({status: true})
reauthenticate__POST__noquery_01.json                             HTTP 404 (HTML "Resource not found")
```

`live-recording-manifest.json` haelt den Lauf-Snapshot (Cleanup der
59 redundanten 200-Snapshot-Files dokumentiert). Der Replay-Loader
ignoriert `live/errors/` automatisch — er sucht nur in `live/` und
`seed/` direkt. Diese Files sind dokumentarisch. Wenn der Mock einen
spezifischen Error-Body braucht, kann ein Test sie via `base_dir`-
Override im Loader laden (siehe `tests/cp_mock/loader.py`).
