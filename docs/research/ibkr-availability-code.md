# IBKR Availability-Code (Feld 6509) — vollständige Tabelle

Konsolidierter Stand der Recherche, Mai 2026 (AP-09). Bisher gab es
nur empirisches Teilwissen (DPB/RPB/F\*) — diese Datei zieht alle
verfügbaren Quellen zusammen, sodass `src/broker_gateway/availability.py`
und das Glossar eine belastbare SSOT haben.

## 1. Quellen

| Quelle | Pfad |
|--------|------|
| **Offizielle IBKR-CP-API-OpenAPI-Spec** (autoritativ) | [`docs/research/ibkr-cpapi-doc.json`](ibkr-cpapi-doc.json) — Schema-Block für Feld `6509` (z.B. unter `iserver/marketdata/snapshot`-Response). |
| **Auto-Memory-Notiz** (Empirie aus PSM-Bootstrap) | `~/.claude/projects/.../memory/IBKR Feld 6509 Availability-Code.md` (lokal) — frühe Beobachtung, dass DPB/RPB/F\* in echten Antworten vorkommen. |
| **Live-Recordings** | `tests/fixtures/recorded/live/**/*.json*` — 67 Vorkommen ueber drei distinct codes (DPB, ZB, RPB), Stand 2026-05-02. |
| **broker-gateway-Code** | [`src/broker_gateway/availability.py`](../../src/broker_gateway/availability.py) — Mapping auf `realtime` / `delayed` / `frozen`. |
| **K6-WS-Adapter-Design** | [`docs/architecture/ws-adapter-design.md`](../architecture/ws-adapter-design.md) Sektion 5.2, Anhang B — Wahrheits-Tabelle für `is_tradeable_now` / `current_session`. |

## 2. Schema laut IBKR-OpenAPI

Feld `6509` ist ein String mit **bis zu drei Zeichen**. Die Position
kodiert orthogonale Eigenschaften:

### 2.1 Erstes Zeichen — Datenklasse

| Char | Bedeutung | Adapter-Mapping (`availability.py`) |
|------|-----------|-------------------------------------|
| `R` | **RealTime** — Daten in Echtzeit, Subscription erforderlich | `realtime` |
| `D` | **Delayed** — 15-20 Minuten verzögert | `delayed` |
| `Z` | **Frozen** — letzter Wert bei Markt-Schluss, Echtzeit-Lieferung | `frozen` |
| `Y` | **Frozen Delayed** — letzter Wert bei Markt-Schluss, verzögerte Lieferung | `frozen` |
| `N` | **Not Subscribed** — keine Marktdaten-Subscription | (Adapter liefert `None`) |
| `H` | **Halted** (nicht in OpenAPI gelistet, aber in K6-WS-Adapter-Design 5.2 als Tradeability-Wert genannt) | (Adapter liefert `None`; WS-Adapter setzt `current_session=halted`) |
| `F` | **Legacy / Mock-Pragmatismus** — historisch in frühen Snapshots gesetzt; semantisch zu `Frozen` synonym | `frozen` |

### 2.2 Zweites Zeichen — Subscription-Form

| Char | Bedeutung |
|------|-----------|
| `P` | **Snapshot** — Snapshot-Request verfügbar (Single Pull, kein Stream-Bedarf für diese Information) |
| `p` (klein) | **Consolidated** — konsolidierte Datenquelle (laut OpenAPI; in der Praxis bisher nicht in unseren Recordings beobachtet) |

### 2.3 Drittes Zeichen — Datenform

| Char | Bedeutung |
|------|-----------|
| `B` | **Book** — Order-Book-Daten (Tiefe verfügbar) |

> **Wichtig:** Das dritte Zeichen ist nicht in jeder Antwort gesetzt.
> Live-Beobachtung 2026-05-02: `ZB` (zwei Zeichen, ohne Snapshot-Letter)
> tritt auf, wenn IBKR den Snapshot-Indikator weglässt — z.B. bei
> Frozen-Antworten ausserhalb der Trading-Hours.

## 3. Empirie aus Live-Recordings

Auswertung von `tests/fixtures/recorded/live/**/*.json*` (Stand
2026-05-02, AP-09 Recherche-Skript):

