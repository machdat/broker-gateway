# CLAUDE.md

Anweisungen für Claude Code in diesem Repository.

## Projekt-Identität

- **Name:** broker-gateway
- **Status:** Repo-Stand v2.2.3. Auf cma-pi-1 läuft Service-Image v2.2.0 (v2.2.1–v2.2.3 sind reine Doku-Patches ohne Image-Rebuild). Paper-Stack (Port 4001, DUP799747) und Live-Stack (Port 4000, U25235077 als heute aktiver Live-Account — Cutover auf dediziertes Service-Konto in Vorbereitung, siehe Konto-Migrations-Plan) sind beide aktiv. TWS-Backend-Cutover (AP `2a203c58` Phase 1-7) abgeschlossen; historical/fundamentals-Endpoints (Karte `a5c7ff1c`) seit v2.2.0 live; Token-Store-Recreate-Runbook (Karte `0de305f0`) seit v2.2.1.
- **KanPrompt-Projekt-ID:** `a6a45428-ac37-48f5-b295-d3ff26f31711`
- **GitHub:** https://github.com/machdat/broker-gateway (public)
- **Lokal:** `C:\Users\christian.mangold\git\broker-gateway`
- **Session-Farbe:** yellow

## Konto-Migrations-Plan

Seit 2026-05-18 ist ein dediziertes Service-Konto für broker-gateway-Live bei IBKR beantragt. Ziel: Entkopplung von U25235077, das der Operator parallel im Browser/in der IBKR-App nutzt — jeder Operator-Login dort kollidiert mit der Service-Session (Single-Session-Constraint). Drei-Phasen-Plan:

| Phase | Karte | Stand |
|-------|-------|-------|
| 1 — Doku-Vorbereitung (Architektur, Security, Glossar, Runbook, CLAUDE.md auf "U25235077 = heute aktiv, Ersatz geplant") | `cdb262f7` | in dieser Karte umgesetzt (v2.2.2) |
| 2 — Cutover (Credentials-Tausch + Compose-Recreate + Smoke + Doku-Aktualisierung) | `0ef946c8` | blocked: wartet auf Account-ID + Login |
| 3 — Bereinigung U25235077 in Doku + Memory | `07b244b1` | blocked durch Phase 2 |

Detail-Pfad: [`docs/runbooks/account-cutover.md`](docs/runbooks/account-cutover.md). Status-Sektion: [`docs/02-architecture.md`](docs/02-architecture.md) Sektion 11.2.

## Lese-Pflicht für neue Sessions

Erste Anlaufstelle ist [`docs/02-architecture.md`](docs/02-architecture.md) — dort liegen Architektur-Prinzipien, Komponenten-Übersicht, IBKR-Adaptions-Schicht, Auth-/Streaming-/Logging-Modell, Test-Strategie und alle aktuell offenen Architektur-Fragen. Deploy-Workflow, Pfade, Restart-Disziplin und Rollback in [`docs/03-deployment.md`](docs/03-deployment.md). Security-Konventionen (Token, Scopes, Header-Redaktion, 2FA-Lifecycle, Vorfall-Reaktion) in [`docs/04-security.md`](docs/04-security.md). API-Konsumenten-Einstieg in [`docs/05-api.md`](docs/05-api.md), formale v1-Spec in [`docs/api/v1.md`](docs/api/v1.md) — Stand v1.34.1 (Drift-Bericht in Section 14, gegen Live-Paper-Stack DUP799747 kalibriert). Begriffsklärungen (IBKR-Vokabular und broker-gateway-Eigenvokabular) in [`docs/06-glossary.md`](docs/06-glossary.md). README.md verlinkt die übrige operationelle Doku (Login, Recordings, Runbooks); KanPrompt liefert die Karten und ihre Detail-Beschreibungen.

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
- **IBKR Live-Account Baseline U25235077** — Non-Pro AT, Kontoreifung.
- **IBKR-Streaming Fan-Out** — dedizierte clientId pro Stream-Holder + App-Level-Fan-Out.
- **PSM End-State Vision** — PSM als Portfolio-Kurator, Trading via Broker-Gateway (also dieses Projekt).

Die Auto-Memory dieses Projekts ist eigenständig — PSM-Memories werden nicht automatisch übernommen, aber sinngemäße Verweise sind in den Projekt-Instructions hinterlegt.
