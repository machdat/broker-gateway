# CLAUDE.md

Anweisungen für Claude Code in diesem Repository.

## Projekt-Identität

- **Name:** broker-gateway
- **Status:** v1.11.0 deployed auf cma-pi-1 (Port 4000). AP-01..AP-03 abgeschlossen, AP-04/AP-05 in Arbeit.
- **KanPrompt-Projekt-ID:** `a6a45428-ac37-48f5-b295-d3ff26f31711`
- **GitHub:** https://github.com/machdat/broker-gateway (public)
- **Lokal:** `C:\Users\christian.mangold\git\broker-gateway`
- **Session-Farbe:** yellow

## Lese-Pflicht für neue Sessions

Erste Anlaufstelle ist [`docs/02-architecture.md`](docs/02-architecture.md) — dort liegen Architektur-Prinzipien, Komponenten-Übersicht, IBKR-Adaptions-Schicht, Auth-/Streaming-/Logging-Modell, Test-Strategie und alle aktuell offenen Architektur-Fragen. Deploy-Workflow, Pfade, Restart-Disziplin und Rollback in [`docs/03-deployment.md`](docs/03-deployment.md). README.md verlinkt die übrige operationelle Doku (Login, Recordings, Runbooks); KanPrompt liefert die Karten und ihre Detail-Beschreibungen.

## Verbindliche Regeln

1. **KanPrompt-Skill zuerst laden.** Vor jedem `mcp__kanprompt__*`-Tool-Call: `mcp__kanprompt__get_skill(name="implementation")`. Globale Anweisung in `~/.claude/CLAUDE.md`.
2. **Karten-Lifecycle einhalten.** Jede Karte: `transition_card_status(in-progress)` → `add_log_entry(action: Start)` → Arbeit → `add_log_entry(action: Abschluss)` → `finalize_card`. Minimum zwei Log-Einträge.
3. **Version-Bump bei jeder funktionalen Änderung.** Patch oder Minor in `pyproject.toml` und im README-Footer. Dokumentations-only-Änderungen ebenfalls als Patch.
4. **Direkt auf `main` committen**, solange CI noch nicht eingerichtet ist (analog zur PSM-Übergangsregel). Sobald CI steht, auf Branch+PR umstellen.

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
