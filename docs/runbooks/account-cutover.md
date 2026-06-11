# Runbook: broker-gateway-Live-Account-Cutover

**Verifiziert am 2026-06-08** (Cutover vollzogen, v2.5.0, Karte
`0ef946c8`; Bereinigung Phase 3 mit v2.5.1, Karte `07b244b1`).

High-level Operator-Pfad fuer den **einmaligen** Wechsel der Live-Account-
Identitaet von U25235077 (Operator-Privatkonto chmangold) auf das
dedizierte Service-Konto. Der Singular-Halter
([`02-architecture.md`](../02-architecture.md) Sektion 3.1) bleibt; nur
die Identitaet hinter ihm wechselt.

> **Dieser Runbook ist absichtlich high-level** und wurde vor dem
> Cutover als Pruef-Checkliste geschrieben. Der tatsaechliche Ablauf
> und drei Korrekturen gegenueber dem angenommenen Pfad stehen in
> Sektion 0; die Abschnitte 2-5 bleiben als Referenz fuer einen
> etwaigen kuenftigen Konto-Wechsel erhalten.

## 0. Durchgefuehrt am 2026-06-08 (v2.5.0, Karte `0ef946c8`)

Der Cutover wurde am 2026-06-08 vollzogen. **Drei Korrekturen gegenueber
dem urspruenglich angenommenen Pfad unten** — die Checklisten-Abschnitte
2-5 sind entsprechend zu lesen:

1. **Credentials liegen in `/mnt/ssd/broker-gateway/.env`** (Keys
   `BG_TWS_USERNAME` / `BG_TWS_PASSWORD` / `BG_TWS_TRADING_MODE`),
   **nicht** in `/etc/default/broker-gateway` (dort steht nur
   `BG_TOKEN_DIR_HOST`). Tausch + Backup (`.env.bak` = Original mit
   U25235077) erfolgten in dieser Datei.
2. **`tws-health` enthaelt kein `account_id`-Feld** (nur `connected`,
   `host`, `port`, `paper`, `read_only`, `client_id`). Die Konto-
   Identitaet wird ueber die Portfolio-Summary verifiziert.
3. **Summary-Pfad ist `GET /v1/portfolio/{accountId}`** (ohne
   `/summary`-Suffix; `/summary` liefert `404 not_found`).

Tatsaechlicher Ablauf: `.env`-Credentials auf das Service-Konto getauscht
(Operator), `docker compose down && up -d` auf dem Live-Stack, einmaliger
2FA-Push (IB Key), ~60 s Warmup. Verifikation: `tws-health`
`connected=true` / `paper=false`; Portfolio-Summary des Service-Kontos
HTTP 200 mit echten Daten; Gegencheck `U25235077` liefert nur
`null`-Werte (entkoppelt). Rollback-Pfad (`cp .env.bak .env` -> recreate
-> 2FA U25235077) validiert, nicht ausgeloest. Account-ID + Credentials
bewusst **nicht** im oeffentlichen Repo — nur Pi-`.env` +
Passwort-Manager.

## 1. Wann tritt das auf

Einmalig, wenn das dediziertes Service-Konto bei IBKR aktiv ist
(Permissions freigeschaltet, 2FA konfiguriert, mindestens einmal vom
Operator im IBKR-Portal verifiziert). Der Cutover-Zeitpunkt waehlt der
Operator — bevorzugt **ausserhalb der US-Trading-Hours** (vor 15:30
oder nach 22:00 Berlin), idealerweise am Wochenende. Falls der Live-
Stack zum Cutover-Zeitpunkt ohnehin abgeschaltet ist (z.B. waehrend
einer Consumer-Entwicklungsphase), entfaellt diese Einschraenkung.

Phase-Plan in drei Karten:

| Phase | KanPrompt-Karte | Scope |
|-------|-----------------|-------|
| 1 (abgeschlossen, v2.2.2) | `cdb262f7` (Doku-Vorbereitung) | Architektur-/Security-/Glossar-/CLAUDE.md-Edits + dieser Runbook |
| 2 (vollzogen 2026-06-08, v2.5.0) | `0ef946c8` (Konto-Cutover) | Credentials-Tausch + Compose-Recreate + Smoke + Doku-Aktualisierung |
| 3 (abgeschlossen, v2.5.1) | `07b244b1` (U25235077-Bereinigung) | "heute aktiv"-Phrasen entfernen, Memory-Sweep, Cassette-Kontextualisierung |

