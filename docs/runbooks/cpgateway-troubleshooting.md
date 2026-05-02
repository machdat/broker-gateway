# Runbook: CP-Gateway Troubleshooting

Bekannte Fehlerbilder beim Betrieb des IBKR-Client-Portal-Gateway-
Containers im broker-gateway-Compose-Stack, mit jeweils getesteter
Loesung.

Das Login-Runbook (`cpgateway-login.md`) deckt den Happy Path ab.
Diese Datei sammelt alles, was schiefgehen kann.

> **Wenn die Session "nur" pausiert war** (Laptop-Sleep, kurzer
> Container-Restart, geplante Wartung): zuerst dem Reauth-Pfad in
> [`cpgateway-session-resume.md`](cpgateway-session-resume.md) folgen,
> bevor du auf Browser-2FA eskalierst. Spart in den meisten Faellen
> die 2FA-Tortur.

---

## 1. Container scheitert beim Build mit SHA256-Mismatch

**Symptom:**
```
sha256sum: WARNING: 1 computed checksum did NOT match
clientportal.gw.tar.gz: FAILED
```
beim `docker compose build cpgateway`.

**Ursache:** Das in `ops/cpgateway/clientportal.gw.tar.gz` liegende
Tarball stimmt nicht mit dem in
`ops/cpgateway/clientportal.gw.tar.gz.sha256` hinterlegten Hash
ueberein. Entweder das Tarball ist beschaedigt, oder eine neuere
IBKR-Version wurde heruntergeladen, ohne dass die Pruefsumme
mitgezogen wurde.

**Loesung:**
1. Pruefen, ob die heruntergeladene Datei intakt ist:
   ```bash
   sha256sum ops/cpgateway/clientportal.gw.tar.gz
   ```
2. Wenn ja, neue IBKR-Version validieren (Release-Notes pruefen) und
   den neuen Hash committen:
   ```bash
   sha256sum ops/cpgateway/clientportal.gw.tar.gz \
       | tee ops/cpgateway/clientportal.gw.tar.gz.sha256
   git add ops/cpgateway/clientportal.gw.tar.gz.sha256
   git commit -m "chore: cpgateway sha256 fuer IBKR <version> aktualisiert"
   ```
3. Wenn das Tarball beschaedigt ist, neu von IBKR herunterladen
   (siehe `ops/cpgateway/README.md`).

**Niemals einfach den Hash blind ueberschreiben** ohne die
Versionsnummer zu dokumentieren — sonst ist nicht mehr nachvollziehbar,
gegen welche IBKR-Version der Service je getestet wurde.

---

## 2. Healthcheck wird nie gruen, Container bleibt `starting`

**Symptom:** `docker compose ps cpgateway` zeigt dauerhaft
`Up X seconds (health: starting)` und nach 2 Minuten `unhealthy`,
ohne dass der Container neu startet.

**Ursachen + Diagnose:**

| Ursache | Diagnose-Befehl | Indiz |
|---------|-----------------|-------|
| Java-Heap-OOM beim Start | `docker compose logs cpgateway` | `OutOfMemoryError`, `Killed` |
| Port 5000 intern blockiert | `docker compose exec cpgateway ss -tln` | Port nicht gelistet |
| `bin/run.sh` nicht ausfuehrbar | `docker compose exec cpgateway ls -l bin/run.sh` | Kein `x`-Bit |
| conf.yaml Syntaxfehler | `docker compose logs cpgateway` | YAML-Parser-Exception |

**Loesungen:**

- **OOM:** In `compose.yaml` beim cpgateway-Service ein `deploy.resources.limits.memory: 1g` hochziehen oder `JAVA_OPTS=-Xmx512m` als Environment-Variable setzen.
- **Port blockiert:** Im Container `cat /opt/clientportal.gw/root/conf.yaml` pruefen — `listenPort` muss 5000 sein. Wenn das stimmt, Container neu bauen (`docker compose build --no-cache cpgateway`).
- **bin/run.sh:** Das Tarball wurde wahrscheinlich falsch verpackt (Wrapper-Verzeichnis statt direktem Inhalt). Anleitung in `ops/cpgateway/README.md` Schritt 2 nochmal genau befolgen.
- **YAML-Fehler:** Geaenderte `conf.yaml` lokal mit `python -c 'import yaml; yaml.safe_load(open("ops/cpgateway/conf.yaml"))'` validieren.

---

## 3. Browser-Login zeigt Zertifikatswarnung

**Symptom:** Beim Aufruf von <http://localhost:5000> kommt eine
"Verbindung nicht sicher"-Warnung mit selbstsigniertem Zertifikat.

**Ursache:** Das Setup wurde auf `listenSsl: true` umgeschaltet
(z.B. um zu testen, ob IBKR bestimmte Endpunkte nur ueber HTTPS
liefert). Der CP-Gateway erzeugt dann ein selbstsigniertes Cert
fuer `localhost`.

**Loesung:**

