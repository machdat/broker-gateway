# Auto-Login Paper-Stack — Setup-Runbook

Bezug: KanPrompt-Karte `ece90a8e-3a5a-4bb4-a875-6e992de359ff` Phase B.

Aktiviert auf cma-pi-1 den Auto-Login-Sidecar fuer den Paper-Stack,
der nach jedem Container-Recreate des `paper-cpgateway` einen
automatischen Login mit Username + Passwort durchfuehrt (kein 2FA,
weil der Paper-Account `cborlm399` ohne 2FA akzeptiert wird).

> ⚠️ **Live-Stack ist explizit ausgeschlossen.** Hard-Guard 1 lehnt
> `BG_STACK_KIND=live` zusammen mit `BG_PAPER_AUTO_LOGIN=1` ab —
> `validate_runtime_config` bricht den Lifespan beim Start mit
> `ConfigError`. Hard-Guard 3 verbietet einen `docker.sock`-Mount
> im Live-Compose. Niemals von Hand umgehen.

## 1. Voraussetzungen

- Service auf v1.29.0 deployt (oder neuer).
- `cma`-User auf cma-pi-1 ist Mitglied von `docker` (`id cma | grep docker`).
- Paper-Account-Credentials (Username + Passwort) liegen im
  Passwort-Manager — nicht in einer Datei auf dem Laptop.
- Paper-Stack laeuft (`docker ps --filter name=broker-gateway-paper`).

## 2. Credentials auf dem Pi hinterlegen

**Pfad:** `/etc/default/broker-gateway-paper` (Mode `0600`, root:root).

```bash
ssh cma@cma-pi-1
sudo install -m 0600 -o root -g root /dev/null /etc/default/broker-gateway-paper
sudo tee /etc/default/broker-gateway-paper >/dev/null <<'EOF'
BG_PAPER_USERNAME=cborlm399
BG_PAPER_PASSWORD=<aus passwort-manager>
BG_PAPER_AUTO_LOGIN=0
BG_AUTO_LOGIN_IMAGE=broker-gateway-paper-auto-login:1.29.0
BG_AUTO_LOGIN_NETWORK=broker-gateway-paper_default
BG_AUTO_LOGIN_TARGET_URL=http://broker-gateway-paper-cpgateway:5000/
EOF
sudo chown root:root /etc/default/broker-gateway-paper
sudo chmod 0600 /etc/default/broker-gateway-paper
```

`BG_PAPER_AUTO_LOGIN=0` als Default haelt den Trigger inaktiv —
Phase-B-Aktivierung passiert in Schritt 4 nach dem ersten
manuellen Smoke.

`ops/build-gateway.sh --env=paper` liest die Datei automatisch ein
(`set -a; . /etc/default/broker-gateway-paper; set +a`) und reicht
die Werte als Compose-Env weiter.

## 3. Sidecar-Image bauen

```bash
ssh cma@cma-pi-1
cd /mnt/ssd/broker-gateway-paper
git pull
./ops/build-gateway.sh --env=paper
```

Das Skript baut zusaetzlich das Sidecar-Image auf `linux/arm64`:

```
[2/4] docker build broker-gateway-paper-auto-login (linux/arm64)
```

Pruefen:

```bash
docker images broker-gateway-paper-auto-login
# REPOSITORY                       TAG     IMAGE ID    CREATED      SIZE
# broker-gateway-paper-auto-login  1.29.0  ...         ...          ~600MB
```

## 4. Manueller Smoke vor Aktivierung (Hard-Guard-Test)

**Smoke 1: Trigger ist NICHT attached, weil `BG_PAPER_AUTO_LOGIN=0`.**

```bash
TOKEN=$(sudo grep BG_BOOTSTRAP_ADMIN_TOKEN /etc/default/broker-gateway-paper | cut -d= -f2)
curl -sS -X POST http://localhost:4001/v1/admin/auto-login/trigger \
    -H "Authorization: Bearer $TOKEN" | jq .
```

Erwartet: HTTP 503 mit `{"error":{"code":"auto_login_disabled",...}}`.

**Smoke 2: Aktivierung.**

