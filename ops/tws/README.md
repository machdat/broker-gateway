# broker-gateway-tws

Container-Slot für den IBKR-Trading-Adapter via **TWS-API** (statt CP-Gateway).
Ersetzt mittelfristig den `cpgateway`-Service; in dieser Karte (8b1781d3) wird
nur das Image- und Compose-Setup geliefert, der Adapter-Code und der Cutover
folgen in separaten Karten.

## Image-Strategie

**Pull statt Build.** Wir pinnen auf das Open-Source-Image
[`ghcr.io/gnzsnz/ib-gateway`](https://github.com/gnzsnz/ib-gateway-docker)
und bauen nichts Eigenes.

| Aspekt | Wert / Begründung |
|--------|-------------------|
| Image | `ghcr.io/gnzsnz/ib-gateway:10.45.1e` |
| Channel | `stable` (Maintainer-zusätzlich getestet, eigener Tag-Suffix) |
| Multi-Arch | `linux/amd64` + `linux/arm64` (verifiziert via `docker manifest inspect`) |
| Lizenz | MIT |
| Inhalt | IB Gateway 10.45.1e + IBC 3.23.0 + Zulu JRE 17.0.16 (Ubuntu 24.04) |
| Spike-Bezug | Karte 368ccdfe verifizierte IB GW 10.46 + IBC 3.23.0 + JDK 17.0.16 — gnzsnz ist 1 Minor unter Spike, Rest identisch |
| PSM-Track-Record | `psm-ibgateway-gnzsnz` läuft auf älterem stable (`10.37.1q`) seit Monaten |

Eigener Build wäre aufwändiger (Bellsoft + IB Gateway + IBC + Xvfb selbst
schichten) ohne klaren Mehrwert: Versionen sind beim Maintainer ohnehin
gepinnt, ARM64 ist gelöst, MIT-Lizenz erlaubt unbeschränkte Nutzung.

## Versions-Historie

| Datum | Tag | Anlass |
|-------|-----|--------|
| 2026-05-08 | `10.45.1e` | Initial-Pin (Karte 8b1781d3, gnzsnz aktueller stable) |

Beim Tag-Bump:
1. `docker manifest inspect ghcr.io/gnzsnz/ib-gateway:<neuer-tag>` — multi-arch verifizieren.
2. gnzsnz-Repo Release Notes & IBC-Changelog gegenchecken.
3. Tag in `compose.yaml` und in dieser Tabelle ändern, PR.
4. Smoke gegen Paper-Konto DUP799747 vor Cutover auf Live.

## Compose-Service-Layout

Service heißt `tws` und läuft parallel zum bestehenden `cpgateway`. In
dieser Karte hängt der `gateway`-Service noch via `depends_on` an
`cpgateway`; der Cutover auf `tws` passiert in Karte 5 (Single-Owner-
Coordination) bzw. 6 (Hard-Cutover).

### Ports

| Stack | Host-Bind | Container-Port | ENV |
|-------|-----------|----------------|-----|
| Live  | `127.0.0.1:4101` | `4001` (IB-GW Live-Default) | `BG_TWS_HOST_PORT=4101` |
| Paper | `127.0.0.1:4102` | `4002` (IB-GW Paper-Default) | `BG_TWS_HOST_PORT=4102` |

**Abweichung von Karte:** Die Karte forderte ursprünglich `4001`/`4002`
direkt am Host. Das kollidiert auf cma-pi-1 mit `gateway-paper:4001`
(Live- und Paper-Stack teilen sich den Host-Port-Namespace). Wir setzen
deshalb `41xx` als TWS-Host-Ports — die Container-internen Ports bleiben
`4001`/`4002` (gnzsnz-Default je nach `TRADING_MODE`).

Bind ausschließlich auf `127.0.0.1`. Externe Erreichbarkeit ist nicht
vorgesehen — der Adapter läuft im Compose-Netz und spricht `tws:4001`/
`tws:4002` direkt an.

### Volumes

| Volume | Mount | Zweck |
|--------|-------|-------|
| `tws-settings` (named) | `/home/ibgateway/Jts` | Persistente Account-Settings, Logs, Auto-Restart-Marker |
| Bind: `./ops/tws/config.ini.tmpl` | `/home/ibgateway/ibc/config.ini.tmpl:ro` | IBC-Template mit broker-gateway-Quirks |
| Bind: `./ops/tws/jts.ini.tmpl` | `/home/ibgateway/Jts/jts.ini.tmpl:ro` | IB-Gateway-Settings-Template |

`config.ini` und `jts.ini` selbst werden vom gnzsnz-Entrypoint per
`envsubst` aus den Templates erzeugt — wir pflegen die Templates, nicht
die fertigen Dateien.

### ENV-Variablen

Setzbar via `.env` (Live-Stack) bzw. `.env.paper` (Paper-Stack). Vorlagen:
[`.env.live.template`](../../.env.live.template),
[`.env.paper.template`](../../.env.paper.template).

| Variable | Pflicht | Default | Zweck |
|----------|---------|---------|-------|
| `TWS_USERID` | ja | – | IBKR-Login-Name (Live: `U25235077`-Account-Holder, Paper: `cborlm399`) |
| `TWS_PASSWORD` | ja | – | IBKR-Password. Niemals einchecken; auf Pi via `/etc/default/broker-gateway-{paper,live}` (Mode 0600). |
| `TRADING_MODE` | ja | `paper` | `paper` oder `live` — wählt den Container-internen Port (4002 vs. 4001) und den IBKR-Endpoint. |
| `READ_ONLY_API` | nein | `yes` | `yes` blockt Order-Submit auf API-Ebene. Erst auf `no` flippen, wenn der TWS-Adapter (Karte 5/6) Orders kontrolliert. |
| `BYPASS_WARNING` | nein | `yes` | gnzsnz-Default für headless Betrieb |
| `EXISTING_SESSION_DETECTED_ACTION` | nein | `primary` | Spike-Quirk: laufende Session übernehmen statt abzubrechen |
| `TWS_ACCEPT_INCOMING` | nein | `accept` | Spike-Quirk: API-Connect ohne UI-Dialog akzeptieren |
| `ALLOW_BLIND_TRADING` | nein | `no` | Hard-no, bis Order-Pfad vollständig verifiziert |
| `SAVE_TWS_SETTINGS` | nein | `yes` | UI-/Account-Settings beim Shutdown persistieren |
| `AUTO_RESTART_TIME` | nein | `11:59 PM` | Daily Soft-Restart durch IBC |
| `RELOGIN_AFTER_TWOFA_TIMEOUT` | nein | `yes` | Live-2FA: nach Timeout neu versuchen |
| `TWOFA_DEVICE` | nein | – | Live-2FA: IBKR-Mobile (Paper braucht das nicht) |
| `TWOFA_EXIT_INTERVAL` | nein | – | Live-2FA: Exit-Interval in Minuten |
| `TIME_ZONE` | nein | `Europe/Vienna` | jts.ini Logon-Section |

`TWS_USERID_PAPER` / `TWS_PASSWORD_PAPER` sind im gnzsnz-Image als
zusätzliches Schema vorgesehen, werden hier aber nicht genutzt — wir
trennen Paper und Live durch separate Stacks (`COMPOSE_PROJECT_NAME`).

## Smoke-Test

Skript: [`scripts/smoke_tws.py`](../../scripts/smoke_tws.py).

Verifiziert auf cma-pi-1 in Phase 3 von Karte `8b1781d3`. Ablauf:

```bash
# Auf cma-pi-1
ssh cma@cma-pi-1
cd /tmp/bg-tws-validate            # oder /mnt/ssd/broker-gateway-paper

# .env mit Paper-Credentials anlegen
sudo bash -c '. /etc/default/broker-gateway-paper && cat > .env.tws-smoke <<EOF
BG_TRADING_MODE=paper
BG_TWS_HOST_PORT=4102
BG_TWS_INTERNAL_PORT=4002
TWS_USERID=$BG_PAPER_USERNAME
TWS_PASSWORD=$BG_PAPER_PASSWORD
EOF'
sudo chown cma:cma .env.tws-smoke && sudo chmod 600 .env.tws-smoke

# Container hochfahren (separates Compose-Project, damit der bestehende
# broker-gateway-paper-Stack unangetastet bleibt)
docker compose -p bg-tws-smoke --env-file .env.tws-smoke up -d tws

# IBC-Login abwarten (~45s) - "Configuration tasks completed" zeigt grün
docker logs -f bg-tws-smoke-tws | grep -E "(Login has completed|Configuration tasks)"

# Smoke-Skript ausführen - Network-Namespace-Sharing mit dem tws-Container,
# damit der Connect aus 127.0.0.1 kommt (siehe "Bekanntes Issue" unten).
docker run --rm --network container:bg-tws-smoke-tws \
    -v $PWD/scripts/smoke_tws.py:/smoke.py:ro \
    python:3.13-slim bash -c "pip install -q ib_async && python /smoke.py"
```

Erwarteter Output (Werte schwanken):

```
connected=True  server_version=178
--- Account Summary (DUP799747) ---
  DUP799747  AccountType        INDIVIDUAL
  DUP799747  AvailableFunds     1000549.74 EUR
  DUP799747  BuyingPower        6670331.61 EUR
  DUP799747  NetLiquidation     1001203.84 EUR
  DUP799747  TotalCashValue     999815.90 EUR
disconnected.
```

**Hinweis zur Paper-Session:** Wenn parallel der `broker-gateway-paper`-
Stack mit `cborlm399`-Login auf dem CP-Gateway läuft, wird der Smoke
diese Session beenden (eine IBKR-Session pro Konto). Memory
`project_paper_session_loop.md` deckt das Verhalten ab — neuer Browser-
Login auf 127.0.0.1:5001 ist fällig, wenn der Paper-Stack wieder
gebraucht wird.

Teardown des Smoke-Stacks:

```bash
docker compose -p bg-tws-smoke --env-file .env.tws-smoke down -v
```

## Bekanntes Issue: Connect aus dem Bridge-Netz

Der Smoke funktioniert nur, wenn der Client im **gleichen Network-
Namespace** wie der `tws`-Container läuft (z.B. via
`--network container:bg-tws-smoke-tws`). Direkter Connect …

- … vom Host an `127.0.0.1:4102` → docker-proxy NAT, Source-IP aus
  IB-Gateway-Sicht ist die Bridge-Gateway-IP. **TimeoutError.**
- … aus einem Sidecar-Container im selben Bridge-Netz an `tws:4002` →
  Source-IP ist die Container-IP (`172.x.x.x`). **TimeoutError.**

…obwohl IBC `[IBGateway]/TrustedIPs=127.0.0.1,172.16.0.0/12` in
`jts.ini` patcht (`TrustedTwsApiClientIPs=172.16.0.0/12` in
`config.ini.tmpl`). Vermutung: IB Gateway selbst akzeptiert keine
CIDR-Notation in `TrustedIPs`, sondern nur konkrete IPs — und droppt
nicht-trusted Connections silent (kein Log-Eintrag, kein Reset).

**Konsequenz für die Adapter-Karte (Folge-Karte 2):** Der Adapter-
Container (oder der `gateway`-Service nach Cutover in Karte 5/6) muss
sein Network-Namespace mit dem `tws`-Service teilen, sonst kommt der
Connect nicht durch:

```yaml
gateway:
  network_mode: "service:tws"
```

Alternativ: konkrete Container-IPs in `TrustedTwsApiClientIPs` pflegen
via Static-IP-Pinning im Bridge-Netz. Empfehlung: `network_mode: service:tws`
ist sauberer, weil tws-Adapter ohnehin 1:1 gekoppelt sind.

## Spike-Quirks-Mapping

| Karten-Constraint | Wo gesetzt | Wert |
|-------------------|------------|------|
| `ExistingSessionDetectedAction=primary` | ENV `EXISTING_SESSION_DETECTED_ACTION` → config.ini | `primary` |
| `AcceptIncomingConnectionAction=accept` | ENV `TWS_ACCEPT_INCOMING` → config.ini | `accept` |
| `ReadOnlyApi=yes` | ENV `READ_ONLY_API` → config.ini | `yes` |
| `TradingMode=paper` | ENV `TRADING_MODE` → config.ini | `paper` (Stack-spezifisch) |

Alle vier Quirks landen über die ENVs in der erzeugten `config.ini` —
das Template referenziert sie direkt. `envsubst` ersetzt die Werte beim
Container-Start.

## Was in dieser Karte NICHT enthalten ist

- Adapter-Code (`src/broker_gateway/tws/`) — Folge-Karte 2.
- Lifecycle-Anpassung (`gateway`-Service hängt noch an `cpgateway`,
  nicht an `tws`) — Folge-Karte 3.
- v1-Spec-Drift gegen TWS-Semantik — Folge-Karte 4.
- Single-Owner-Coordination (welcher Service besitzt die IBKR-Session) —
  Folge-Karte 5.
- Migration-Pfad / Hard-Cutover (`cpgateway` deprecaten) — Folge-Karte 6.
- Pi-Deploy aus dieser Karte — kein Cutover hier.

## Referenzen

- gnzsnz Repo: https://github.com/gnzsnz/ib-gateway-docker (MIT)
- IBC Repo: https://github.com/IbcAlpha/IBC (3.23.0, Apache-2.0)
- IB Gateway Standalone: https://download2.interactivebrokers.com/installers/ibgateway/
- Spike-Karte: `368ccdfe` (TWS-API + IB Gateway 10.46 + IBC 3.23.0 + Bellsoft 17.0.16+12 auf Pi 5 verifiziert)
- Memory: `project_tws_api_pi5_setup.md`, `project_psm_ibgateway_container.md`
