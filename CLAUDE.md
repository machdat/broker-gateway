# CLAUDE.md

Anweisungen für Claude Code in diesem Repository.

## Projekt-Identität

- **Name:** broker-gateway
- **Status:** Repo-Stand v2.9.0 (tws-Watchdog, Karte `53c10ff4`). Auf cma-pi-1 läuft Service-Image v2.8.3 (quotes-Fix; der v2.8.4-Doku-Patch und das v2.9.0-Ops-Tooling laufen ohne neues Service-Image). Paper-Stack (Port 4001, dediziertes IBKR-Paper-Konto) und Live-Stack (Port 4000, am dedizierten IBKR-Service-Konto — siehe Konto-Migrations-Plan) sind beide aktiv; Konto-IDs liegen ausschließlich in der Pi-`.env`/`.env.paper` und im Passwort-Manager, bewusst nicht im öffentlichen Repo. Meilensteine: TWS-Backend-Cutover (AP `2a203c58` Phase 1-7) abgeschlossen; historical/fundamentals-Endpoints (Karte `a5c7ff1c`) seit v2.2.0 live; Token-Store-Recreate-Runbook (Karte `0de305f0`) seit v2.2.1; Order-Status-Scope-Fix (Karte `baba6beb`) seit v2.2.4; What-If-Preview `POST /v1/orders/whatif` (Karte `fe164f56`) seit v2.3.0 — **erfordert eine write-fähige Session** (IBKR Warning 321), liefert im read-only-Modus beider Stacks `503 whatif_requires_write_session` (Hang-Fix v2.3.1); whatToShow/ADJUSTED_LAST für Historical-Bars (Karte `6c1da48e`) seit v2.4.0; Live-Account-Cutover auf das dedizierte Service-Konto (Karte `0ef946c8`) seit v2.5.0. **AP-14** (trading_robot-KW29-Vorbereitung): Contract-Trading-Hours im Instrument (Karte `f1a01d97`, v2.6.0), `GET /v1/orders` Offene-Orders-Liste + `oca_group` (Karte `def3e8f5`, v2.7.0), `PATCH /v1/orders/{id}` Order-Modify + GTC-STP-Schreib-Lifecycle (Karte `35ac9a17`, v2.8.0) inkl. gemeinsamem gateway/tws-write-Schalter `BG_TWS_READ_ONLY` + Hard-Guard 5 gegen Live-write (v2.8.1). quotes/snapshot im TWS-Backend repariert — `reqTickersAsync` statt des nicht-existenten `reqMktDataAsync`, plus Volume-Skalierung /1e6 analog cp (Karte `2ac8c839`, v2.8.2/v2.8.3). **tws-Watchdog** (systemd-Timer alle 15 min, ntfy-Push-Alarm bei dauerhaftem `tws_down` + Paper-Auto-Recovery, Live nur Alarm wegen 2FA; Karte `53c10ff4`, v2.9.0) — siehe [`docs/runbooks/tws-recovery.md`](docs/runbooks/tws-recovery.md).
- **KanPrompt-Projekt-ID:** `a6a45428-ac37-48f5-b295-d3ff26f31711`
- **GitHub:** https://github.com/machdat/broker-gateway (public)
- **Lokal:** `C:\Users\christian.mangold\git\broker-gateway`
- **Session-Farbe:** yellow

## Konto-Migrations-Plan

**Abgeschlossen.** Am 2026-06-08 wurde broker-gateway-Live per Cutover (Phase 2) vom Privatkonto U25235077 auf ein dediziertes IBKR-Service-Konto umgestellt. Damit ist U25235077 entkoppelt: der Operator kann es parallel im Browser/in der IBKR-App nutzen, ohne die Service-Session zu verdrängen (Single-Session-Constraint). Account-ID und Credentials liegen ausschließlich in der Pi-`.env` und im Passwort-Manager — bewusst nicht im öffentlichen Repo. Drei-Phasen-Plan:

| Phase | Karte | Stand |
|-------|-------|-------|
| 1 — Doku-Vorbereitung (Architektur, Security, Glossar, Runbook) | `cdb262f7` | done (v2.2.2) |
| 2 — Cutover (Credentials-Tausch + Compose-Recreate + Smoke + Doku-Aktualisierung) | `0ef946c8` | done, vollzogen am 2026-06-08 (v2.5.0) |
| 3 — Bereinigung U25235077 in Doku + Memory | `07b244b1` | done (v2.5.1) |

