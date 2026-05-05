# cpgateway Login-Flow — Reverse-Engineering

Bezug: KanPrompt-Karte `ece90a8e-3a5a-4bb4-a875-6e992de359ff` *Paper-Stack
Auto-Login via Headless-Chromium-Sidecar (on-demand)*.

Ziel: dokumentieren, wie das Java-cpgateway-Login-Frontend tatsächlich
funktioniert, damit der geplante Auto-Login-Sidecar (Playwright/Chromium)
die richtigen Selektoren und Erfolgs-Marker verwendet.

Status: **Phase-1-Teilbefund**, ergänzt sich um HAR-Aufzeichnung beim
nächsten manuellen Browser-Login.

---

## 1. Probe-Setup

- Datum: 2026-05-04
- Ziel: `http://broker-gateway-paper-cpgateway:5000/` (Service-Name im
  Docker-Netz `broker-gateway-paper_default`)
- Methode: `docker run --rm --network broker-gateway-paper_default
  curlimages/curl:latest -sS -i -L http://broker-gateway-paper-cpgateway:5000/`
- Stack-Version: cpgateway-Image so wie deployed am 2026-05-04, broker-
  gateway-paper Hauptcontainer auf v1.25.0.

> Anmerkung: Der frühere Versuch mit Service-Name `paper-cpgateway` schlug
> mit `Could not resolve host` fehl. Der korrekte Compose-Service-Name in
> diesem Stack ist `broker-gateway-paper-cpgateway`. **Die Karten-Sollung
> ("paper-cpgateway") ist entsprechend zu korrigieren bei der
> Implementation.**

## 2. Statische Befunde (ohne JavaScript-Ausführung)

### 2.1 Redirect-Pfad

```
GET /                            -> 302 Found
                                    Location: /sso/Login?forwardTo=22&RL=1&ip2loc=US

GET /sso/Login?forwardTo=22&RL=1&ip2loc=US  -> 200 OK  (HTML)
```

### 2.2 Response-Header der Login-Page

```
HTTP/1.1 200 OK
Referrer-Policy: Origin-when-cross-origin
Content-Type: text/html;charset=UTF-8
Content-Language: en-US
X-Content-Type-Options: nosniff
Cache-Control: max-age=0, no-cache, no-store
Pragma: no-cache
Strict-Transport-Security: max-age=600 ; includeSubDomains
Vary: Accept-Encoding,Origin
```

### 2.3 Cookies, die schon beim ersten GET gesetzt werden

| Cookie | Pfad | Flags | Bedeutung |
|--------|------|-------|-----------|
| `JSESSIONID` | `/sso` | HttpOnly, Secure, SameSite=None | Java-Session-Cookie der SSO-App; muss in allen Folge-Requests mitgehen. |
| `URL_PARAM` | `/` | Secure | Trägt die ursprünglichen Query-Parameter (`forwardTo=22&RL=1&ip2loc=US`) für den Post-Login-Redirect. |
| `partnerID` | `/` | Secure, SameSite=None | Wird sofort wieder gelöscht (Expires 1970) — Reset-Marker. |
| `x-sess-uuid` | `/` | Secure, HttpOnly | IBKR-Session-Tracking-Cookie. |

### 2.4 HTML-Struktur — die Form ist NICHT statisch

Wichtigster Befund: das HTML enthält **kein** klassisches `<form
action=...>` mit `<input name="username">`-Feldern. Die Login-Maske wird
zur Laufzeit per JavaScript injiziert:

```html
<script type="text/javascript">
  $(document).ready(function() {
    window.IBSSO.XYZ.inject({
      WRAPPER: '.loginformWrapper',
      INJECTED_CALLBACK: function(err) {
        if (err) return;
        $('.xyz-append-hook').append('<div class="gstat pt-3"></div>');
        getMaintenaceMsg();
      },
      LOCALE: 'en_US',
      SERVICE: 'AM.LOGIN',
      // ... weitere Optionen folgen
    });
  });
</script>
```

Folgen unmittelbar daraus:

- Es gibt einen **Wrapper** `<div class="loginformWrapper">…</div>` im
  HTML, in den die Form später per JS hineingerendert wird.
- Die IBKR-`IBSSO.XYZ`-Library lebt unter
  `lib/xyz.bundle.min.js` (relativ zur Login-Page) und ist die zentrale
  SSO-Komponente, die IBKR auch im Web-Portal verwendet (Service-Code
  `AM.LOGIN`).
- Die Form-`action`, die Feldnamen und der eigentliche Submit-Endpoint
  sind erst **nach JS-Ausführung** im DOM sichtbar.

### 2.5 Mitgeladene Vendor-Scripts