## 2. Voraussetzungen (vor Phase 2)

1. **Konkrete Account-ID** (z.B. `U99999999`) liegt vor. Format wird in
   der Phase-2-Karte verifiziert.
2. **Username + Passwort** des Service-Kontos sind im Passwort-Manager
   hinterlegt. NICHT in Karten, Logs, PR-Bodies oder Memories speichern.
3. **IBKR-Permissions** sind freigeschaltet: mindestens Aktien + Cash/FX
   (gleicher Bundle wie U25235077). Bei Abweichungen entweder beim
   IBKR-Support nachsteuern oder die abweichenden Permissions in einer
   Folge-Karte adressieren, **bevor** der Cutover startet.
4. **2FA-Methode** ist konfiguriert. Default: "IB Key" (gnzsnz/IBC
   `TWOFA_DEVICE=IB Key`, Push-Bestaetigung am Handy). Andere Methoden
   funktionieren, brauchen aber ggf. eine Anpassung im `compose.yaml`
   `BG_TWS_2FA_DEVICE`-Override.
5. **Operator hat das Service-Konto mindestens einmal im IBKR-Portal
   eingeloggt** — verifiziert, dass es nicht in einem Anfaenger-Lockout
   haengt oder ungewoehnliche Pflichthinweise zeigt (Risiko-
   Vereinbarung, Pre-Pro-Bestaetigung etc.).
6. **Operator-Backup von `/etc/default/broker-gateway`** liegt vor
   (`.bak`-Kopie mit den U25235077-Credentials), damit der Rollback-
   Pfad eine eindeutige Quelle hat.

## 3. Cutover-Schritte (high-level)

1. **Credentials atomar tauschen.** `/etc/default/broker-gateway` auf
   `BG_TWS_USERNAME=<service-username>` und
   `BG_TWS_PASSWORD=<service-password>` umstellen. Mode bleibt `0600`,
   `root:root`. `BG_TWS_TRADING_MODE=live` bleibt. Falls die Account-ID
   als eigener ENV-Wert gefuehrt wird (`BG_TWS_ACCOUNT_ID` etc.), in
   derselben Datei aktualisieren.
2. **`ops/tws/`-Templates pruefen.** Aktuell haengt die Account-ID
   nicht in den TWS-/IBC-Templates (Stand v2.2.x — TRADING_MODE wird
   durchgereicht, Account-ID kommt aus dem IBC-Login). Falls bis zum
   Cutover-Zeitpunkt eine Template-Variable hinzukam, wird sie in der
   Phase-2-Karte mit dokumentiert.
3. **Compose-Recreate.** `cd /mnt/ssd/broker-gateway && docker compose
   down && docker compose up -d` (Live-Stack, Default-Compose ohne
   `cp-legacy`-Profile). Force-recreate ist nicht zwingend, weil
   `docker compose up` die ENV-Datei neu liest, aber kann zur Vorsicht
   genutzt werden.
4. **2FA-Push am Handy.** IBC triggert den Login des Service-Kontos.
   Operator bestaetigt den Push einmal. **Bei Fehlversuch oder unklarem
   Verhalten SOFORT rollback** — IBKR sperrt bei wiederholten Fehl-
   Logins (Memory `feedback_ibkr_lockout_threshold`).
5. **Warmup-Pause 60-90 s** (Memory `project_ibkr_session_resume`)
   bevor der erste Health-Check geprueft wird.

## 4. Smoke-Reihenfolge (in dieser Ordnung)