- **Wenn HTTPS bewusst gewollt:** Im Browser die Warnung akzeptieren.
  Die Verbindung laeuft durch den SSH-Tunnel — Transport-Verschluesselung
  haengt am SSH, nicht am IBKR-Cert. Sobald der Browser die Seite
  laedt, normal anmelden.
- **Wenn HTTPS unbeabsichtigt:** `ops/cpgateway/conf.yaml` zurueck auf
  `listenSsl: false` setzen, Image neu bauen, Container neu starten.
  Browser-URL bleibt `http://localhost:5000`.

---

## 4. `/iserver/auth/status` liefert `competing: true`

**Symptom:** Nach erfolgreichem Browser-Login zeigt
`curl /v1/api/iserver/auth/status` zwar `authenticated: true`, aber
zusaetzlich `competing: true`. Kurz darauf kippt der Status auf
`authenticated: false`.

**Ursache:** Es gibt eine zweite aktive Login-Session fuer denselben
IBKR-Account — typischerweise die IBKR-Mobile-App, ein paralleles
TWS auf einem anderen Geraet, oder eine alte Session aus einem
frueheren Container-Run, die noch nicht serverseitig invalidiert
wurde. IBKR akzeptiert nur **eine aktive Session pro Account** — die
neuere kickt die aeltere oder umgekehrt.

**Loesung:**

1. Auf allen anderen Geraeten/Apps explizit **abmelden**:
   - IBKR-Mobile-App: Profil → Logout
   - TWS-Desktop: File → Exit
   - Andere Browser-Tabs auf <http://localhost:5000>: schliessen
2. 30-60 Sekunden warten (IBKR-Server-Side-Cleanup).
3. Browser-Login (Schritt 3 aus `cpgateway-login.md`) wiederholen.
4. `competing: false` im Status-Check verifizieren.

Falls `competing: true` dauerhaft bleibt: bei IBKR-Support melden,
dass eine "stuck session" auf U25235077 forciert beendet werden soll.

---

## 5. Container laeuft, aber broker-gateway meldet `cp_reachable: false`

> **Erst pruefen:** Ist der Container laufzeit-gesund, aber die Session
> nur pausiert? Dann dem Reauth-Pfad in
> [`cpgateway-session-resume.md`](cpgateway-session-resume.md) folgen.
> Erst wenn dort Schritt 5 (Eskalation auf Browser-Login) erreicht
> wird, ist dieses Fehlerbild relevant.

**Symptom:** `cpgateway`-Container ist healthy, aber
`/v1/internal/health` des broker-gateway-Service zeigt
`cp_reachable: false` und `last_tickle_error: "ConnectError"`.

**Ursache:** Netzwerk-Routing zwischen den beiden Containern
funktioniert nicht. Entweder das Compose-Network ist falsch konfiguriert,
oder die Service-Discovery-DNS innerhalb von Docker greift nicht.

**Diagnose:**
```bash
docker compose exec gateway curl -sS http://cpgateway:5000/v1/api/one/user -o /dev/null -w '%{http_code}\n'
```

Erwartet: `401` oder `200`. Wenn `Could not resolve host` oder
`Connection refused` kommt:

- `docker compose ps` pruefen: beide Services im selben Stack?
- `docker network ls` und `docker network inspect broker-gateway_default`
  pruefen: beide Container in der Liste?
- `BG_CP_BASE_URL` im gateway-Service ist auf `http://cpgateway:5000`
  gesetzt? Default ist das, aber Override im `.env` moeglich.

**Loesung:** Stack komplett neu starten:
```bash
docker compose down
docker compose up -d
```

Falls das nicht hilft: Compose-Network-Recreate erzwingen:
```bash
docker compose down
docker network prune -f
docker compose up -d
```

---

## 6. Log-Dateien gehoeren root statt cma (UID-Mismatch)

**Symptom:** Log-Dateien in `var/cpgateway/logs/` auf dem Host gehoeren
`root:root` (oder einem anderen unerwarteten User), obwohl der Container
laeuft. `cma` kann lesen, aber `rm`/`mv`/`logrotate` schlaegt fehl.

**Ursache:** UID/GID, mit der das Image gebaut wurde, passt nicht zum
Host-User. Ab v1.0.3 laeuft der Container als `cpgw` mit UID/GID, die
ueber Build-Args `CPGW_UID`/`CPGW_GID` (Default 1000) gesteuert werden.
Wenn `id cma` auf dem Ziel-Host eine andere UID liefert als beim Build
verwendet wurde, schreibt der Container Logs mit der internen UID -
fuer den Host wirkt das wie ein fremder Owner.

**Diagnose:**
```bash
id cma                           # UID/GID des Host-Users
ls -n var/cpgateway/logs         # numerische UID/GID der Log-Dateien
docker compose exec cpgateway id # UID/GID des Container-Prozesses
```

Stimmen UID und GID zwischen Host-User und Container-Prozess nicht
ueberein, ist das die Ursache.

**Loesung:**

