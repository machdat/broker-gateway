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

## 4. Offen — Phase 1.b (HAR-Aufzeichnung beim nächsten manuellen Login)

Beim nächsten Browser-Login (z.B. nach dem nächsten Container-Recreate
beim Deploy) ist Folgendes zu erfassen:

1. Chromium → DevTools → Network-Tab → "Preserve log" aktivieren.
2. Login durchführen (Username/Passwort eingeben, Submit).
3. Sobald die "Client login succeeds"-Seite erscheint:
   Network-Tab → Rechtsklick → **"Save all as HAR with content"**.
4. HAR-Datei (mit Credentials redacten — Username teilweise, Passwort
   vollständig) ablegen unter
   `docs/research/cpgateway-login-flow.har` oder im Karten-Anhang.
5. Aus dem HAR extrahieren:
   - Echte POST-URL der Login-Form (vermutlich
     `https://www.interactivebrokers.com/sso/Login` oder ein
     cpgateway-internes `/sso/login`-Forward).
   - Echte Feldnamen (`USER`, `username`, `j_username`, ...).
   - Eventuelle CSRF-/`xyzab`-Hidden-Felder.
   - Response-Code und Redirect-URL bei Erfolg.
   - DOM-Selektoren: per "Inspect" auf das Username-Feld gehen, ID/class
     notieren.

## 5. Konsequenzen für die Sidecar-Skript-Logik

Folgende Schritte sollte das Sidecar-Skript abarbeiten:

```
1. Goto http://broker-gateway-paper-cpgateway:5000/
2. Warte auf Redirect zu /sso/Login? ...
3. Warte auf .loginformWrapper input[type="password"] (Form fertig injiziert)
4. Tippe Username in das Feld (Selektor noch zu ermitteln)
5. Tippe Passwort in das Feld
6. Klick auf Submit-Button (Selektor noch zu ermitteln)
7. Warte auf Erfolgs-Marker (URL-Wechsel ODER DOM-Text "Client login succeeds")
   - Timeout 30s -> Exit-1 (Form/Selector-Drift)
8. Cross-Check: GET http://broker-gateway-paper:8000/v1/internal/health
   - Falls auth_status != "ok" innerhalb 10s -> Exit-2 (Login abgelehnt)
9. Exit-0
```

Sonderfälle:

- 2FA-Pflicht: Erkennung über zusätzliches `code`-Feld in der Form oder
  Redirect auf `/sso/2fa` o.ä. → Exit-4.
- CP-down: erste `goto` liefert ECONNREFUSED → Exit-3.
- Fremde Ziel-URL: Hard-Guard prüft, dass die Goto-URL
  `broker-gateway-paper-cpgateway` enthält → sonst Exit-5.

## 6. Quellen / Belege

- HTML-Snapshot 2026-05-04: Auszug oben (Section 2.4) ist 1:1 aus dem
  Live-Response des Paper-cpgateway.
- Karten-Memory: `project_paper_login_no_2fa.md`,
  `project_container_recreate_kills_session.md`.
- IBSSO-XYZ-Library lebt im cpgateway-JAR; kein offizieller Ankerpunkt
  in der IBKR-CP-API-Doku (`docs/research/ibkr-cpapi-doc.json`).
