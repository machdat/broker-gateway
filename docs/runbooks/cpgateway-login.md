# Runbook: CP-Gateway Browser-2FA-Login

> **Bei Session-Pause zuerst Resume-Pfad versuchen:**
> [`cpgateway-session-resume.md`](cpgateway-session-resume.md). Wenn
> der Container weitergelaufen ist und nur das SSO-Cookie schlaeft,
> spart `POST /iserver/reauthenticate` plus 2x 90 s Drift-Check die
> 2FA-Tortur.

Schritt-fuer-Schritt-Anleitung, wie der IBKR Client Portal Gateway im
broker-gateway-Compose-Stack initial mit dem Live-Account
**U25235077** (Non-Pro AT) verbunden wird.

Der CP-Gateway-Container haelt nur eine **eine** Trading-Session pro
Konto offen (harte IBKR-Regel). Diese Session erfordert beim ersten
Start eine manuelle Browser-Anmeldung mit 2FA — danach uebernimmt der
broker-gateway-Service den Tickle-Lifecycle und haelt die Session
warm.

> **Wichtig:** Nach jedem Container-Restart UND nach jedem von IBKR
> erzwungenen Session-Verlust (z.B. paralleler Login von der
> IBKR-Mobile-App, "Session-Kicked"-Event) muss dieser Login erneut
> durchgespielt werden. Ohne frischen Login antworten alle
> `/v1/api/iserver/...`-Endpunkte unauthorisiert.

## Voraussetzungen

- Compose-Stack ist auf dem Ziel-Host aufgesetzt
  (z.B. `cma-pi-1:/mnt/ssd/broker-gateway`).
- `ops/cpgateway/clientportal.gw.tar.gz` liegt vor und die SHA256
  in `ops/cpgateway/clientportal.gw.tar.gz.sha256` stimmt
  (siehe `ops/cpgateway/README.md`).
- IBKR-Login-Daten fuer **U25235077** sind griffbereit:
  Username, Passwort, 2FA-Geraet (IBKR Mobile App / SMS / Soft-Token).
- SSH-Zugang zum Ziel-Host vom eigenen Laptop aus.

## Schritte

### 1. CP-Gateway-Container starten

Auf dem Ziel-Host (z.B. cma-pi-1):

```bash
cd /mnt/ssd/broker-gateway
docker compose up -d cpgateway
docker compose ps cpgateway
```

Erwartete Ausgabe in `docker compose ps`:
- Status `running`
- Health `healthy` nach max. 2 Minuten (der Healthcheck akzeptiert
  HTTP 401 als "lebendig, aber unauth" — das ist der Normalzustand
  vor dem Login).

Falls der Container nicht innerhalb von 2 Minuten healthy wird:
siehe `cpgateway-troubleshooting.md` Abschnitt **Healthcheck haengt**.

### 2. SSH-Reverse-Tunnel vom Laptop oeffnen

Der CP-Gateway-Port 5000 ist nicht extern publiziert (kein
`ports:`-Mapping in compose.yaml). Vom Entwickler-Laptop aus wird er
ueber einen SSH-Local-Forward zugaenglich gemacht:

```bash
ssh -N -L 5000:localhost:5000 cma@cma-pi-1
```

Erklaerung:
- `-N` keine Shell, nur Tunnel.
- `-L 5000:localhost:5000` lokaler Port 5000 wird auf den Host-Port
  5000 gemappt. **Aber:** der Container-Port 5000 ist im Compose-Netz
  nicht auf den Host gemappt. Damit der Tunnel auf den Container
  trifft, muss vorher temporaer `published: true` gesetzt werden ODER
  der `docker exec`-Variante (Schritt 2b) genutzt werden.

#### Variante A — temporaere Port-Publikation am Compose-Host

Der saubere Weg: einmalig auf dem Host `docker compose up -d
cpgateway` mit zusaetzlichem `--publish 5000:5000`-Override. Das geht
mit einem Compose-Override-File `compose.login-override.yaml`:

```yaml
services:
  cpgateway:
    ports:
      - "127.0.0.1:5000:5000"
```

