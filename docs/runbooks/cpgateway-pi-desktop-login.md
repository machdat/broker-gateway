# Runbook: cpgateway Pi-Desktop Quick-Login

> **Wann benutzen:** wenn die cpgateway-Session frisch gemacht werden
> muss (`auth_status != ok`), aber ein voller manueller Browser-Login
> via SSH-Tunnel zu aufwändig ist. Voraussetzung ist die VNC-Pi-Desktop-
> Session unter User `cma`. Für den Erst-Login (frischer Container,
> noch nie eingeloggt) und für Live mit 2FA-Erstauthentisierung gilt
> weiterhin [`cpgateway-login.md`](cpgateway-login.md).

Hintergrund: cpgateway hält genau **eine** Trading-Session pro Konto
offen, die nach jedem Container-Recreate, IBKR-Session-Kick oder
Auth-Drift neu gemacht werden muss. Der manuelle Recovery-Login ging
früher nur über SSH-Tunnel + Browser auf dem User-Laptop. Seit v1.30.0
exponiert `cpgateway` auf cma-pi-1 einen lokalen `127.0.0.1`-Bind
(Live `:5000`, Paper `:5001`), so dass der Login-Browser auf der
Pi-Desktop-Session selbst läuft. Ergebnis: VNC öffnen, Tab refreshen,
Passwort tippen — fertig.

## Voraussetzungen

- VNC-Client am Laptop (z.B. RealVNC Viewer, TigerVNC). Standard-Port
  ist `5900` auf dem Pi. Bei aktuellem wayvnc-Setup hängt der Server
  auf `*:5900`; siehe Sicherheits-Hinweis unten.
- Pi-Desktop-Session läuft als User `cma` (Wayland, ungebrochen seit
  Setup).
- Chromium oder Firefox auf dem Pi installiert (beide vorhanden:
  `/usr/bin/chromium`, `/usr/bin/firefox`).
- broker-gateway-Stack ist mindestens auf Version 1.30.0 deployed —
  davor war `cpgateway:5000` nicht auf den Pi-Host gemappt.

## Erst-Setup (einmalig)

1. Vom Laptop aus per VNC auf den Pi verbinden (Port 5900). Der
   Wayland-Compositor zeigt den eingeloggten cma-Desktop.
2. Browser auf dem Pi-Desktop öffnen (Empfehlung: Chromium — leichter
   und reagiert direkter unter Wayland).
3. Login-Tabs öffnen und als Bookmark ablegen:
   - **Paper:** `http://127.0.0.1:5001/sso/Login?forwardTo=22&RL=1&ip2loc=on`
   - **Live:**  `http://127.0.0.1:5000/sso/Login?forwardTo=22&RL=1&ip2loc=on`
4. Auf der Login-Seite den Live-/Paper-Toggle einmalig auf den
   gewünschten Modus stellen — der Browser merkt sich die Wahl im
   LocalStorage.
5. Username im Browser-Passwort-Manager speichern lassen, **Passwort
   nicht speichern**. Begründung: das Passwort ist der einzige
   Sicherheits-Anker zwischen einem kompromittierten VNC-Zugriff und
   einem aktiven IBKR-Login.
6. Browser-Tab als „Pinned" markieren, damit er bei Browser-Restart
   automatisch wieder aufgemacht wird. Browser auf dem Pi-Desktop
   offen lassen.

## Recovery-Workflow (alltäglich)

Sobald der Stack einen frischen Login braucht (z.B. nach
`docker compose restart cpgateway`, IBKR-Session-Kick,
`auth_status != ok` im `/v1/internal/health`-Snapshot):

1. **VNC-Verbindung zum Pi öffnen** (Port 5900, User cma).
2. **Login-Tab im Browser refreshen** (`Ctrl+R` / `F5`). Wenn die
   Session abgelaufen ist, kommt das leere Login-Formular; der Live-/
   Paper-Toggle steht noch wie zuletzt benutzt.
3. **Passwort tippen, Enter**. cpgateway zeigt nach erfolgreichem
   Login die Status-Seite, der `broker-gateway`-Tickle-Job hält die
   Session anschließend warm.

Verifikation (optional, vom Laptop oder vom Pi-Terminal):

```bash
# Snapshot via broker-gateway-API
curl -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
     http://cma-pi-1:4000/v1/internal/health  # Live
curl -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
     http://cma-pi-1:4001/v1/internal/health  # Paper
```

`auth_status: ok` ⇒ alles in Ordnung. `auth_status: auth_lost` direkt
nach dem Login deutet auf eine Bridge-Drift hin — dann zusätzlich
[`cpgateway-session-resume.md`](cpgateway-session-resume.md).

## Sicherheits-Hinweise

- **wayvnc bindet aktuell auf `*:5900`** (alle Interfaces). In einem
  vertrauenswürdigen LAN ist das vertretbar; sobald der Pi remote
  erreichbar wird (VPN, exposed Port), sollte der wayvnc-Bind auf
  `127.0.0.1:5900` umgestellt werden und der Zugriff ausschließlich
  über einen SSH-Tunnel erfolgen (`ssh -L 5900:127.0.0.1:5900 cma@cma-pi-1`).
  Das ist als optionale Folge-Karte vorgemerkt.
- **Der `cpgateway`-Port ist explizit auf `127.0.0.1` gebunden**
  (siehe `compose.yaml`). Niemals auf `0.0.0.0` umstellen — cpgateway
  hat keine eigene Auth-Schicht vor dem SRP-Login. Wer den Port
  remote erreichen will, geht ebenfalls über SSH-Tunnel
  (`ssh -L 5000:127.0.0.1:5000 cma@cma-pi-1`).
- **Passwort nicht im Browser speichern**, siehe Erst-Setup Schritt 5.

## Verwandte Runbooks

- [`cpgateway-login.md`](cpgateway-login.md) — vollständiger
  Erst-Login mit 2FA, gilt für Live und für jeden frisch erstellten
  cpgateway-Container.
- [`cpgateway-session-resume.md`](cpgateway-session-resume.md) —
  Resume-Pfad ohne 2FA, wenn der Container nur eine kurze Pause
  hatte.
- [`auto-login-paper-setup.md`](auto-login-paper-setup.md) — Auto-
  Login-Skeleton (deaktiviert; siehe Karte c824617e Phase-2a-Diagnose
  zur Begründung).
