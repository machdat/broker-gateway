# Runbook: Doku-Drift-Check (AP-03)

Frueh-Warner-Mechanismus: vergleicht die aktuelle IBKR-OpenAPI-Spec
(via HTTP gefetcht) gegen die eingecheckte Baseline
`docs/research/ibkr-cpapi-doc.json` und produziert einen Bericht unter
`reports/doc-drift/<YYYY-MM-DD>.md`.

Komplementaer zum
[Mock-Drift-Check](mock-drift-check.md): der Doku-Drift erkennt
**angekuendigte** Schema-Aenderungen (IBKR pflegt die Spec oft Tage vor
dem Live-Rollout) **ohne** Auth-Aufwand. Der Mock-Drift hingegen
erkennt **undokumentierte** Aenderungen am tatsaechlichen Live-Verhalten.

## Wann laeuft das Skript

- **Taeglich um 06:00 Europe/Berlin** auf cma-pi-1 als systemd-timer
  (`doc-drift.timer`). Installations-Anleitung:
  [ops/systemd/README.md](../../ops/systemd/README.md).
- **Manuell** zur Verifikation oder bei Hinweisen auf Schema-Aenderungen.

## Lauf

```bash
python scripts/check_doc_drift.py
```

Defaults:

- Quelle: `https://www.interactivebrokers.com/api/doc.json`
- Baseline: `docs/research/ibkr-cpapi-doc.json`
- Bericht: `reports/doc-drift/<heute>.md`
- Auto-Karten-Anlage: deaktiviert (mit `--auto-card` einschalten)

Exit-Code:

| Code | Bedeutung |
|------|-----------|
| 0 | kein Drift oder nur `value`-Drift (Doku-Texte) |
| 1 | **breaking drift** - Pfad/Operation/Status-Code/Response-Feld entfernt, Typ geaendert, neues required Request-Feld |
| 2 | **minor drift (additive)** - neuer Pfad/Operation, neues optionales Feld, neuer Enum-Wert |
| 3 | Quell-URL nicht erreichbar (kein Internet, IBKR down, falsche URL) |

Mit `--source-url <url>` laesst sich die Quelle ueberschreiben (z.B.
fuer einen Mirror). Mit `--baseline <pfad>` die Vergleichsdatei.

## Bericht lesen

Jeder Bericht beginnt mit einer Zusammenfassung:

```
- breaking: 0 - minor (additive): 1 - value: 3

**Klassifikation:** minor (additive)
```

Danach pro Klassifikation ein Abschnitt mit Findings im Format
`<pfad>:<operation>.<schema-pfad> (<kind>)`:

```markdown
## Minor (additive)
- `/iserver/accounts:get.responses.200.schema.properties.selectedAccount` (added_field)
```

## Reaktion pro Klassifikation

| Klassifikation | Was tun |
|----------------|---------|
| **no drift** | Nichts. Baseline ist aktuell. |
| **value (irrelevant)** | Nichts - nur Doku-Texte geaendert. Optional bei Gelegenheit Baseline auffrischen. |
| **minor (additive)** | Karte wird per `--auto-card` angelegt. Pruefen, ob das neue Feld/Pfad im Service-Code genutzt werden soll. Baseline mit `curl ...` neu fetchen und committen. |
| **breaking** | Sofortige Eskalation: Karte hat `blocked=true`. Konsumenten (PSM, trading-robot) informieren. Folge-Karte fuer Schema-Migration. **Erst dann** Baseline aktualisieren. |

## Auto-Karten-Anlage

Mit `--auto-card` legt das Skript bei `exit 1` oder `exit 2` automatisch
eine KanPrompt-Karte im broker-gateway-Projekt an:

- Title-Praefix: `Doku-Drift breaking <YYYY-MM-DD>` bzw.
  `Doku-Drift minor <YYYY-MM-DD>`.
- Card-Type: `bugfix` (breaking) bzw. `feature` (minor).
- `blocked=true` bei breaking, `false` bei minor.
- `priority=0` bei breaking, `1` bei minor.

**Spam-Schutz:** maximal eine Karte pro Tag pro Drift-Klasse. Vor der
Anlage prueft das Skript via `GET /api/v1/projects/<pid>/cards`, ob
heute schon eine Karte mit dem gleichen Title-Praefix existiert. Wenn
ja, wird die zweite Anlage uebersprungen (Log: `[skip] Heute existiert
bereits eine Karte ...`).

**Voraussetzung:** Env-Variable `KANPROMPT_API_KEY`. Auf cma-pi-1 liegt
der Key in `/etc/default/doc-drift` (siehe systemd-Unit). Der Schluessel
wird **nicht** ins Repo committet.

## Baseline aktualisieren

Der Drift-Check **schreibt niemals** die Baseline. Aenderungen sind eine
bewusste Entscheidung:

```bash
curl -A "Mozilla/5.0" -o docs/research/ibkr-cpapi-doc.json \
    https://www.interactivebrokers.com/api/doc.json
git diff docs/research/ibkr-cpapi-doc.json | less
git add docs/research/ibkr-cpapi-doc.json
git commit -m "docs: IBKR CP-API Spec auf <datum> aktualisiert"
```

**Wichtig:** Der naechste Doku-Drift-Lauf nach dem Commit ist im Idealfall
"no drift". Sollte er es nicht sein, hat IBKR die Live-Spec zwischen
Fetch und Commit weiter geaendert (selten) - dann den Lauf nochmal,
wieder fetchen, neu committen.

## Drift-Strategie auf einen Blick

```
+--------------+     +-------------------+    +------------------+
|  Doku-Drift  | -->  |  Live-Drift  | -->  |  Konsumenten OK  |
|  taeglich    |     |  bei Build       |    |  (PSM, robot)    |
|  06:00       |     |  Acceptance      |    +------------------+
+--------------+     +-------------------+
| Karte bei    |     | Build bricht    |
| Drift        |     | bei Drift       |
+--------------+     +-------------------+
   FRUEH                        SPAET
```

Mehr Hintergrund: [docs/cp-recordings.md, Section "Drift-Strategie"](../cp-recordings.md).

## Troubleshooting

| Symptom | Pruefen |
|---------|---------|
| Exit 3 + "Live-Spec nicht ladbar" | DNS / Proxy / IBKR-Erreichbarkeit. Spec liegt unter docs.json - User-Agent gesetzt? |
| `--auto-card` legt keine Karte an, obwohl Drift | KANPROMPT_API_KEY gesetzt? KanPrompt-API erreichbar? Spam-Schutz: existiert heute schon eine Karte? |
| Karte legt sich an, blockiert sich aber nicht beim breaking | Pruefe `blocked`-Feld im API-Response - manche KanPrompt-Versionen brauchen separat `set_card_blocked`. |
| Bericht zeigt sehr viele `value (irrelevant)`-Findings | description/summary werden in der IBKR-Doku oft refresht - Klassifikation bleibt korrekt, Exit 0. Optional Baseline neu fetchen. |
| systemd-Service haengt im "activating" | siehe `journalctl -u doc-drift.service`. EnvironmentFile vorhanden? venv-Python ausfuehrbar? |