Dann auf dem Host:
```bash
docker compose -f compose.yaml -f compose.login-override.yaml up -d cpgateway
```

Damit lauscht 5000 auf 127.0.0.1 des Hosts — von aussen weiterhin
abgeschottet, aber per SSH-Tunnel erreichbar.

#### Variante B — Port-Forward direkt aus dem Container

Alternativ (wenn der Override nicht verfuegbar ist) auf dem Host:
```bash
docker run --rm --network broker-gateway_default \
    -p 127.0.0.1:5000:5000 alpine/socat \
    TCP-LISTEN:5000,fork TCP:cpgateway:5000
```

Das ist eine Wegwerf-Loesung fuer einen einmaligen Login.

### 3. Browser-Login durchfuehren

Auf dem Laptop (mit aktivem SSH-Tunnel aus Schritt 2):

1. Browser oeffnen, Adresse: <http://localhost:5000>
2. Es erscheint die IBKR-Login-Seite (HTML-Form). Keine Zertifikats-
   warnung, weil die CP-Gateway-Konfiguration `listenSsl: false`
   verwendet — der Transport ist durch den SSH-Tunnel abgesichert.
3. Username `U25235077` und Passwort eingeben.
4. 2FA-Bestaetigung im IBKR-Mobile-App (Push) oder via SMS-/Soft-Token.
5. Nach erfolgreicher Anmeldung zeigt die Seite "Client login succeeds"
   bzw. "You're now logged in".

### 4. Login validieren

Auf dem Ziel-Host:

```bash
curl -s http://localhost:5000/v1/api/iserver/auth/status | jq .
```

Erwartete Antwort:
```json
{
  "authenticated": true,
  "competing": false,
  "connected": true,
  "message": "",
  "MAC": "...",
  "serverInfo": { "serverName": "...", "serverVersion": "..." }
}
```

Wichtigstes Feld: `"authenticated": true`. Wenn das fehlt oder
`false` zurueckkommt, ist der Login nicht durchgegangen — Browser-
Login wiederholen.

### 5. broker-gateway-Service starten

Nach erfolgreichem Login auf dem Host:

```bash
docker compose up -d gateway
```

Health-Check des broker-gateway-Service (admin-Token vorausgesetzt):

```bash
curl -s -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
    http://localhost:4000/v1/internal/health | jq .
```

Erwartete Felder:
- `cp_reachable: true`
- `session_status: "ok"`
- `last_tickle: "<timestamp innerhalb der letzten 60 Sekunden>"`

Der Tickle-Job des broker-gateway-Service haelt die IBKR-Session ab
diesem Moment automatisch warm. Solange der Service laeuft, bleibt
`authenticated: true`.

### 6. SSH-Tunnel schliessen

Login-Tunnel wird nicht mehr gebraucht — Strg+C im SSH-Fenster.
Falls Variante A genutzt wurde, optional zusaetzlich auf dem Host
das Override-File entfernen und nur mit dem normalen
`compose.yaml` weiterlaufen:

```bash
docker compose -f compose.yaml up -d --no-deps cpgateway
```

So bleibt Port 5000 nicht laenger publiziert als noetig.

## Wann der Login wiederholt werden muss

| Anlass | Wiederholung noetig? |
|--------|----------------------|
| `docker compose restart cpgateway` | **Ja** — neue Session, kein gespeichertes Cookie |
| Container-Image-Rebuild + `up -d cpgateway` | **Ja** |
| Reboot des Hosts (cma-pi-1 neu hochgefahren) | **Ja** |
| broker-gateway-Service neu gestartet, cpgateway laeuft weiter | Nein — Session bleibt im cpgateway |
| `competing: true` im `/iserver/auth/status` (paralleler Login von Mobile-App) | **Ja**, sobald die andere Session beendet ist |
| Tickle scheitert > 3x in Folge | **Ja** — Service kippt auf `auth_lost` |

Der broker-gateway-Service detektiert die letzten beiden Faelle und
markiert seinen internen Status als `auth_lost`. Ab dann liefern alle
Business-Endpunkte `503 Service Unavailable` mit `Retry-After: 30` —
das ist das Signal, dieses Runbook erneut durchzulaufen.
