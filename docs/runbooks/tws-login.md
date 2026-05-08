# Runbook: TWS-Container-Login (broker-gateway-tws)

Anleitung für den IB-Gateway-Container `broker-gateway-tws`
(Image `ghcr.io/gnzsnz/ib-gateway:10.45.1e`), der den `cpgateway`-Service
mittelfristig ersetzt. Stand Karte `8b1781d3`: Image- und Compose-Setup
fertig, Adapter-Code und Cutover folgen in Karten 2–6.

> **Abgrenzung zum CP-Gateway:** Solange `gateway` weiterhin via
> `depends_on: cpgateway` startet, läuft der TWS-Container **parallel**
> ohne aktive Service-Anbindung. Der Browser-2FA-Login auf dem
> CP-Gateway (`docs/runbooks/cpgateway-login.md`) bleibt der
> Production-Pfad bis zum Cutover. Dieses Runbook ist relevant, sobald
> jemand den TWS-Container für Smoke-Tests, Adapter-Entwicklung oder
> nach dem Cutover als Production-Adapter betreibt.

## Wie sich TWS-Login von CP-Gateway-Login unterscheidet

| Aspekt | CP-Gateway | TWS-Container |
|--------|------------|---------------|
| Auth-Mechanismus | Browser-Form + 2FA-Push (IBKR-Mobile / SMS) | IBC startet IB Gateway headless mit ENV-Credentials |
| 2FA | Erforderlich für Live-Account `U25235077` | Beim Live-Login Push an IBKR-Mobile-App; IBC wartet auf User-Bestätigung |
| Session-Pflege | Tickle alle 60 s (`/iserver/auth/tickle`) | IBC hält die Java-Session, daily Soft-Restart 23:59 |
| Container-Restart | Browser-Login muss neu durchlaufen | IBC re-loggt automatisch (auto-restart) |
| Sat-Reset | Manuelles 2FA am Sonntagmorgen | IBC re-loggt automatisch nach IBKR-Server-Reset |

## Voraussetzungen

- Compose-Stack ist auf dem Ziel-Host aufgesetzt (z.B.
  `cma-pi-1:/mnt/ssd/broker-gateway-paper`).
- IBKR-Credentials liegen am Host:
  - **Paper:** `/etc/default/broker-gateway-paper` (Mode 0600) mit
    `BG_PAPER_USERNAME=cborlm399` und `BG_PAPER_PASSWORD=…`.
  - **Live (zukünftig):** `/etc/default/broker-gateway` mit dem
    Live-Account-Holder. Live-Setup ist out-of-scope für dieses Runbook
    (Karte folgt).
- SSH-Zugang zum Ziel-Host.

## Smoke-Lifecycle (Test-Stack)

Für Adapter-Entwicklung oder einen Funktions-Smoke ohne den Live-
oder Paper-Stack zu beeinträchtigen:

```bash
ssh cma@cma-pi-1
cd /tmp/bg-tws-validate              # Test-Verzeichnis ohne Cutover-Risiko
sudo bash -c '. /etc/default/broker-gateway-paper && cat > .env.tws-smoke <<EOF
BG_TRADING_MODE=paper
BG_TWS_HOST_PORT=4102
BG_TWS_INTERNAL_PORT=4002
TWS_USERID=$BG_PAPER_USERNAME
TWS_PASSWORD=$BG_PAPER_PASSWORD
EOF'
sudo chown cma:cma .env.tws-smoke && sudo chmod 600 .env.tws-smoke

docker compose -p bg-tws-smoke --env-file .env.tws-smoke up -d tws
docker logs -f bg-tws-smoke-tws | grep -E "(Login has completed|Configuration tasks)"
```

Erwartete Login-Sequenz im Container-Log:

```
IBC: Setting Trading mode = paper
IBC: Setting user name
IBC: Setting password
IBC: Click button: Paper Log In
IBC: detected dialog entitled: Existing session detected
IBC: Don't know the type of the other session, so continue this one (scenario 3)
IBC: Click button: Continue Login
IBC: Login has completed
IBC: Read-Only API checkbox is already set to: true
IBC: Auto restart time set to 11:59 PM
IBC: Configuration tasks completed
```