```bash
sudo sed -i 's/^BG_PAPER_AUTO_LOGIN=.*/BG_PAPER_AUTO_LOGIN=1/' \
    /etc/default/broker-gateway-paper
./ops/build-gateway.sh --env=paper  # baut + restartet gateway
```

Nach dem Restart einmal manuell anstossen:

```bash
curl -sS -X POST http://localhost:4001/v1/admin/auto-login/trigger \
    -H "Authorization: Bearer $TOKEN" | jq .
```

Erwartet (wenn `paper-cpgateway` aktuell eingeloggt ist): HTTP 200 mit
`{"skipped": true, "reason": "auth_status_ok", ...}`.

**Smoke 3: erzwungenes Container-Recreate.**

```bash
docker compose --env-file .env.paper -p broker-gateway-paper \
    -f compose.yaml -f compose.paper.auto-login.yaml \
    up -d --no-deps --force-recreate cpgateway
```

Innerhalb von 90 Sekunden sollte der naechste Tickle-Loop den
Auto-Login triggern. Pruefen:

```bash
curl -sS http://localhost:4001/v1/internal/health \
    -H "Authorization: Bearer $TOKEN" | jq .
```

Erwartete Felder:
- `auth_status: "ok"`
- `last_auto_login_attempt_at: <ISO-Datetime>`
- `last_auto_login_success_at: <ISO-Datetime>`
- `auto_login_throttle_state: "cooldown_5min"` (gerade gelaufen)

## 5. Beobachtung im Log

Sidecar-Logs landen via `logger.info("auto-login sidecar stdout: %s", ...)`
im strukturierten gateway-Log (`docker logs broker-gateway-paper`). Format:

```
auto-login sidecar stdout: {"phase":"start","target":"http://broker-gateway-paper-cpgateway:5000/","ts":...,"username":"cb***99"}
auto-login sidecar stdout: {"phase":"done","duration_s":3.42,"error":"","exit_code":0,"phase":"done","ts":...}
```

Klartext-Username/Passwort darf NIRGENDS auftauchen. Wenn ein
Klartext-Username im Log auftaucht: sofortiger Bug — Maskierung
greift nicht.

## 6. Deaktivierung / Rollback

Schnellster Weg:

```bash
sudo sed -i 's/^BG_PAPER_AUTO_LOGIN=.*/BG_PAPER_AUTO_LOGIN=0/' \
    /etc/default/broker-gateway-paper
docker compose --env-file .env.paper -p broker-gateway-paper \
    -f compose.yaml -f compose.paper.auto-login.yaml \
    up -d --no-deps gateway
```

Der naechste Lifespan-Start sieht `BG_PAPER_AUTO_LOGIN=0` und haengt
den Trigger nicht an. `/v1/admin/auto-login/trigger` antwortet wieder
mit HTTP 503.

## 7. Was NICHT passiert

- Auto-Login wird **nie** im Live-Stack aktiv. Hard-Guard 1+1b
  verhindern das schon im Lifespan-Start.
- Auto-Login wird **nicht** mehr versucht, wenn IBKR ploetzlich 2FA
  verlangt. Sidecar exitet mit Code 4, Throttle-State springt auf
  `2fa_required_manual_intervention`, jeder weitere Trigger wird
  geskipt — bis ein Mensch den Service neu startet.
- Mehr als 5 Versuche pro Tag werden **nicht** durchgereicht. Nach
  dem Limit: Throttle-State `daily_limit_reached`, kein neuer Sidecar-
  Aufruf bis zum naechsten Tag.

## 8. Bezug

- Karten-Skill: `mcp__kanprompt__get_skill(name="implementation")`.
- Reverse-Engineering: `docs/research/cpgateway-login-flow.md`.
- Architektur-Pfad: `docs/02-architecture.md` Sektion 6.4.
- Sicherheits-Modell: `docs/04-security.md` Sektion 7.4.
- **Token-Store-Recovery nach Container-Recreate:**
  [`token-store-recreate.md`](token-store-recreate.md). Auto-Login fixt die
  cpgateway-Session, aber Konsumenten-Tokens koennen vor v2.1.4 oder bei
  geloeschtem Volume trotzdem 401 liefern.