```
/scripts/common/js/jquery-3.7.0/jquery.min.js
/scripts/common/js/bootstrap-5.2.2/bootstrap.bundle.min.js
lib/xyz.bundle.min.js                # IBKR IBSSO.XYZ-Bundle
```

CSS-Layer:
```
/css/bootstrap-5.2.2/bootstrap.min.css   (Layer: bootstrap)
/css/fontawesome-6.4.2/all.min.css        (Layer: vendors)
/css/bootstrap-switch-3.3.2/bootstrap-switch.min.css (Layer: vendors)
/css/reg-am/login.min.css
/css/ibkr/theme-ibkr-portal.min.css
```

### 2.6 Gstat-Maintenance-Endpoint

Die Login-Page macht beim Laden einen weiteren Call:

```
POST /portal.proxy/v1/gstat/bulletins
Content-Type: application/json; charset=utf8
Body: {"p":"login","type":"maintenance","format":"webapp"}
```

Der Auto-Login-Sidecar darf diesen Call **nicht blockieren**. Wenn
Playwright in Headless-Mode XHRs blockt (z.B. via Route-Interception),
muss diese URL whitelisted sein, sonst hängt das Login-Widget evtl. im
"Initialize"-State.

## 3. Konsequenzen für Implementation

1. **Plain-Form-POST aus Python ist nicht ausreichend.** Ohne JS-Ausführung
   gibt es keine Form, also auch keinen sinnvollen Submit-Endpoint. Die
   Wahl von Playwright mit echtem Chromium aus der Karte ist damit
   bestätigt — eine schlanke `httpx`-Variante hätte keine Chance.
2. **`SERVICE='AM.LOGIN'` ist das Login-Framework**, nicht ein
   cpgateway-spezifisches Widget. Selektoren sind potentiell mit dem
   IBKR-Web-Portal-Login identisch. Damit gibt es zwei sekundäre
   Recherche-Pfade:
   - Beobachtung des Web-Portal-Logins (`https://portal.interactivebrokers.com`)
     gibt vermutlich dieselben DOM-IDs.
   - Open-Source-Implementierungen wie `Voyz/IBeam` haben diese Selektoren
     bereits ermittelt — als Zweitquelle prüfen.
3. **Wrapper-Selektor für Polling:** Sidecar wartet auf
   `.loginformWrapper input[type="text"]` (Username-Feld) bzw.
   `.loginformWrapper input[type="password"]` als Erstindiz, dass die
   Form fertig injiziert ist.
4. **Erfolgs-Marker noch unbekannt** — muss aus HAR-Aufzeichnung kommen.
   Vermutung (nicht verifiziert):
   - URL-Wechsel auf Pfad ohne `/sso/Login`, vermutlich `/sso/StartTradingSession`
     oder analog.
   - DOM-Element mit dem im Browser-Tab heute sichtbaren Text "Client login
     succeeds".
5. **JSESSIONID-Cookie-Domain** ist auf Path `/sso` gesetzt — die SSO-App
   und das Trading-Frontend laufen unter unterschiedlichen Pfaden im
   gleichen Container. Cookie-Verwaltung im Sidecar muss das mitnehmen
   (Playwright handhabt das automatisch).

## 4. Phase 1.b — HAR-Aufzeichnung (durchgeführt 2026-05-05)

Aufzeichnung am 2026-05-05 vom Laptop aus, mit SSH-Tunnel
`localhost:5001 → cma-pi-1:5001 → broker-gateway-paper-cpgateway:5000`
und einem socat-Forwarder (`alpine/socat`). Der `cpgateway`-Container
wurde unmittelbar vor der Aufzeichnung mit `docker compose ... up -d
--force-recreate cpgateway` neu erstellt, damit die Aufzeichnung
einen vollständigen Login-Flow zeigt (vorher: leerer Session-State,
HTTP-Listener antwortet mit 401).

**Artefakte im Repo:**

- `docs/research/cpgateway-login-flow.har` — HAR-Export, Username
  per `cb***99` maskiert, keine Cookies (Chromium-DevTools-Default
  exportiert sie nicht in HARs), kein Passwort-Material (siehe
  Section 4.2).
- `docs/research/cpgateway-login-flow.summary.json` — strukturelle
  Zusammenfassung pro Request (Method, URL, Status, Field-Names,
  Set-Cookies-Namen, Größen).
- `scripts/redact_har.py` — Redaktions-Werkzeug, das aus einer
  Roh-HAR Username + Passwort + sensible Cookies entfernt; Aufruf
  per `BG_REDACT_USERNAME` / `BG_REDACT_PASSWORD`-Env, niemals als
  CLI-Argument.

