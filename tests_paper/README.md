# tests_paper/

Paper-Account-Tests gegen den deployed `broker-gateway-paper`-Stack
(siehe [`docs/runbooks/paper-account-setup.md`](../docs/runbooks/paper-account-setup.md)).

> **Wichtig:** Diese Tests werden im Default-`pytest`-Lauf
> **ausgeschlossen**. Sie brauchen einen aktiven Paper-Stack mit
> gueltigem `DU`-Konto-Login, sonst schlagen sie zwangslaeufig fehl.

## Aggressivitaets-Stufen

Vier Marker, von harmlos zu destruktiv:

| Marker | Verzeichnis | Was passiert |
|--------|-------------|--------------|
| `paper_readonly` | `L1_readonly/` | Nur Lese-Operationen (`/v1/health`, `/v1/instruments/...`, `/v1/quotes/snapshot`, `/v1/portfolio/...`, `/v1/trades`). Keine Schreib-Operation. |
| `paper_safe_write` | `L2_safe_write/` | Sichere Schreib-Operationen ohne Order-Lifecycle (z.B. Token-Erstellung in der Paper-Instanz). |
| `paper_pic` | `L3_pic/` | "Place + Immediate Cancel": eine Limit-Order weit unter Markt platzieren und sofort canceln. Keine Position. |
| `paper_destructive` | `L4_destructive/` | Erzeugt und schliesst Positionen (Buy + Sell). Nur mit ausdruecklicher Bestaetigung im Aufruf. |

## Aufruf

```bash
# Voraussetzung: BG_PAPER_BASE_URL plus BG_PAPER_BOOTSTRAP_TOKEN
# gesetzt; Paper-Stack ist eingeloggt (siehe paper_session_check.py).

pytest tests_paper -m paper_readonly       # L1
pytest tests_paper -m paper_safe_write     # L2
pytest tests_paper -m paper_pic            # L3 (place + cancel)
pytest tests_paper -m paper_destructive    # L4 (Position auf/zu)
```

Im Repo-Default werden die vier Marker per
`-m "not paper_readonly and not paper_safe_write and not paper_pic
and not paper_destructive"` aus dem `pyproject.toml`-`addopts`
ausgeschlossen. So laeuft `pytest` im Repo-Root weiterhin nur die
in-process-Mock-Suite unter `tests/`.

## Konvention

- Pro Stufe ein eigenes Verzeichnis mit eigenem `conftest.py`-Setup
  (Pre-Flight, Token, DU-Whitelist).
- Tests in der Stufe **dokumentieren** den Marker im
  `pytestmark = pytest.mark.paper_<level>` am Modul-Anfang.
- Keine Stufe darf Tests aus einer hoeheren Stufe enthalten - L1 ist
  garantiert read-only.