1. UID/GID des Host-Users ermitteln: `id cma` -> z.B. `uid=1001 gid=1001`.
2. Werte in `.env` (im Repo-Root) eintragen:
   ```
   CPGW_UID=1001
   CPGW_GID=1001
   ```
3. Image neu bauen: `docker compose build cpgateway`.
4. Bestehende Logs einmalig auf Host-User ueberfuehren:
   `sudo chown -R cma:cma var/cpgateway/`.
5. Stack neu starten: `docker compose up -d cpgateway`.

Default 1000 deckt den Standard-Pi-Setup ab, daher ist auf cma-pi-1
kein Override noetig. Aelteres `chmod 777`-Workaround (vor v1.0.3)
ist obsolet und sollte aus lokalen Setups entfernt werden.

---

## 7. conf.yaml nutzt Property, das CP-Gateway nicht kennt

**Symptom:** Container geht in CrashLoop kurz nach dem Start. Compose-
Healthcheck erreicht `unhealthy` und Container wird endlos neu gestartet.
`docker compose logs cpgateway` zeigt:

```
loading configuration file error:Cannot create property=XYZ for
JavaBean=ibgroup.web.core.clientportal.gw.Config
 in 'reader', line N, column 1:
    listenPort: 5000
    ^
Unable to find property 'XYZ' on class: ibgroup.web.core.clientportal.gw.Config
```

**Ursache:** Die `ops/cpgateway/conf.yaml` enthaelt einen Eintrag, den
das IBKR-Schema nicht kennt. Konkret aufgetreten waehrend der
Erst-Inbetriebnahme: ein erfundenes `proxyRemotePort` (existiert nicht;
korrekt ist nur `proxyRemoteHost: "https://api.ibkr.com"`). Snake-YAML
laedt jeden Top-Level-Schluessel in eine Java-Bean-Property und scheitert
hart, wenn die Property fehlt.

**Loesung:**

1. Original-conf des installierten Tarballs als Referenz oeffnen, um die
   gueltigen Felder zu sehen:
   ```bash
   tar xzf ops/cpgateway/clientportal.gw.tar.gz -C /tmp/cpgw-inspect
   cat /tmp/cpgw-inspect/root/conf.yaml
   ```
   Alternativ aus dem laufenden (oder zuletzt erfolgreich gebauten) Image:
   ```bash
   docker run --rm broker-cpgateway:VERSION cat /opt/clientportal.gw/root/conf.yaml
   ```
2. Nicht-existierendes Property aus `ops/cpgateway/conf.yaml` entfernen
   oder durch das tatsaechlich existierende Schluesselwort ersetzen.
3. Image neu bauen und Container starten:
   ```bash
   docker compose build cpgateway
   docker compose up -d cpgateway
   ```

Faustregel: nur Felder verwenden, die in der mitgelieferten Original-
`conf.yaml` vorkommen. Eigene Felder erfindet das CP-Gateway nicht zu.

---

## 8. ips.allow Wildcard fuehrt zu HTTP 500 auf jedem Request

**Symptom:** Container ist `healthy`, der HTTP-Listener auf 5000 antwortet,
aber jeder Request liefert HTTP 500 mit Body `Internal Server Error` und
`Content-Length: 21`. Browser-Login-Seite laedt nicht, `/v1/api/...`
schlaegt fehl, der `gateway`-Service zeigt `cp_reachable=false` mit
HTTP-Errors.

**Diagnose:** Die Antwort-Body sagt nichts. Die echte Ursache steht im
internen Container-Log (Volume-Mount `var/cpgateway/logs/`):

```bash
grep -A5 PatternSyntaxException var/cpgateway/logs/gw.YYYY-MM-DD.log
```

oder im laufenden Container:
```bash
docker compose exec cpgateway tail -50 /opt/clientportal.gw/logs/gw.*.log
```

Erwartete Spuren: ein Java-Stacktrace mit `IPv4Validator.setPattern` und
`java.util.regex.PatternSyntaxException` in der ersten Zeile.

**Ursache:** Das CP-Gateway interpretiert die Eintraege unter
`ips.allow` als Java-Regex-Patterns (nicht als Glob-Pattern). Das
nackte Sternchen `*` allein ist **kein** gueltiges Regex - es muss `.*`
heissen, oder als Glob-Praefix wie `192.*` (was zu Regex `192\..*` wird).
Mit `["*"]` scheitert die Pattern-Compilation und jeder Request wird
serverseitig mit HTTP 500 abgewiesen.

**Loesung:** explizite Liste mit den realen Sub-Netzen, aus denen
Zugriffe kommen:

```yaml
ips:
  allow:
    - 127.0.0.1
    - 10.*
    - 172.*       # Compose-Default-Bridge ist 172.x
    - 192.168.*
  deny: []
```

Oder noch enger, wenn das Compose-Netz fest gepinnt ist (z.B. nur
`172.18.*`). `127.0.0.1` muss bleiben, sonst scheitert auch der
Healthcheck im Container selbst.