### 4.1 Beobachtete Request-Reihenfolge (25 Entries)

| # | Methode + Pfad | Body-Bytes | Bedeutung |
|---|----------------|------------|-----------|
| 0, 1 | `POST /sso/Authenticator` | 200 | SRP-`INIT`, `LOGIN_TYPE=1` (Page-Bootstrap) |
| 2 | `GET /sso/Login?...` | — | Login-Page-HTML (vgl. Section 2) |
| 3-16 | Assets + Fonts | — | Bootstrap, jQuery, `xyz.bundle.min.js`, Fonts |
| 17 | `POST /sso/report` | 173 | Telemetry (kann für Sidecar ignoriert werden) |
| 18 | `POST /portal.proxy/v1/gstat/bulletins` | 52 | Maintenance-Banner-Probe |
| 19-21 | Logo + Fonts | — | Last-Mile-Assets |
| 22 | `POST /sso/Authenticator` | 200 | SRP-`INIT`, **`LOGIN_TYPE=2`** (zweiter Bootstrap nach Form-Render) |
| **23** | **`POST /sso/Authenticator`** | **377** | **SRP-`COMPLETEAUTH`**, `LOGIN_TYPE=2` — der eigentliche Login-Submit |
| **24** | **`POST /sso/Dispatcher`** | **24** | Erfolgs-Forwarder, Response-Body **`"Client login succeeds"`** |

### 4.2 Login-Protokoll: SRP-6 mit `SERVICE=AM.LOGIN`

Der Login läuft als **Secure Remote Password (SRP-6)** ab —
**das Klartext-Passwort verlässt den Browser nie**. Daraus folgt
direkt, dass eine plain-HTTP-Variante ausgeschlossen ist; der
Sidecar muss das JS-Bundle in einem echten Browser ausführen lassen
(Playwright/Chromium).

**INIT-Body (Form-encoded, `application/x-www-form-urlencoded`):**

```
ACTION=INIT
USER=<username>
A=<128-Hex SRP-Client-Public-Key>
RESP_TYPE=JSON
LOGIN_TYPE={1|2}
SERVICE=AM.LOGIN
```

**COMPLETEAUTH-Body:**

```
ACTION=COMPLETEAUTH
USER=<username>
M1=<40-Hex SHA-1-Client-Proof>
EKX=<252-Hex Session-Key-Exchange>
RESP_TYPE=JSON
VERSION=1
LOGIN_TYPE=2
```

`M1` und `EKX` werden vom IBKR-Bundle aus dem Passwort + dem
Server-`B` + `salt` abgeleitet — siehe SRP-6-Spezifikation, RFC 5054
und IBKR-`AM.LOGIN`-Implementierung.

**Dispatcher-Body (Erfolgsweiche):**

```
loginType=2&forwardTo=22
```

Response: HTTP 200, Body `Client login succeeds` (21 Bytes, plain
text). Das ist der **Erfolgs-Marker**, den der Auto-Login-Sidecar
abwarten muss.

### 4.3 Sidecar-Implementations-Implikationen

1. **Browser-Pfad alternativlos.** Eigener SRP-Client wäre möglich,
   aber: brüchig (IBKR kann Konstanten ändern), benötigt das genaue
   Hash-Schema, und müsste das `xyz.bundle.min.js`-Verhalten
   1:1 nachbauen. Playwright nutzt einfach das echte Bundle.
2. **Selektoren bleiben offen.** HAR liefert keinen DOM. Beim
   Sidecar-Bau müssen die Eingabefeld-Selektoren entweder live per
   `page.locator('.loginformWrapper input[type="text"]').first` /
   `... input[type="password"]` ermittelt werden, oder per
   `page.fill('input[name="USER"]', …)` falls das injizierte JS die
   `name`-Attribute setzt (im COMPLETEAUTH-Body steht `USER` — das
   legt nahe, dass das DOM-Feld auch `name="USER"` hat).
3. **Erfolgs-Marker im Sidecar.** Drei Optionen, von schnell nach
   robust:
   - `page.wait_for_url('**/sso/Dispatcher')` direkt nach Submit.
   - `page.wait_for_response(lambda r: r.url.endswith('/sso/Dispatcher') and r.status == 200)`.
   - DOM-Text: `page.wait_for_selector('text=Client login succeeds')`.
4. **Maintenance-Banner-Endpunkt nicht blocken.** `POST
   /portal.proxy/v1/gstat/bulletins` muss erreichbar bleiben, sonst
   bleibt das Login-Widget evtl. im "Initialize"-State (vgl.
   Section 2.6).
