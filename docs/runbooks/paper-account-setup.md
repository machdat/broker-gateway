# Runbook: Paper-Account-Setup (broker-gateway-paper)

Setup-Anleitung fuer einen zweiten broker-gateway-Stack gegen einen
IBKR-Paper-Account, der parallel zum Live-Stack betrieben wird. Dient
als Test-Ziel fuer AP-06 (Paper-Account-Test-Stack), AP-07
(Paper-Test-Harness) und AP-08 (L1 Paper-Suite).

## Deploy-Target

**Paper-Stack laeuft auf cma-pi-1**; broker-gateway-paper extern
**Port 4001**, cpgateway-paper intern **Port 5001** (nicht published),
**Volume-Pfad `/mnt/ssd/broker-gateway-paper/var/cpgateway-paper/`**
(SSD analog zum Live-Stack `/mnt/ssd/broker-gateway/`).

### Optionen und Abwaegung

| Option | Vorteil | Nachteil |
|--------|---------|----------|
| **cma-pi-1 (gewaehlt)** | Geringster Setup-Overhead, gleicher Host wie Live-Stack, keine zusaetzliche Hardware. SSH/Tunnel-Workflow identisch zum Live-Stack. | Ressourcen- und Port-Konkurrenz mit Live-Stack; ein Host-Reboot trifft beide Instanzen. |
| Separater Host (zweiter Pi / VM) | Vollstaendige Entkopplung, Live bleibt unangetastet bei Paper-Reboots. | Zusaetzliche Hardware/Setup-Kosten, separater SSH-Tunnel-Workflow. |

**Begruendung Default**: solange der Paper-Stack ausschliesslich Test-
und CI-Last erzeugt (kein Live-Trading), ist die Ressourcenkonkurrenz
auf cma-pi-1 vernachlaessigbar. Die Port-Disjunktion (4000/5000 vs.
4001/5001) und die getrennten Volumes (`var/cpgateway/` vs.
`var/cpgateway-paper/`) sorgen fuer saubere Compose-Trennung. Bei
Last- oder Stabilitaetsproblemen kann auf einen zweiten Host migriert
werden, ohne dass sich die Paper-Karten in AP-06/07/08 aendern.

### Port-Beleg (Stand 2026-05-02)

`ss -tlnp` auf cma-pi-1 zeigt nur den Live-Gateway auf Port 4000:

```
$ ssh cma@cma-pi-1 "ss -tlnp 2>/dev/null | grep -E ':(4001|5001|4000) '"
LISTEN 0      4096                       0.0.0.0:4000       0.0.0.0:*
LISTEN 0      4096                          [::]:4000          [::]:*
```

Die geplanten Paper-Ports **4001** und **5001** sind frei, kein
Listener-Konflikt.

### Konvention

| Ressource | Live | Paper |
|-----------|------|-------|
| Compose-Project-Name | `broker-gateway` | `broker-gateway-paper` |
| broker-gateway extern | `4000` | `4001` |
| cpgateway intern | `5000` | `5001` (nicht published) |
| Volume-Pfad | `/mnt/ssd/broker-gateway/` | `/mnt/ssd/broker-gateway-paper/` |
| CP-Gateway-Logs | `var/cpgateway/logs/` | `var/cpgateway-paper/logs/` |
| .env-Datei | `.env` | `.env.paper` |
| Image-Tag | `broker-gateway:<version>` | `broker-gateway-paper:<version>` |

Die konkrete Compose-Datei und das Build-Skript-Schalter
(`ops/build-gateway.sh --env=paper|live`) folgen in AP-06 K2.

## Initial-Login Paper-Account (AP-06 K4)

Der Paper-Account braucht beim Erst-Start denselben Browser-2FA-
Schritt wie der Live-Account; siehe ausfuehrliche Anleitung in
[`cpgateway-login.md`](cpgateway-login.md). Hier nur die paper-
spezifischen Abweichungen.

### Voraussetzungen

- `.env.paper` ist aus `.env.paper.template` befuellt (siehe AP-06 K3),
  insbesondere `BG_BOOTSTRAP_ADMIN_TOKEN` (eigener Token, nicht den
  Live-Wert wiederverwenden) und `BG_PAPER_ACCOUNT_ID` (DU-Praefix-
  Konto-Nummer).
- Compose-Stack ist mit `./ops/build-gateway.sh --env=paper` einmal
  durchgelaufen, sodass der `broker-gateway-paper-cpgateway`-Container
  Healthy ist (HTTP 401 vor Login = ok).
- Paper-Login-URL: `https://<region>.interactivebrokers.com/portal`
  bzw. die regional passende Paper-Domain. **Wichtig:** Der Username
  beginnt mit `DU` (Demo-User), das Passwort kann sich vom Live-Account
  unterscheiden.

### Schritte

1. **SSH-Tunnel auf cma-pi-1:5001** (statt 5000 wie beim Live-Stack):

   ```bash
   ssh -N -L 5001:localhost:5001 cma@cma-pi-1
   ```

   Voraussetzung: in der `compose.yaml` ist der Paper-cpgateway nicht
   per `ports:` exponiert. Fuer den Login einmalig ein
   `compose.login-override.yaml` mit `127.0.0.1:5001:5000` einsetzen
   (analog Live-Variante A) oder den `socat`-Wegwerf-Container nutzen.

2. **Browser-Login** im Browser an `http://localhost:5001`. Username
   `DU<...>` plus Passwort plus 2FA. Nach `Client login succeeds`:

   ```bash
   curl -s http://localhost:5001/v1/api/iserver/auth/status | jq .
   ```

   Erwartung: `authenticated: true`, `competing: false`, `connected:
   true`.

3. **broker-gateway-paper starten**:

   ```bash
   ssh cma@cma-pi-1 'cd /mnt/ssd/broker-gateway-paper && \
       ./ops/build-gateway.sh --env=paper'
   ```

4. **Smoke-Validierung** mit dem `paper_session_check.py`-Skript
   (siehe unten):

   ```bash
   BG_PAPER_BASE_URL=http://cma-pi-1:4001 \
   BG_PAPER_BOOTSTRAP_TOKEN="$(grep -E '^BG_BOOTSTRAP_ADMIN_TOKEN=' \
       /mnt/ssd/broker-gateway-paper/.env.paper | cut -d= -f2)" \
   python3 scripts/paper_session_check.py
   ```

   Exit-Code `0` heisst: alle drei Probes (`/v1/health`,
   `/v1/internal/health`, `/v1/instruments/search?symbol=AAPL`) sind
   200, und `account_id` hat den DU-Praefix. Exit-Code `1` druckt
   eine Diagnose pro fehlgeschlagene Probe.

### Wann der Login wiederholt werden muss

Identisch zum Live-Stack (siehe `cpgateway-login.md`), nur dass der
Trigger der Paper-cpgateway-Container ist. Faustregel: nach jedem
`docker compose --env-file .env.paper restart cpgateway` oder Reboot
des Hosts ist ein neuer Browser-Login fuer das DU-Konto faellig.

## Folge-Karten

- **AP-07 / AP-08**: pytest-Harness und L1-Paper-Suite gegen den Stack.