Smoke-Test:

```bash
docker run --rm --network container:bg-tws-smoke-tws \
    -v $PWD/scripts/smoke_tws.py:/smoke.py:ro \
    python:3.13-slim bash -c "pip install -q ib_async && python /smoke.py"
```

Teardown:

```bash
docker compose -p bg-tws-smoke --env-file .env.tws-smoke down -v
```

## Bekannte Quirks

### Single-Session pro Account

IBKR erlaubt nur eine aktive Session pro Konto. Wenn zur Smoke-Zeit der
`broker-gateway-paper`-Stack mit demselben Login (`cborlm399`) auf
seinem CP-Gateway authentifiziert ist, wird die Smoke-Session sie
weckicken (Spike-Quirk `ExistingSessionDetectedAction=primary`). Memory
`project_paper_session_loop.md` deckt das Verhalten ab — neuer
Browser-Login auf `127.0.0.1:5001` ist nach dem Smoke fällig, falls
der Paper-Stack wieder gebraucht wird.

### Connect nur aus geteiltem Network-Namespace

Der TWS-Adapter (Folge-Karte 2) muss sich das Network-Namespace mit
dem `tws`-Service teilen, um den `127.0.0.1`-Loopback nutzen zu können
(siehe `ops/tws/README.md` "Bekanntes Issue: Connect aus dem Bridge-
Netz"). Direkter Connect aus dem Bridge-Netz oder vom Host läuft in
einen Timeout, weil IB Gateway nicht-trusted Source-IPs silent droppt.

### Daily Auto-Restart 23:59

IBC startet IB Gateway täglich um 23:59 (Container-Zeitzone
`Europe/Vienna`) durch. Während der ~30 s Restart-Phase ist der
API-Port nicht erreichbar — Adapter müssen Reconnect tolerieren. Die
Setting kommt aus `AUTO_RESTART_TIME` in `compose.yaml` und kann pro
Stack via `.env` überschrieben werden.

### Sat-Reset (Sonntag 03:00 UTC)

IBKR-Server resetten die Sessions am Sonntagmorgen. Der gnzsnz-
Container fängt das automatisch ab — IBC erkennt den Disconnect und
loggt sich neu ein. Kein manueller Eingriff nötig, anders als beim
CP-Gateway.

## Production-Lifecycle (nach Cutover)

Wird in Folge-Karte 6 (Hard-Cutover) ausgearbeitet, sobald der
Adapter-Code (Karte 2) und die Lifecycle-Anpassung (Karte 3) stehen.
Voraussichtlicher Pfad:

1. `tws`-Container hochfahren mit `TWS_USERID`/`TWS_PASSWORD` aus dem
   Stack-spezifischen `/etc/default/broker-gateway{,-paper}`.
2. IBC-Login abwarten (Paper: ~10 s, Live: User-Push auf IBKR-Mobile,
   ~30 s).
3. Adapter-Service (im selben Network-Namespace wie `tws`) connectet
   und meldet `health=ok`.
4. `gateway`-Service routet Traffic zum Adapter statt zum cpgateway-
   Service.

## Referenzen

- `ops/tws/README.md` — Image-Strategie, ENV-Tabelle, Spike-Quirks-Mapping.
- `scripts/smoke_tws.py` — Smoke-Skript für Account-Summary.
- Memory `project_tws_api_pi5_setup.md` — Spike-Erkenntnisse (Karte
  `368ccdfe`), IBC-Config-Defaults, Symlink-Workarounds.
- Memory `project_psm_ibgateway_container.md` — PSM nutzt das
  gnzsnz-Pattern bereits, dient als Vorbild.
- Memory `project_paper_session_loop.md` — Single-Session-Constraint.
- gnzsnz/ib-gateway-docker — https://github.com/gnzsnz/ib-gateway-docker
- IBC — https://github.com/IbcAlpha/IBC
