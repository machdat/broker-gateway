# ops/cpgateway

Build- und Konfigurations-Artefakte fuer den IBKR Client Portal Gateway,
der im Compose-Stack als Service `cpgateway` mitlaeuft.

## Inhalt

| Datei | Eingecheckt | Zweck |
|-------|-------------|-------|
| `README.md` | ja | Diese Datei |
| `conf.yaml` | ja | IBKR-CP-Gateway-Konfiguration (listenPort, listenSsl) — Single Source of Truth |
| `clientportal.gw.tar.gz.sha256` | ja | SHA256-Pruefsumme der getesteten IBKR-Tarball-Version |
| `logback-debug.xml` | ja | Optionale DEBUG-Logback-Variante (nur Diagnose, vgl. Karte 739777a9; siehe Datei-Header fuer Aktivierungs-Workflow per `docker cp`) |
| `clientportal.gw.tar.gz` | **nein** (.gitignore) | IBKR-Tarball, separat von IBKR bezogen |
| `clientportal.gw/` | **nein** (.gitignore) | Optional lokal entpackter Tarball, falls man ihn ausserhalb des Containers ansehen will |

## Tarball beziehen

Der IBKR Client Portal Gateway wird **nicht** im Repository versioniert.
Er muss vor dem ersten Container-Build manuell hierher gelegt werden:

1. Download bei IBKR: <https://www.interactivebrokers.com/en/trading/ibgateway-stable.php>
   Auswahl **Client Portal Gateway** (nicht TWS-Gateway). Liefert ein
   ZIP-Archiv `clientportal.gw.zip`.
2. ZIP entpacken und Inhalt in ein Tarball umpacken, sodass das
   Top-Level-Layout direkt `bin/`, `dist/`, `root/` enthaelt
   (kein zusaetzliches Wrapper-Verzeichnis):
   ```bash
   unzip clientportal.gw.zip -d clientportal.gw
   tar -czf clientportal.gw.tar.gz -C clientportal.gw .
   ```
3. Pruefsumme erzeugen und nach `clientportal.gw.tar.gz.sha256` schreiben:
   ```bash
   sha256sum clientportal.gw.tar.gz | tee clientportal.gw.tar.gz.sha256
   ```
4. Eintrag in `clientportal.gw.tar.gz.sha256` committen, Tarball selbst
   bleibt lokal — `.gitignore` sorgt dafuer, dass er nicht versehentlich
   eingecheckt wird (`git status` zeigt ihn nicht).

## Pruefsumme verifizieren

Vor jedem Container-Build:
```bash
sha256sum -c clientportal.gw.tar.gz.sha256
```

Schlaegt das fehl, ist entweder das Tarball korrumpiert oder eine
abweichende IBKR-Version im Spiel. In dem Fall **erst** dokumentieren,
welche Version installiert wird, **dann** Pruefsumme aktualisieren — nie
ohne Versionsnotiz blind ueberschreiben.

## Login-Flow

Der Container startet, danach erfolgt der Browser-Login mit 2FA. Das
detaillierte Runbook liegt in `docs/runbooks/cpgateway-login.md`.
