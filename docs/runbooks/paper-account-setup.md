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

## Folge-Karten

- **AP-06 K2**: `ops/build-gateway.sh` um `--env=paper|live`-Schalter erweitern.
- **AP-06 K3**: `compose.paper.yaml` mit Port- und Volume-Override.
- **AP-06 K4**: Initial-Login fuer Paper-Account (analog `cpgateway-login.md`).
- **AP-07 / AP-08**: pytest-Harness und L1-Paper-Suite gegen den Stack.