5. **Telemetry-Endpoint `/sso/report`** ist unkritisch und kann
   ignoriert oder geblockt werden.
6. **Mehrere INIT-Calls vor dem COMPLETEAUTH.** Der erste `INIT`
   passiert bereits beim Bootstrap, der zweite (`LOGIN_TYPE=2`) erst
   nach Form-Render — der Sidecar muss diese Calls dem Browser
   überlassen (passiert automatisch durch das `xyz.bundle`).

### 4.4 Was im HAR-Artefakt NICHT enthalten ist

- **Cookies / Set-Cookie-Header** — Chromium-DevTools schreibt sie
  per Default nicht in HAR-Exports. Die Cookie-Mechanik ist über
  den `curl -i`-Snapshot in Section 2.3 dokumentiert.
- **Passwort-Material** — selbst nicht in `M1` oder `EKX`, weil SRP
  ephemer arbeitet. Replay des HAR-Bodies funktioniert nicht (der
  Server kennt das aktuelle `B` aus dem `INIT` nicht mehr).
- **DOM-Selektoren** — siehe oben.

## 5. Konsequenzen für die Sidecar-Skript-Logik

Mit den Erkenntnissen aus Phase 1.b konkretisiert sich der Ablauf:

```
1. Goto http://broker-gateway-paper-cpgateway:5000/
   - Hard-Guard: target-URL enthält "broker-gateway-paper-cpgateway"
     sonst Exit-5
2. Erwartet Redirect zu /sso/Login?forwardTo=22&RL=1&ip2loc=US
3. Polling: warte bis .loginformWrapper input[type="password"] sichtbar
   (Form via xyz.bundle.min.js injiziert) — Timeout 20s -> Exit-1
4. Hard-Stop bei 2FA-Indikator: zusätzliches Feld
   .loginformWrapper input[name="code"|"otp"] oder Redirect auf
   /sso/2fa -> Exit-4
5. fill('input[name="USER"]', BG_PAPER_USERNAME) — Fallback-Selector
   '.loginformWrapper input[type="text"]:visible'
6. fill('input[name="PASSWORD"]', BG_PAPER_PASSWORD) — Fallback-Selector
   '.loginformWrapper input[type="password"]:visible'
7. Submit (Klick oder press Enter) — der xyz.bundle übernimmt SRP-Init,
   Mehrere /sso/Authenticator-Calls (INIT LOGIN_TYPE=1, LOGIN_TYPE=2,
   COMPLETEAUTH) folgen automatisch. Sidecar muss NICHTS davon manuell
   triggern.
8. Erfolgs-Marker: page.wait_for_response(
       lambda r: r.url.endswith("/sso/Dispatcher") and r.status == 200,
       timeout=30000)
   und Body == "Client login succeeds"
   - Timeout/anderer Body -> Exit-1
9. Cross-Check: GET http://broker-gateway-paper:8000/v1/internal/health
   mit Admin-Token
   - Falls auth_status != "ok" innerhalb 10s -> Exit-2 (Login abgelehnt)
10. Exit-0
```

Allowlist für Browser-Routen (falls der Sidecar auf
Bandbreiten-Optimierung setzt): Schriften und Bilder dürfen geblockt
werden, aber `*.css`, `*.js`, `/sso/Authenticator`, `/sso/Dispatcher`
und `/portal.proxy/v1/gstat/bulletins` müssen passieren.

Sonderfälle:

- 2FA-Pflicht: siehe Schritt 4 → Exit-4.
- CP-down: erste `goto` liefert ECONNREFUSED → Exit-3.
- Fremde Ziel-URL: Hard-Guard in Schritt 1 → Exit-5.
- Dispatcher liefert nicht `Client login succeeds` (z.B. abgewiesen
  wegen Captcha oder gesperrtem Account) → Exit-2.

## 6. Quellen / Belege

- HTML-Snapshot 2026-05-04: Auszug oben (Section 2.4) ist 1:1 aus dem
  Live-Response des Paper-cpgateway.
- HAR-Aufzeichnung 2026-05-05: `cpgateway-login-flow.har` und
  `cpgateway-login-flow.summary.json` (in diesem Verzeichnis). Quelle
  des Section-4-Inhalts.
- Karten-Memory: `project_paper_login_no_2fa.md`,
  `project_container_recreate_kills_session.md`.
- IBSSO-XYZ-Library lebt im cpgateway-JAR; kein offizieller Ankerpunkt
  in der IBKR-CP-API-Doku (`docs/research/ibkr-cpapi-doc.json`).
- SRP-6-Spezifikation: RFC 5054 (TLS-SRP) / Wu, "The SRP
  Authentication and Key Exchange System".