| Smoke | Befehl | Erwartet |
|-------|--------|----------|
| Container-Logs | `docker logs broker-gateway-tws --tail 100` | `IBC: Login has completed`, `Configuration tasks completed`, Account-Nummer entspricht dem Service-Konto. |
| TWS-Listener | `docker exec broker-gateway-tws bash -c 'exec 3<>/dev/tcp/127.0.0.1/${BG_TWS_PORT:-4003}'` | Exit 0 (Socket erreichbar). |
| TWS-Health | `curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:4000/v1/internal/tws-health \| jq .` | `connected=true`, `account_id=<neue-id>`. |
| Portfolio | `curl -sS -H "Authorization: Bearer $TOKEN" http://localhost:4000/v1/portfolio/<neue-id>/positions` | HTTP 200 + JSON-Body. Bei frisch eroeffnetem Konto ist die Position-Liste leer; das ist OK. |
| Portfolio-Summary | `curl -sS -H "Authorization: Bearer $TOKEN" http://localhost:4000/v1/portfolio/<neue-id>/summary` | HTTP 200, plausible Cash-Werte (`net_liquidation`, `total_cash_value`). |
| Stabilitaet | `/v1/internal/tws-health` im 5-Minuten-Takt fuer 30 Minuten | Mind. 6 Polls mit `connected=true`. |

Liefert ein Smoke einen unerwarteten Fehler, gilt Schritt 5 (Rollback).

## 5. Rollback

**Status: theoretisch** — beim Cutover am 2026-06-08 validiert
(Backup-Pfad `.env.bak` geprueft), aber nie ausgeloest.

Wenn ein Smoke scheitert, der 2FA-Push abgelehnt wird, oder der Service
nach 30 Minuten nicht stabil bleibt:

1. `docker compose down` auf dem Live-Stack.
2. `sudo mv /etc/default/broker-gateway /etc/default/broker-gateway.failed-cutover.<datum>`
   (zur Diagnose aufheben).
3. `sudo mv /etc/default/broker-gateway.bak /etc/default/broker-gateway`
   (Backup zurueckspielen — siehe Voraussetzungen Schritt 6).
4. `docker compose up -d` mit den U25235077-Credentials.
5. 2FA-Push am Handy fuer U25235077 bestaetigen.
6. Smoke-Reihenfolge aus Sektion 4 gegen U25235077 erneut, um den
   Vor-Cutover-Stand zu verifizieren.
7. Cutover-Karte mit `add_log_entry(action='Rollback', details='Grund')`
   final, neue Folge-Karte fuer Cutover-V2 anlegen.

## 6. Was dieser Runbook NICHT abdeckt

- **Memory-Bereinigung** in `spaces/.../memory/*.md`. Phase 3 (Karte
  `07b244b1`) — bewusst spaeter, damit der Service erst stabil laeuft
  bevor die Memory-Inhalte umgeschrieben werden.
- **Cassette-Inhalte** unter `tests/fixtures/recorded/live/`. Bleiben
  mit `U25235077` als deterministische Mock-Daten — sind kein Hinweis
  auf das aktive Live-Konto.
- **CHANGELOG-Historie.** Append-only. Eintraege mit U25235077
  beschreiben den Service-Stand zu ihrer Zeit und werden nicht
  umgeschrieben.
- **Live-Recordings neu aufnehmen.** Erst wenn der Service auf dem
  Service-Konto stabil laeuft UND ein konkreter Drift-Bedarf entsteht.
  Bis dahin sind die U25235077-Cassettes die einzige Live-Quelle.

## 7. Bezug

- Single-Session-Constraint und Singular-Halter:
  [`02-architecture.md`](../02-architecture.md) Sektion 2.1 + 3.1.
- Status-Sektion mit Phase-Plan:
  [`02-architecture.md`](../02-architecture.md) Sektion 11.2
  ("Account-Identitaet-Wechsel").
- Security-Pfade fuer kompromittierten Host und WS-Recording-Felder:
  [`04-security.md`](../04-security.md) Sektion 6.4, 10.3, 12.2.
- Glossar-Eintrag: [`06-glossary.md`](../06-glossary.md), "Service-Konto
  vs. Privat-Konto" in Sektion 2.
- IBKR-Login-Pattern und 2FA-Push-Empirie: Auto-Memory
  `project_live_2fa_gnzsnz_pattern`, `project_ibkr_session_resume`,
  `feedback_ibkr_lockout_threshold`.
- Phase-2-Karte: `0ef946c8` (vollzogen 2026-06-08, v2.5.0).
- Phase-3-Karte: `07b244b1` (abgeschlossen, v2.5.1).
