# Runbook: CP-Gateway Browser-2FA-Login

> **Bei Session-Pause zuerst Resume-Pfad versuchen:**
> [`cpgateway-session-resume.md`](cpgateway-session-resume.md). Wenn
> der Container weitergelaufen ist und nur das SSO-Cookie schlaeft,
> spart `POST /iserver/reauthenticate` plus 2x 90 s Drift-Check die
> 2FA-Tortur.

Schritt-fuer-Schritt-Anleitung, wie der IBKR Client Portal Gateway im
broker-gateway-Compose-Stack initial mit dem **aktiven Live-Account**
(dediziertes Service-Konto; Credentials in Pi-`.env` +
Passwort-Manager) verbunden wird.

> **Konto-Hinweis:** Bis zum Cutover am 2026-06-08 war `U25235077`
> (Non-Pro AT, Operator-Privatkonto chmangold) der Live-Account —
> siehe [`account-cutover.md`](account-cutover.md). Aeltere
> Beispiel-Ausgaben und Recordings tragen daher noch diese ID.

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
- IBKR-Login-Daten fuer das **aktive Live-Konto** sind griffbereit:
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
3. Username und Passwort des aktiven Live-Kontos eingeben
   (siehe Konto-Hinweis am Dokumentanfang).
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

## Bekannter Auth-Bug (Stand 2026-05-08)

Der Login-Pfad oben funktioniert auf der **UI-Ebene** (Browser zeigt
"Client login succeeds"), aber der **Service-Lifecycle bleibt
`auth_lost`**. Schritt 4 (`/v1/api/iserver/auth/status`) liefert HTTP
401 statt `authenticated:true`. Das Problem ist
**cpgateway-spezifisch und strukturell**:

- IBKR hat den `clientportal.gw`-Tarball seit 2023-04-24 nicht mehr
  aktualisiert (Stable + Beta beide auf diesem Build, Manifest
  `When-Built=2023-04-24T15:42:44-0400`,
  `Implementation-Version=ibgroup.web.core.iblink.router.clientportal.gw-20230424154245`).
- Wire-Diagnose 2026-05-08 (Karten 739777a9 + d1c837f9): `POST
  /sso/Authenticator` Round 1+2 -> 200, `POST /sso/Dispatcher` -> 200
  mit Login-Form-HTML-Body. Authenticator(2)-Antwort liefert ein
  gueltiges SRP `M2`-Server-Proof + `reached_max_login:false` — der
  SRP-Handshake ist kryptographisch erfolgreich, aber das Backend
  etabliert NACH `COMPLETEAUTH` keine authentifizierte Session.
- ndcdyn.interactivebrokers.com Login mit identischen Credentials
  klappt sofort (Konto-Dashboard, Balance) — der Auth-Pfad dort nutzt
  ein anderes Backend-API.

**Konsequenz fuer Operations:** der hier dokumentierte Login-Pfad
bringt `authenticated:true` mit dem aktuellen Tarball nicht zustande.
Das Runbook bleibt erhalten als Referenz fuer den Tag, an dem ein
Fork (siehe Karte a78431aa) oder ein neuer IBKR-Build den Bug
behebt.

## Alternative Container-Wrapper (Stand 2026-05-08)

Karte `a78431aa` hat zwei Open-Source-Wrapper als moegliche
Workaround-Pfade evaluiert:

| Wrapper | Login-Mechanik | Eigener Login-Stack? | Aktivitaet | Befund |
|---------|----------------|----------------------|------------|--------|
| `ppaanngggg/ib-cp-server` | chromedp (Headless-Chrome) gegen IBKR-Login-Form, Proxy zu embedded `clientportal.gw` | nein | last push 2024-04, 0 Stars, MIT | identisch zu Pi-Chromium-Pfad; reproduziert denselben Tarball-Auth-Bug. Selektor `#xyz-field-username` veraltet (heute `#xyz-field-credential`); Fork wartet zwingend auf `.xyzblock-notification`-2FA-Push (Paper-Account hat keinen). |
| `schuss-capital/ib-client-docker` | keiner — reiner Tarball-Container ohne Login-Layer | nein | last push 2022-11, 0 Stars, kein License | hilft nicht: kein Beitrag zum Login-Pfad. |
| Eigenbau (`ops/cpgateway/`) | Pi-Chromium / nsenter / Cookie-Bridge | nein | aktiv | dokumentierter Stand, gleicher Tarball-Bug. |