Detail-Pfad: [`docs/runbooks/account-cutover.md`](docs/runbooks/account-cutover.md). Status-Sektion: [`docs/02-architecture.md`](docs/02-architecture.md) Sektion 11.2 (geklärt).

## Lese-Pflicht für neue Sessions

Erste Anlaufstelle ist [`docs/02-architecture.md`](docs/02-architecture.md) — dort liegen Architektur-Prinzipien, Komponenten-Übersicht, IBKR-Adaptions-Schicht, Auth-/Streaming-/Logging-Modell, Test-Strategie und alle aktuell offenen Architektur-Fragen. Deploy-Workflow, Pfade, Restart-Disziplin und Rollback in [`docs/03-deployment.md`](docs/03-deployment.md). Security-Konventionen (Token, Scopes, Header-Redaktion, 2FA-Lifecycle, Vorfall-Reaktion) in [`docs/04-security.md`](docs/04-security.md). API-Konsumenten-Einstieg in [`docs/05-api.md`](docs/05-api.md), formale v1-Spec in [`docs/api/v1.md`](docs/api/v1.md) — Stand v1.39.0 (Drift-Bericht in Section 14, gegen den Paper-Stack kalibriert; `POST /v1/orders/whatif` seit v1.35.0/Service-v2.3.0; `GET`/`PATCH /v1/orders` seit AP-14/Service-v2.6.0–v2.8.0). Begriffsklärungen (IBKR-Vokabular und broker-gateway-Eigenvokabular) in [`docs/06-glossary.md`](docs/06-glossary.md). README.md verlinkt die übrige operationelle Doku (Login, Recordings, Runbooks); KanPrompt liefert die Karten und ihre Detail-Beschreibungen.

## Verbindliche Regeln

1. **KanPrompt-Skill zuerst laden.** Vor jedem `mcp__kanprompt__*`-Tool-Call: `mcp__kanprompt__get_skill(name="implementation")`. Globale Anweisung in `~/.claude/CLAUDE.md`.
2. **Karten-Lifecycle einhalten.** Jede Karte: `transition_card_status(in-progress)` → `add_log_entry(action: Start)` → Arbeit → `add_log_entry(action: Abschluss)` → `finalize_card`. Minimum zwei Log-Einträge.
3. **Version-Bump bei jeder funktionalen Änderung.** Patch oder Minor in `pyproject.toml` und im README-Footer. Dokumentations-only-Änderungen ebenfalls als Patch.
4. **Branch + PR + grüne CI vor Merge.** Seit AP-13 K1 läuft `.github/workflows/ci.yml` auf jedem push/PR (pytest-Matrix Python 3.12 + 3.13). Direkt-auf-`main`-Commits sind nur noch für Hotfixes erlaubt, in denen das Risiko eines fehlgeschlagenen Workflows kleiner ist als die Verzögerung; im Zweifel über Branch + PR.

## Scope (kompakt)

`broker-gateway` ist ein Singular-Service, der die IBKR-Trading-Session als gemultiplexte HTTP-API ausliefert. Consumer (PSM, trading-robot) sehen IBKR nicht. API ist versioniert (`/v1`).

Vollständige Projekt-Beschreibung: `docs/02-architecture.md`, README.md und KanPrompt-Projekt-Instructions (`mcp__kanprompt__get_project`).

## Memory-Bezug zu Schwester-Projekten

Persistente Erkenntnisse aus PSM-Sessions, die hier relevant bleiben:

- **IBKR Feld 6509 Availability-Code** (DPB/RPB) — Realtime vs Delayed.
- **IBKR Live-Account Baseline U25235077** — Non-Pro AT, Kontoreifung (historische Baseline; seit Cutover 2026-06-08 Operator-Privatkonto, Live läuft am Service-Konto).
- **IBKR-Streaming Fan-Out** — dedizierte clientId pro Stream-Holder + App-Level-Fan-Out.
- **PSM End-State Vision** — PSM als Portfolio-Kurator, Trading via Broker-Gateway (also dieses Projekt).

Die Auto-Memory dieses Projekts ist eigenständig — PSM-Memories werden nicht automatisch übernommen, aber sinngemäße Verweise sind in den Projekt-Instructions hinterlegt.
