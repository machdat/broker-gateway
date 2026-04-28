# Runbook: Mock-Drift-Check (AP-02 #06)

Vergleicht Live-CP-Gateway-Antworten gegen die unter
`tests/fixtures/recorded/live/` eingecheckten Fixtures und produziert
einen Markdown-Bericht unter `reports/drift/<YYYY-MM-DD>.md`. Die
Berichte sind eingecheckt und bilden den Drift-Verlauf des Repos ab.

## Wann laufen wir das

- **Bei jedem Container-Rebuild** als Build-Acceptance-Test (siehe
  Section [Build-Acceptance-Modus](#build-acceptance-modus) unten).
  Das ist die Hauptverwendung seit AP-03.
- **Manuell ad-hoc**, wenn der Mock mit `tests/test_*` gruen ist, in
  Integration-Sandboxes aber unerklaerliche Schemafehler auftauchen.
- **Sofort** wenn ein Doku-Drift-Lauf eine Aenderung meldet, die wir
  gegen den Live-Endpunkt verifizieren wollen.

Routine-mae Wochenlaeufe sind **nicht** mehr noetig - die Build-
Acceptance fuengt Drift im Moment des Container-Rebuilds. Die
**taegliche** Frueh-Warnung uebernimmt der
[Doku-Drift-Check](doc-drift-check.md).

Das Skript ist explizit **nicht** Teil der Standard-Test-Suite -
ein normaler `pytest`-Lauf zieht keine Live-Verbindung zum CP-Gateway.

## Voraussetzungen

Identisch zum
[Happy-Path-Recording-Runbook](recording-session-happy-path.md):

- `cpgateway`-Container laeuft (Compose-Stack auf cma-pi-1).
- SSH-Reverse-Tunnel: `ssh -L 5000:localhost:5000 cma@cma-pi-1` plus
  socat-Helper auf der Pi (siehe Happy-Path-Runbook Schritt 2).
- Browser-2FA-Login frisch gemacht: `curl
  http://localhost:5000/v1/api/iserver/auth/status | jq` muss
  `authenticated: true` zeigen.
- Lokales venv aktiviert mit dev-Dependencies.

## Lauf

```bash
python scripts/check_mock_drift.py \
    --base-url http://localhost:5000/v1/api
```

Defaults: liest `tests/fixtures/recorded/live/`, schreibt
`reports/drift/<heute>.md`. Mit `--date 2026-04-25` laesst sich das
Datum festlegen (z.B. fuer Reproduktions-Laeufe).

Exit-Code:

| Code | Bedeutung |
|------|-----------|
| 0 | kein breaking drift - Fixtures sind tragfaehig |
| 1 | mindestens ein breaking drift - sofort eskalieren |
| 2 | I/O-Fehler / keine Recordings vorhanden |
| 3 | `/iserver/auth/status` nicht authentifiziert - Login fehlt |

## Bericht lesen

Jeder Bericht beginnt mit einer Zusammenfassung:

```
- no drift: 14 - minor drift (additive): 1 - value drift: 0 - **breaking drift: 0** - uebersprungen: 4
```

Danach pro Endpunkt eine Sektion:

```
### GET /iserver/accounts

**Klassifikation:** minor drift (additive)

**Hinzugekommen (additive):**
- `selectedAccount`
```

### Reaktion pro Klassifikation

| Klassifikation | Was tun |
|----------------|---------|
| **no drift** | Nichts. Fixture ist aktuell. |
| **minor drift (additive)** | Karte anlegen 'Schema in `<Endpunkt>` erweitert'. Additive API-Felder sind erlaubt; entscheiden, ob das neue Feld im Service-Code genutzt werden soll. Fixture mit `recording_session.py refresh` aktualisieren. |
| **value drift** | Sichtkontrolle. Wenn der Wert sich erwartbar aendert (z.B. IBKR-Server-Tags), Fixture refreshen. Wenn der Wert ein Schema-Bestandteil ist (z.B. ein Code), Karte aufmachen. |
| **breaking drift** | Sofortige Eskalation: Karte mit `blocked=true` anlegen, Block-Grund klar formulieren. Konsumenten (PSM, trading-robot) koennten betroffen sein. Erst nach Eskalations-Entscheidung Fixture refreshen. |

## Refresh einer einzelnen Fixture

Wenn der Bericht zeigt, dass die Aenderung erwartet ist:

```bash
python scripts/recording_session.py refresh \
    tests/fixtures/recorded/live/iserver_accounts__GET__noquery_01.json
```

Das Skript ruft den im Fixture gespeicherten Request erneut ab, zeigt
einen Diff vorher (gleiche Logik wie `check_mock_drift.py`) und ersetzt
die Datei nach expliziter Bestaetigung. Im CI-Modus mit `--yes`.

**WICHTIG:** Das Drift-Skript schreibt **nie** Fixture-Files. Aenderungen
am Fixture-Bestand passieren ausschliesslich ueber `refresh`.

## Was wird uebersprungen

| Endpunkt-Muster | Grund |
|-----------------|-------|
| `/orders/whatif`, `/orders/`, `/order/` | Side Effects + dokumentarisch |
| `/logout`, `/reauthenticate` | zerstoert die Session |
| HTTP-Methoden `POST/PUT/DELETE/PATCH` (Ausnahme: `/tickle`) | schreibend |
| 4xx/5xx-Recordings (z.B. aus `errors/`) | dokumentarisch, nicht drift-relevant |

Skript-Logik: `_should_skip()` in `scripts/check_mock_drift.py`. Falls
eine Erweiterung noetig wird, dort eintragen und Test in
`tests/test_check_mock_drift.py::test_skip_logic_blocks_orders_and_logout`
mitziehen.

## Wo der Bericht abgelegt wird

```
reports/drift/2026-04-25.md   <- erster Bericht (AP-02 #06)
reports/drift/2026-05-02.md   <- naechster woechentlicher Lauf
...
```

Reports sind eingecheckt - `.gitignore` filtert `reports/` nicht.
Damit ist der Drift-Verlauf des Repos nachvollziehbar.

## Build-Acceptance-Modus

Seit AP-03 ist der Mock-Drift-Check **Pflicht-Teil** des Container-Builds.
`ops/build-gateway.sh` ruft das Skript zwischen `docker compose build`
und `docker compose up -d` auf - wenn der Drift breaking oder value ist,
bricht der Build ab und der neue Container wird nicht gestartet.

```bash
./ops/build-gateway.sh
# 1/3 docker compose build gateway
# 2/3 check_mock_drift --build-acceptance (commit abc123)
# 3/3 docker compose up -d gateway
```

Aufruf direkt:

```bash
GIT_COMMIT="$(git rev-parse HEAD)" \
python scripts/check_mock_drift.py --base-url http://localhost:5000/v1/api \
    --build-acceptance
```

Was der Modus aendert:

- **90s Warmup** vor dem ersten Replay. IBKR braucht nach Container-Start
  ungefaehr diese Zeit, bis Marktdaten/Portfolio-Endpunkte stabil
  antworten (Quelle: `project_ibkr_session_resume`-Memory). Skip mit
  `--warmup-seconds 0` nur fuer Tests.
- **Strenger Exit-Code:** schon ein einziger `value drift` (nicht-
  Timestamp-Feld) bricht den Build ab. Im manuellen Modus toleriert
  das Skript value drift. Begruendung: ein Container-Rebuild ist genau
  der Moment, wo ein veralteter Mock in Produktion gehen koennte -
  also lieber falsch-positiv als zu spaet.
- **Bericht-Pfad:** `reports/drift/build-<commit-sha>.md` statt
  Datums-Datei. So lassen sich Drift-Befunde einem konkreten Commit
  zuordnen.

**Voraussetzung:** Browser-Login + Reauth muessen vorab laufen. Das
Skript versucht **keinen** automatischen Login - Exit 3 bricht den
Build mit klarer Fehlermeldung ab.

**Notfall-Bypass** (sollte selten/nie gebraucht werden):

```bash
SKIP_ACCEPTANCE=1 ./ops/build-gateway.sh
```

Dieser Pfad existiert fuer Doku-only-Releases oder andere
Aenderungen, bei denen die IBKR-Session nicht erreichbar ist (kalte
Testumgebung, Wartungsfenster). Begruendung sollte im Commit stehen.

## Troubleshooting

| Symptom | Pruefen |
|---------|---------|
| Exit 3 mit "Browser-Login fehlt" | `curl http://localhost:5000/v1/api/iserver/auth/status` -> `authenticated: false`? Login durchlaufen, dann Skript erneut. |
| Build bricht in `[2/3] check_mock_drift --build-acceptance` ab | Bericht unter `reports/drift/build-<sha>.md` ansehen. Wenn der Drift gewollt ist (z.B. neues Schema gerade ausgerollt): `recording_session.py refresh` fuer betroffene Fixtures, Build erneut. |
| Skript haengt | SSH-Tunnel pruefen (`channel 2: open failed: connect failed`?). socat-Helper auf der Pi laeuft? Container `broker-cpgateway` healthy? |
| Sehr viele "value drift" auf Header-Werten | Header werden gar nicht geprueft - Diff arbeitet auf `body_json`. Falls eine Drift-Klasse unerwartet erscheint, in `tests/cp_mock/diff.py::DEFAULT_IGNORE_FIELDS` schauen. |
| Recording mit 4xx/5xx wird uebersprungen, obwohl gewuenscht | `/errors/`-Recordings sind dokumentarisch und werden absichtlich ignoriert. Wenn ein Endpunkt von 4xx/5xx auf 200 wechselt, manuell mit `refresh` neu aufzeichnen. |