**Empfehlung: keinen Fork adoptieren.** Beide Wrapper nutzen denselben
2023er IBKR-Tarball intern und bringen keinen eigenen Login-Stack
(z.B. direkt-SRP gegen `api.ibkr.com` ohne Java-Backend). Damit
treffen sie die strukturelle Wurzel nicht. Phase 2 (Smoke auf cma-pi-1)
wurde uebersprungen — die Inventur war eindeutig.

Reaktivieren der Fork-Pruefung wird sinnvoll, sobald (a) IBKR den
Tarball aktualisiert, (b) ein Fork mit echtem direkt-SRP-Stack
auftaucht, oder (c) ein Strategiewechsel zu IBKR-Web-API/OAuth
ansteht.

## Diagnose: DEBUG-Logging aktivieren

Logback-Default loggt nur Access-Log-Stil. Fuer SRP-/Cookie-/Auth-
Pipeline-Detail liegt im Repo eine vorbereitete Variante:

```bash
# 1. Datei auf den Pi holen
ssh cma@cma-pi-1 'curl -sL -o /tmp/logback-debug.xml \
  https://raw.githubusercontent.com/machdat/broker-gateway/main/ops/cpgateway/logback-debug.xml'

# 2. Default sichern + Debug-Variante einspielen
ssh cma@cma-pi-1 'docker exec broker-gateway-paper-cpgateway \
  cp /opt/clientportal.gw/root/logback.xml \
     /opt/clientportal.gw/root/logback.xml.dist'
ssh cma@cma-pi-1 'docker cp /tmp/logback-debug.xml \
  broker-gateway-paper-cpgateway:/opt/clientportal.gw/root/logback.xml'

# 3. Logback liest die neue Konfig per scan=true automatisch nach ~10s.
# Kein Container-Restart noetig (Memory project_container_recreate_kills_session).
```

Aktiviert: `HttpMessageLogger` + `CookieManager` + `ibgroup`-Namespace
auf DEBUG, STDOUT-Appender enabled. Logs landen in
`/opt/clientportal.gw/logs/gw.<datum>.log` und
`gw.message.<datum>.log` plus `docker logs`. **Die Wire-Logs koennen
Cookies/Tokens enthalten — Logs nicht aus dem Pi raus geben, nach
Diagnose loeschen.**

Reset auf Default:
```bash
ssh cma@cma-pi-1 'docker exec broker-gateway-paper-cpgateway \
  cp /opt/clientportal.gw/root/logback.xml.dist \
     /opt/clientportal.gw/root/logback.xml'
```

## Diagnose: Wire-Capture (HTTP-Bodies)

`HttpMessageLogger` schreibt nur `body size XXXX`, nicht den Inhalt.
Fuer Klartext-Bodies eignet sich ein tcpdump-Container im
gemeinsamen Network-Namespace des cpgateway:

```bash
# Capture starten (5 Sekunden Setup wegen apk add tcpdump)
ssh cma@cma-pi-1 'docker run --rm -d --name cpgw-tcpdump \
  --network=container:broker-gateway-paper-cpgateway \
  --cap-add=NET_ADMIN --cap-add=NET_RAW \
  -v /tmp:/host alpine \
  sh -c "apk add --no-cache tcpdump >/dev/null 2>&1 && \
         exec tcpdump -i any -s0 -U -w /host/cpgw-cap.pcap tcp port 5000"'

# ... User-Login durchfuehren ...

# Capture stoppen
ssh cma@cma-pi-1 'docker stop cpgw-tcpdump'

# Bodies extrahieren (tshark via apk add)
ssh cma@cma-pi-1 'docker run --rm -v /tmp:/host alpine \
  sh -c "apk add --no-cache tshark >/dev/null 2>&1 && \
         tshark -r /host/cpgw-cap.pcap --export-objects http,/host/bodies"'

# Authenticator/Dispatcher-Bodies inspizieren
ssh cma@cma-pi-1 'cd /tmp/bodies && for f in Authenticator* Dispatcher*; do \
  echo "=== $f ==="; cat -- "$f"; echo; done'
```

Das pcap und die Bodies enthalten Cookies und SRP-Material — auf der
Pi belassen, nach der Diagnose loeschen. Die `HttpMessageLogger`-
Header-Logs sind dafuer in der Regel sicher genug, der pcap-Capture
ist nur fuer eine Vertiefungs-Session.