| Code | Vorkommen | Files | Bedeutung |
|------|-----------|-------|-----------|
| `DPB` | 62 | 6 | Delayed + Snapshot + Book — Standard-Antwort für Paper-/Delayed-Konten |
| `ZB` | 3 | 2 | Frozen + Book (kein Snapshot-Letter) — Markt geschlossen |
| `RPB` | 2 | 1 | RealTime + Snapshot + Book — Live-Konto in RTH |

Andere in der OpenAPI dokumentierte Werte (`Yp`, `N\*`, `Rp\*` etc.)
sind in unseren Recordings bisher nicht aufgetreten — was nicht
heisst, dass sie nicht vorkommen können.

## 4. Frozen-Semantik (Z\* / Y\*)

Aus IBKR-OpenAPI plus Empirie:

- **Wann tritt Z\* / Y\* auf?** Wenn der gefragte Markt ausserhalb
  seiner Trading-Hours ist. IBKR liefert dann den letzten Snapshot
  vor Schluss; Felder wie `last`, `bid`, `ask` reflektieren den
  Closing-State.
- **Z** vs. **Y**: Z = Echtzeit-Lieferung des eingefrorenen Werts
  (Subscription vorhanden); Y = verzögerte Lieferung des
  eingefrorenen Werts.
- **Trading-Halt**: in unseren Recordings nicht beobachtet, weil
  Halts selten und kurz sind. Der WS-Adapter (AP-11 K5) behandelt
  Z\*/Y\* unabhängig vom Schedule als `current_session=halted` und
  `is_tradeable_now=false`.
- **Welche Felder sind in Frozen-Antworten gefüllt?** Empirisch
  gleich wie in delayed/realtime — `last`, `bid`, `ask`, `volume`
  sind belegt; nur `change_pct` kann auf `0` springen, weil der
  letzte Tick stehengeblieben ist.

## 5. Mapping-Regel (broker-gateway-Adapter)

`src/broker_gateway/availability.py` mappt nur auf das **erste
Zeichen** und reduziert auf drei semantische Kategorien:

```python
_PREFIX_MAP = {
    "R": "realtime",
    "D": "delayed",
    "Z": "frozen",
    "Y": "frozen",
    "F": "frozen",  # Legacy
}
```

Das `Y` ist seit AP-02 #04 dabei (initial nur `R`/`D`/`Z`/`F`); `H`
(Halted) ist nicht im Public-API-Adapter, sondern erst im
WS-Adapter-Tradeability-Layer (AP-11 K5,
`src/broker_gateway/cp/tradeability.py`). `N` (Not Subscribed) wird
zu `None` gemappt — Konsumenten müssen das als Fehlerfall behandeln.

Sub-Code-Information (zweites/drittes Zeichen) wird **nicht**
weitergereicht — `Quote.availability` ist absichtlich grobkörnig. Wer
Snapshot-vs-Consolidated braucht, liest `Quote.availability_raw`
(Originaltext aus 6509).

## 6. Restunsicherheiten

| Frage | Stand |
|-------|-------|
| Tritt `p` (Consolidated) jemals in CP-API-Antworten auf? | unklar; in unseren Recordings nicht beobachtet. |
| Gibt es ein viertes Zeichen in 6509? | OpenAPI-Schema sagt 'kann drei Zeichen enthalten' — vier Zeichen sind nicht ausgeschlossen, aber empirisch unbekannt. |
| Mapping `H` (Halted) — kommt der jemals als 6509-Wert oder nur im WS-`smd`-Frame? | OpenAPI listet `H` nicht für 6509; WS-Adapter K5 behandelt es defensiv als Halted in der Wahrheits-Tabelle. |

## 7. Verhalten bei unbekannten Codes

`availability.py` liefert `None` für jeden Praefix, der nicht in
`_PREFIX_MAP` steht. Konsumenten **müssen** auf `None` defensiv
reagieren (typisch: als unbekannt loggen + Daten als nicht
vertrauenswürdig markieren). Der WS-Adapter (K5) interpretiert
unbekannte Codes als `current_session=closed` und
`is_tradeable_now=false` — das ist die sichere Wahl bei Halt-Verdacht.
