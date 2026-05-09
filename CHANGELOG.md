# Changelog

Alle bemerkenswerten Aenderungen am Service. Format lose an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/) angelehnt;
SemVer in `pyproject.toml`.

## [1.34.1] — 2026-05-09 (Doku-Update v1-Spec + Architektur fuer TWS-Backend)

Karte 45b03110: reine Doku-Aktualisierung im Anschluss an den
TWS-Refactor (Karten 368ccdfe Spike, 8b1781d3 Container-Slot,
441b53db Adapter, 33cb35b1 Lifecycle). Konsumenten-API-Vertrag
unveraendert; kein Code-Verhalten betroffen; kein Pi-Deploy.

- `docs/api/v1.md`:
  - Header auf v1.34.1 mit Hinweis auf TWS-Refactor v1.32-v1.34.
  - Section 3.2 (Internal Health): Beispiel-Body um
    `auth_status_consumer` erweitert; Feld-Tabelle backend-aware
    formuliert (CP- vs TWS-Bedeutung pro Feld).
  - Section 3.2 Auth-Lifecycle in zwei Bloecke geteilt (CP-Tickle vs
    TWS-Heartbeat); 503-Verhalten verweist auf zentrale
    `is_session_unavailable`.
  - Section 3.2 ENV-Tabelle: `BG_BACKEND`, `BG_TWS_HEARTBEAT_SEC`,
    `BG_TWS_HOST`, `BG_TWS_PORT` ergaenzt; CP-Defaults belassen.
  - Section 9.4 (Events Stream Source): Hinweis, dass TWS-Bridge in
    Folgekarte 4 entsteht.
  - Section 11 (Internals): Heartbeat-/Reconnect-Bullets backend-aware.
  - Section 11.1 (ThrottleManager): Hinweis, dass der CP-spezifische
    Bucket-Layer im TWS-Pfad nicht greift; TWS-Pacing liegt bei
    `ib_async`.
  - Neue Section 14.1: TWS-Backend-Refactor v1.32-v1.34, Service-
    Schicht-Limitation und Drift-Status.
- `docs/02-architecture.md`:
  - Header-Stand auf v1.34.1.
  - Section 4.1 Compose-Stack-Diagramm um zweiten Backend-Pfad
    erweitert; Image-Tag auf `broker-gateway:1.34.1`; Pi-Desktop-
    Login-Runbook verlinkt; geplanter `tws`-Compose-Service erlaeutert.
  - Section 4.2 Repo-Layout: `auth_status.py` und `tws/`-Pfad
    ergaenzt; `cp/auto_login_*` als cp-spezifisch markiert.
  - Section 4.3 Healthchecks: Schema von `/v1/internal/health`
    backend-aware; `/v1/internal/tws-health` als TWS-Diagnose-Endpunkt
    aufgenommen.
  - Section 5 Lead-In: `cp/` und `tws/` als gleichberechtigte
    Adapter-Familien.
  - Section 6.4 Auto-Login: Backend-Hinweis vorangestellt - cp-spezifisch.
  - Section 7.4 WS-Lifespan: Backend-Hinweis vorangestellt - cp-spezifisch.
  - Section 11.2 offene Architektur-Fragen: TWS-Migration als laufender
    Refactor-Punkt mit Verweis auf Folgekarten 4 und 6.
- `README.md`:
  - Status-Block auf v1.34.1 mit TWS-Refactor-Zusammenfassung; Live-
    und Paper-Stack-Ports referenziert; Doku-Karte 45b03110 erwaehnt.
  - Container-Stack-Section um `BG_BACKEND`-Hinweis und Verweis auf
    Karte 6 (Hard-Cutover) ergaenzt.
  - Footer auf Version 1.34.1.
- `compose.yaml`: Image-Tag `broker-gateway:1.34.1`.
- `pyproject.toml` + `src/broker_gateway/__init__.py`: 1.34.0 -> 1.34.1.

**Out-of-Scope:** KanPrompt-Projekt-Instructions (in der DB) sind
nicht im Repo gepflegt - der Hinweis ist in der Karte als optionaler
Mini-Schritt vermerkt und kann separat ueber `mcp__kanprompt__update_project`
nachgezogen werden, gehoert aber nicht zur Code-Basis dieser Karte.

## [1.34.0] — 2026-05-09 (TWS-Lifecycle, Feature-Flag BG_BACKEND)

Karte 33cb35b1 (Folge zur Adapter-Karte 441b53db) fuehrt einen
TWS-Backend-Lifecycle parallel zum cpgateway-Pfad ein. Die Wahl
erfolgt ueber das ENV-Flag `BG_BACKEND=cp|tws` (Default `cp`); der
Cutover auf `tws` als Default ist Migration-Karte 6. Konsumenten-
Schema bleibt stabil.

- `src/broker_gateway/auth_status.py` (neu): zentrales `AuthStatus`-
  Enum (sechs Werte: `ok`, `reauth_pending`, `auth_lost`, `cp_down`,
  `tws_down`, `session_lost`) plus `to_consumer_status` (Mapping auf
  `ok | down | lost`) und `is_session_unavailable` (503-Guard-Kontrakt).
  `cp.lifecycle.AuthStatus` re-exportiert von hier (Backward-Compat).
- `src/broker_gateway/tws/lifecycle.py` (neu):
  - `TWSLifecycle`: Heartbeat-Loop (Default 60 s, konfigurierbar via
    `BG_TWS_HEARTBEAT_SEC`) ueber `ib.isConnected()` + `ib.client.
    isReady()`. Nach `max_connect_failures` Connect-Fehlversuchen in
    Folge wechselt der Status auf `tws_down`. `tick_once()` fuer Tests
    + manuellen Trigger, `start()`/`stop()` mit asyncio.Task,
    Async-Context-Manager.
  - `TWSLifecycleCpAdapter`: wrappt `TWSLifecycle` ueber dasselbe
    Interface wie `cp.AuthLifecycle`. Wird im `BG_BACKEND=tws`-Pfad
    unter `app.state.cp_lifecycle` gehaengt, damit alle bestehenden
    Endpunkte (`require_session_ok`, `/v1/internal/health`,
    `/v1/status`) ohne Refactor weiterlaufen. Karte 6 entfernt diese
    Bruecke beim Hard-Cutover.
- `src/broker_gateway/config.py`: neue `backend_kind()`-Funktion fuer
  `BG_BACKEND` (Default `cp`, ungueltige Werte fallen mit Warning auf
  `cp` zurueck).
- `src/broker_gateway/cp/lifecycle.py`: `AuthStatus` aus
  `auth_status.py` importiert (statt selbst definiert);
  `require_session_ok` nutzt `is_session_unavailable` als zentrale
  Quelle und greift damit auch fuer `tws_down`/`session_lost`.
- `src/broker_gateway/api/v1/internal_health.py`:
  `InternalHealthResponse` um `auth_status_consumer: ok | down | lost`
  (stabiler Konsumenten-View) erweitert. Field `auth_status` bleibt
  rohes Enum (Backward-Compat fuer Operations-Tools).
- `src/broker_gateway/main.py`: `BG_BACKEND`-Switch im Lifespan baut
  entweder `AuthLifecycle` (CP) oder `TWSLifecycle` + Adapter (TWS).
  Im TWS-Pfad wird der `TWSClient` zusaetzlich unter
  `app.state.tws_client` gehaengt, damit `/v1/internal/tws-health`
  funktioniert. `_maybe_attach_auto_login` skipped bei TWS-Backend
  (Auto-Login ist cpgateway-spezifisch).
- `compose.yaml`: Image-Tag auf `broker-gateway:1.34.0`, `BG_BACKEND`
  + `BG_TWS_HEARTBEAT_SEC` als optionale Variablen dokumentiert.
- Tests (neu, gesamt 54 Tests):
  - `tests/test_auth_status.py`: Enum-Werte, `to_consumer_status`,
    `is_session_unavailable`, Backward-Compat.
  - `tests/test_tws_lifecycle.py`: Snapshot, alle State-Uebergaenge,
    Connect-Failures, IsReady-Defensive, Start/Stop, Heartbeat-Loop,
    CP-Adapter, FastAPI-Dependency. Coverage 99% fuer
    `tws/lifecycle.py`, 100% fuer `auth_status.py`.
  - `tests/test_main_backend_switch.py`: `backend_kind()`-ENV-Logik,
    CP-/TWS-Lifecycle-Wahl, Schema-Paritaet `/v1/internal/health`,
    owned-lifecycle-Branche mit gemocktem `TWSClient`.
- Doku:
  - `docs/02-architecture.md`: neue Sektion 5.1 "TWS-Backend-Adapter".
  - `README.md`: Sektion "Auth-Lifecycle" um TWS-Pfad und neue
    ENV-Variablen erweitert.

**Out-of-Scope (Folgekarten):** Order-Routing ueber TWS
(`read_only=False`), TWS-Faehigkeit der Service-Schicht
(`PortfolioService`, `OrdersService`, `QuotesService`) - bis Karte 4
(Single-Owner-Coordination) bzw. Karte 6 (Hard-Cutover). Unter
`BG_BACKEND=tws` sind nur `/v1/internal/health`, `/v1/health` und
`/v1/internal/tws-health` funktional. Kein Pi-Deploy aus dieser Karte.

## [1.33.0] — 2026-05-09 (TWS-Adapter Read-Only-Pfade)

Karte 441b53db (Folge zur Container-Slot-Karte 8b1781d3) implementiert
die Python-Bindings fuer die IBKR-TWS-API ueber `ib_async==2.1.x`. Der
neue Adapter `broker_gateway.tws.TWSClient` lebt parallel zum
bestehenden `CPGatewayClient` - die `cp/`-Modulstruktur bleibt
unveraendert (Rollback-Faehigkeit), Cutover erfolgt in einer spaeteren
Karte. Versions-Sprung 1.31.1 -> 1.33.0 weil 1.32.0 fuer PR #14
(Container-Slot) reserviert ist.

- `pyproject.toml`: `ib-async>=2.1,<2.2` als runtime-Dependency.
- `src/broker_gateway/tws/` (neu):
  - `client.py`: `TWSClient` mit Lifecycle (`connect`/`disconnect`,
    Async-Context-Manager) und sechs Read-Methoden (`account_summary`,
    `positions`, `qualify`, `historical_bars`, `market_snapshot`,
    `market_stream`). `ClientIdPool` (asyncio.Queue, Default-Range
    100..199) reserviert pro Adapter-Instanz eine eindeutige clientId.
  - `types.py`: Pydantic-Modelle (`AccountField`, `Position`, `Bar`,
    `Snapshot`, `Tick`) mit `from_*`-Mapping-Funktionen. Decimal-
    Disziplin fuer alle Geldbetraege und Mengen, UTC-Normalisierung
    der Timestamps. `nan` aus ib_async-Tickern wird auf `None`
    abgebildet.
  - `contracts.py`: Helper fuer `Stock`, `Forex`, `Future`-Contracts.
- `src/broker_gateway/api/v1/internal_tws_health.py` (neu): GET
  `/v1/internal/tws-health` (Scope `admin:*`) liefert Adapter-Status
  (connected, host, port, paper, read_only, client_id, checked_at).
  Default-Dependency raised 503 `tws_not_configured`, solange der
  Adapter nicht via `create_app(tws_client=...)` injektiert ist - das
  ist der Production-Default bis zum Cutover.
- `src/broker_gateway/main.py`: `create_app()` bekam `tws_client`-
  Parameter; bei Injektion wird die `get_tws_client`-Dependency
  ueberschrieben. Lifespan macht bewusst KEIN Auto-Connect (bis zum
  Cutover ist nicht garantiert, dass der TWS-Container im Stack
  laeuft - ein hartes connect waehrend Service-Startup wuerde den
  ganzen broker-gateway blockieren).
- `tests/test_tws_client.py`, `tests/test_tws_types.py`,
  `tests/test_tws_contracts.py`, `tests/test_tws_internal_health.py`
  (alle neu): 90 Tests; Coverage `src/broker_gateway/tws/` = 97 %.
  Disziplin-Test verifiziert, dass alle Read-Methoden Coroutinen
  zurueckgeben (kein Sync-Wrapping); ein zweiter Test belegt, dass
  `asyncio.run()` im laufenden Loop einen RuntimeError wirft (Loop-
  Schutz).

Out-of-Scope (Folge-Karten):
- Order-Routing (`read_only=False`, Submit/Cancel/Modify/Status-Stream).
- Lifecycle-Anpassung im `AuthLifecycle`-Modul fuer den TWS-Pfad.
- v1-Spec-Drift gegen die TWS-Realitaet abgleichen.
- Single-Owner-Coordination zwischen TWS- und CP-Adapter.
- Hard-Cutover (`cp/` entfernen, gateway-Service zeigt nur noch auf
  `TWSClient`).

Kein Pi-Deploy aus dieser Karte heraus.

## [1.31.1] — 2026-05-08 (Doku: Fork-Evaluation cpgateway-Wrapper)

Karte a78431aa (Time-Boxed Spike) hat zwei Open-Source-Wrapper als
moegliche Workaround-Pfade fuer den Tarball-Auth-Bug evaluiert
(`ppaanngggg/ib-cp-server`, `schuss-capital/ib-client-docker`). Beide
nutzen denselben 2023er IBKR-Tarball intern und bringen keinen
eigenen Login-Stack — Empfehlung: keinen Fork adoptieren. Phase 2
(Smoke auf cma-pi-1) wurde nach eindeutiger Inventur uebersprungen.

- `docs/runbooks/cpgateway-login.md`: neues Kapitel "Alternative
  Container-Wrapper" mit Befund-Tabelle und Begruendung.

## [1.31.0] — 2026-05-07 (Cookie-Bridge Browser → Service)

Karte 406fce15 schliesst die Luecke zwischen Pi-Browser-Login (Karte
739777a9, nsenter-Variante) und Service-Cookie-Jar: cpgateway setzt
JSESSIONID + x-sess-uuid mit `Path=/sso`, der Service ruft aber
`/v1/api/*` auf — ohne Override greift das Cookie-Path-Matching nicht
und alle authenticated Endpoints liefern 401, selbst wenn die
Browser-Session perfekt etabliert ist.

- `src/broker_gateway/cp/client.py`: `CPGatewayClient` akzeptiert
  optionalen `cookies=httpx.Cookies()`-Parameter, exponiert die Cookies
  via Property `client.cookies` als Shared State mit dem httpx-Jar
  (Phase A). Response-event_hook
  `_force_root_path_on_session_cookies` schreibt jeden Set-Cookie aus
  der aktuellen Response, dessen Path != `/` ist, im Client-Jar auf
  `Path=/` um (Phase B). Greift nur fuer Cookies, die in dieser
  Roundtrip-Reaktion vom Server kamen — fremde Hosts im Jar bleiben
  unangetastet.
- `src/broker_gateway/cp/lifecycle.py`: `AuthLifecycle.client`-Property
  als read-only Zugriff auf den Lifecycle-eigenen CPGatewayClient
  (Voraussetzung fuer Phase C).
- `src/broker_gateway/api/v1/internal_seed_cookies.py` (neu): `POST
  /v1/internal/seed-cookies` mit Scope `admin:*`. Body
  `{"jsessionid", "x_sess_uuid", "host"?, "ssodh_init"?}` — Cookies
  werden mit `Path=/` in den Lifecycle-Client und (falls separat) in
  den Services-Client geseedet. Default ruft der Endpoint anschliessend
  `/iserver/auth/ssodh/init` mit `{"keepAlive": true}` auf (Phase D);
  bei Fehler bleibt der Phase-B-Path-Override als Fallback. Zum
  Schluss `lifecycle.tick_once()` -> der naechste internal/health-
  Snapshot zeigt sofort den frischen Auth-Status.
- `tests/test_cp_client_cookies.py` (neu): 9 Tests fuer Phase A + B
  (Roundtrip auth_status -> tickle inkl. Cookie-Header).
- `tests/test_admin_seed_cookies.py` (neu): 12 Tests fuer Phase C + D
  (401/403/422, Jar-Befuellung, auth_status=ok, tick-Trigger,
  Services-Client-Sync, ssodh/init-on/off/error).
- `tests/cp_mock/replay.py`: Stub-Route fuer
  `POST /iserver/auth/ssodh/init` mit toggleablem `ssodh_init_should_fail`.

Funktional ist v1.31.0 die Loesung fuer das in Karte 739777a9 zuletzt
beschriebene Symptom *"Client login succeeds, aber Service bleibt
auth_status=cp_down"*: nach dem Pi-Browser-Login traegt der Operator
JSESSIONID + x-sess-uuid (Browser-DevTools) per `curl POST
/v1/internal/seed-cookies` ein, der Service-Lifecycle springt
typischerweise innerhalb 1-2 Tickle-Zyklen auf `auth_status=ok`.

Test-Stand: 725 passed, 4 skipped (713 vor Karte 406fce15 + 12 neue).

## [1.30.0] — 2026-05-06 (Pi-Desktop Quick-Login fuer cpgateway)

Auto-Login-PoC mit IBeam (Karte c824617e) hat ARM64-Browser-Stabilitaet
gezeigt, scheiterte aber am IBKR-Submit-Pfad: Form wird nach Submit
zurueckgesetzt, ohne sichtbare Fehlermeldung — passt zum Phase-B-Symptom
mit Playwright. Statt einer weiteren Iteration auf Headless-Login
macht v1.30.0 den manuellen Recovery-Pfad bequem.

`cpgateway` exponiert auf cma-pi-1 jetzt einen lokalen 127.0.0.1-Bind
(Live `:5000`, Paper `:5001`), so dass ein Browser auf der Pi-Desktop-
Session den Login direkt ueber `localhost` abwickelt — VNC oeffnen,
Login-Tab refreshen, Passwort tippen.

- compose.yaml: cpgateway-Service erhaelt
  `127.0.0.1:${BG_CPGATEWAY_HOST_PORT:-5000}:5000`. Bind explizit lokal,
  cpgateway hat keine eigene Auth-Schicht vor dem SRP-Login.
- ops/build-gateway.sh: setzt `BG_CPGATEWAY_HOST_PORT` auf 5000 (Live)
  bzw. 5001 (Paper), damit beide Stacks parallel ohne Konflikt
  erreichbar sind.
- compose.paper.auto-login.yaml: `BG_AUTO_LOGIN_IMAGE`-Default zieht
  auf `:1.30.0` mit (Memory: image-Tag = Service-Version).
- docs/runbooks/cpgateway-pi-desktop-login.md (neu): Voraussetzungen,
  Erst-Setup, Recovery-Workflow in drei Schritten, Sicherheits-Hinweis
  zu wayvnc-Bind 0.0.0.0:5900.

Auto-Login bleibt deaktiviert (`BG_PAPER_AUTO_LOGIN=0`), Trigger,
Throttle, Admin-Endpoint und LifecycleSnapshot-Felder unveraendert.
Branch `feature/auto-login-ibeam` (Karte c824617e Phase 2a) bleibt
lokal als Upstream-PR-Baseline und Diagnose-Spur, NICHT in main.

Live-Stack-Restart wurde mit dieser Version NICHT ausgeloest (vernichtet
laufende Session). Der naechste ohnehin faellige Recovery-Login bringt
den Live-Bind automatisch mit.

Karte: `91283e58-14c8-49aa-b072-3ee98f537a10`.

## [1.29.3] — 2026-05-05 (Phase-B-Hotfix — Live/Paper-Toggle vor Submit)

Live-Smoke 3 nach v1.29.2 hat den Sidecar 32s laufen lassen, dann
Timeout mit "no /sso/Dispatcher response within timeout". Diagnose-
Lauf zeigte den eigentlichen Grund: cpgateway hat nach Container-
Recreate den Default-Mode auf "Live" stehen, auch wenn der
konfigurierte User ein Paper-Account ist. Server lehnt mit Banner
"You have selected the Live Account Mode, but the specified user is
a Paper Trading user. Please select the correct Login mode." ab,
ohne `/sso/Dispatcher` zu triggern.

Die ursprueengliche HAR-Aufzeichnung aus Phase 1.b zeigt diesen
Wechsel als `LOGIN_TYPE=1 -> LOGIN_TYPE=2` zwischen den
`/sso/Authenticator`-Calls — der manuelle Login hatte zwischendrin
den Toggle geklickt.

### Geaendert
- `ops/auto-login/auto_login.py` klickt vor Username/Password-Fill
  den Live/Paper-Toggle (`.loginformWrapper .toggle-label:has-text(
  "Paper")`) und wartet 800 ms, damit das xyz-Bundle den naechsten
  `/sso/Authenticator`-Init mit `LOGIN_TYPE=2` rausschicken kann.
  Toggle-Klick ist defensiv: wenn das Element nicht da ist (z.B.
  paper-only-Build), bleibt der Login wie bisher.
- `compose.paper.auto-login.yaml` hebt den Default-Image-Tag fuer
  den Sidecar auf `:1.29.3` an.

## [1.29.2] — 2026-05-05 (Phase-B-Hotfix — Chromium-Container-Args)

Live-Smoke 3 nach v1.29.1 hat den Sidecar erstmals tatsaechlich
gestartet — Chromium crashed dann beim Form-Submit mit
"Page crashed". Klassischer Container-Pattern: das Default
`/dev/shm` im Container ist 64MB, Chromium-Renderer braucht mehr und
schiesst sich beim Allokieren.

### Geaendert
- `ops/auto-login/auto_login.py` ergaenzt die Chromium-Launch-Args
  um `--disable-dev-shm-usage`, `--disable-gpu` und
  `--disable-software-rasterizer`. Das erzwingt `/tmp` als Shared-
  Memory-Verzeichnis (etwas langsamer, aber stabil) und vermeidet
  den ueberfluessigen GPU/Rasterizer-Pfad auf dem Pi.
- `compose.paper.auto-login.yaml` hebt den Default-Image-Tag fuer
  den Sidecar auf `:1.29.2` an, damit Build und Aufruf wieder
  konsistent sind.

## [1.29.1] — 2026-05-05 (Phase-B-Hotfix — docker-CLI im gateway-Image)

Live-Smoke 3 auf cma-pi-1 hat einen Bug aufgedeckt: das gateway-Image
hat zwar den `docker.sock`-Mount, aber kein `docker`-CLI-Binary. Der
Auto-Login-Trigger schlug deswegen mit Exit-Code 9 (`docker binary
not found`) fehl, ohne dass je ein Sidecar startete.

### Geaendert
- `Dockerfile` zieht via Multi-Stage-Copy das `docker`-Binary aus
  `docker:27-cli`. Das Image wird damit ca. 50 MB groesser, hat aber
  keinen dockerd-Daemon (nur das CLI-Binary). Im Live-Stack bleibt
  der Pfad ohnehin unbenutzt (Hard-Guard 3 verhindert den
  `docker.sock`-Mount).

## [1.29.0] — 2026-05-05 (Karte ece90a8e Phase B — Sidecar-Image + Verdrahtung)

Phase B der Karte ece90a8e (*Paper-Stack Auto-Login via Headless-
Chromium-Sidecar*) liefert das Sidecar-Image, den echten
Subprocess-Runner, die Lifecycle-Verdrahtung und den Admin-Endpoint
zur manuellen Aktivierung. Damit ist der Auto-Login-Pfad funktional —
Phase 1 (HAR + SRP-6-Befund) und Phase A (Skeleton + Hard-Guards)
sind bereits gemerged.

### Neu
- ``ops/auto-login/Dockerfile`` (basiert auf
  ``mcr.microsoft.com/playwright/python``, ARM64-faehig),
  ``requirements.txt``, ``auto_login.py`` (Playwright-Flow auf
  Basis der HAR-Befunde aus Phase 1 — wartet auf
  ``POST /sso/Dispatcher`` mit Body ``Client login succeeds``) und
  ``auto_login_logic.py`` (browser-unabhaengige Hilfen:
  ``mask_username``, ``is_paper_target``, ``classify_dispatcher``,
  ``emit_log``).
- ``broker_gateway.cp.auto_login_runner.DockerSubprocessAutoLoginRunner``:
  echter ``AutoLoginRunner`` via ``docker run --rm`` als
  ``asyncio.create_subprocess_exec``-Aufruf. Reicht
  ``BG_PAPER_USERNAME``/``_PASSWORD`` als ``-e VAR``-Form (ohne
  Wert) weiter, sodass der Klartext NICHT in ``ps``-Listings landet.
  Timeout-, FileNotFoundError- und nicht-null-Exit-Behandlung sind
  abgedeckt.
- ``AuthLifecycle.attach_auto_login_trigger(trigger)`` + interne
  ``_maybe_invoke_auto_login``-Hook. Wird in ``_handle_auth_loss``
  (Reauth-Loop erschoepft → ``AUTH_LOST``) und im CP_DOWN-Branch
  von ``tick_once`` aufgerufen. Trigger-Exceptions werden geloggt,
  brechen aber den Tickle-Loop nicht ab.
- ``main.py`` haengt im Lifespan einen ``AutoLoginTrigger`` an, wenn
  ``BG_STACK_KIND=paper`` UND ``BG_PAPER_AUTO_LOGIN=1``. Image-Tag,
  Network und Target-URL sind ueber ``BG_AUTO_LOGIN_*``-Env-Vars
  konfigurierbar.
- ``POST /v1/admin/auto-login/trigger`` (Scope ``admin:*``):
  manueller Anstoss, respektiert dieselben Hard-Guards und Throttle.
  Liefert HTTP 503 ``auto_login_disabled``, wenn der Trigger nicht
  attached ist.
- ``compose.paper.auto-login.yaml`` als Compose-Override fuer den
  Paper-Stack: mountet ``/var/run/docker.sock`` und reicht die
  Auto-Login-Vars in den ``gateway``-Service. Wird automatisch von
  ``ops/build-gateway.sh --env=paper`` eingehaengt; Live-Compose
  bleibt unveraendert (Hard-Guard 3).
- ``ops/build-gateway.sh`` baut bei ``--env=paper`` zusaetzlich das
  Sidecar-Image (``--platform linux/arm64``) und liest
  ``/etc/default/broker-gateway-paper`` ein.
- ``docs/runbooks/auto-login-paper-setup.md``: Schritt-fuer-Schritt-
  Runbook fuer Pi-Setup, Smoke-Tests, Aktivierung und Rollback.

### Tests
- ``tests/test_auto_login_sidecar_logic.py``: 17 Tests fuer die
  browser-unabhaengige Sidecar-Logik (Username-Maskierung,
  Hard-Guard-URL-Check, Dispatcher-Classification, JSON-Logger).
- ``tests/test_auto_login_runner.py``: 12 Tests fuer
  Subprocess-Argument-Aufbau, Env-Mapping, Exit-Code-Mapping,
  Timeout-Handling.
- ``tests/test_admin_auto_login.py``: 5 Tests fuer den neuen
  Admin-Endpoint (Auth-Schutz, Scope-Check, Skipped/Success-
  Outcome).
- ``tests/test_cp_lifecycle.py``: 3 zusaetzliche Tests fuer die
  Trigger-Verdrahtung (greift bei ``AUTH_LOST``, greift NICHT bei
  ``OK``, schluckt Exceptions).

### Bewusst noch nicht enthalten
- Live-Smokes 2 + 3 auf cma-pi-1 (zwingend nach dem Merge dieses PR;
  Runbook in ``docs/runbooks/auto-login-paper-setup.md``).
- AppArmor- bzw. Seccomp-Profil fuer den ``docker.sock``-Mount —
  dokumentierte Restrisiko-Akzeptanz im Paper-Stack
  (``docs/04-security.md`` Sektion 7.4).

## [1.28.0] — 2026-05-05 (Karte ece90a8e Phase A — Auto-Login-Skeleton fuer Paper-Stack)

Phase A der Karte ece90a8e (*Paper-Stack Auto-Login via Headless-
Chromium-Sidecar*) liefert das Code-Skelett ohne den eigentlichen
Sidecar — der folgt in Phase B. Phase 1 (Reverse-Engineering, HAR +
SRP-6-Befund) wurde mit PR #5 gemerged.

### Neu
- ``BG_STACK_KIND={live|paper}`` ist Pflicht-Env beim Lifespan-Start.
  Fehlt der Wert oder ist er ungueltig, bricht der Service mit
  ``ConfigError`` ab. ``ops/build-gateway.sh`` exportiert ihn defensiv
  abhaengig vom ``--env=``-Schalter.
- ``broker_gateway.config.validate_runtime_config`` als zentrale
  Hard-Guard-Pruefung beim Startup:
  1. ``BG_STACK_KIND=live`` + ``BG_PAPER_AUTO_LOGIN=1`` → Startup-Fail.
  2. ``BG_STACK_KIND=live`` + ``BG_PAPER_USERNAME``/``_PASSWORD``
     gesetzt → Startup-Fail (verhindert stillen Drift).
  3. ``BG_PAPER_AUTO_LOGIN=1`` ohne Credentials → Startup-Fail.
- ``LifecycleSnapshot`` um vier Auto-Login-Felder erweitert:
  ``last_auto_login_attempt_at``, ``last_auto_login_success_at``,
  ``auto_login_failures_total``, ``auto_login_throttle_state``.
  Default-Werte (None / 0 / "ready") halten Backwards-Kompat.
- ``broker_gateway.cp.auto_login_throttle.AutoLoginThrottle``: clock-
  injectable Throttle mit konservativen Limits (max 1/5min, max 3/h,
  max 5/Tag, Backoff 5/15/45 min nach Fehlschlag, Sticky-Stop bei
  2FA-Detection).
- ``broker_gateway.cp.auto_login_trigger.AutoLoginTrigger``: Skeleton
  mit injizierbarem ``AutoLoginRunner`` (Phase B liefert den echten
  docker-SDK-Runner). Pre-Conditions (enabled? stack_kind? auth_status
  in {auth_lost, cp_down}? throttle erlaubt?) werden zentral geprueft.
- ``GET /v1/internal/health`` liefert die Auto-Login-Felder zusaetzlich
  zur Bridge-Probe und den bestehenden Lifecycle-Daten.
- ``.env.live.template``/``.env.paper.template``: ``BG_STACK_KIND`` als
  Pflicht-Variable, Paper-Template um ``BG_PAPER_AUTO_LOGIN=0`` und
  Hinweise zu ``BG_PAPER_USERNAME``/``_PASSWORD``.

### Geaendert
- ``main.py`` ruft ``validate_runtime_config`` im Lifespan-Block
  (nicht im ``create_app``-Body), damit Modul-Level-Imports ohne
  gesetzte Env weiter klappen — die Validation-Pruefung trifft erst
  beim ersten echten Lifespan-Run.
- ``tests/conftest.py`` setzt ``BG_STACK_KIND=paper`` als autouse-
  Default und entfernt ``BG_PAPER_*``-Vars, damit Tests nicht
  versehentlich am neuen Hard-Guard scheitern.

### Bewusst noch nicht enthalten (Phase B)
- ``ops/auto-login/`` (Sidecar-Image + ``auto_login.py`` mit Playwright).
- Echter docker-SDK-``AutoLoginRunner`` in ``cp/auto_login_trigger.py``.
- Lifecycle-Hook, der den Trigger nach ``CP_DOWN``/``AUTH_LOST``
  auch tatsaechlich aufruft (Phase A bleibt das Skeleton ohne
  Verdrahtung in den Tickle-Loop).
- ``POST /v1/admin/auto-login/trigger`` (manueller Anstoss).
- Live-Smokes 2 + 3 auf cma-pi-1.

## [1.27.1] — 2026-05-05 (Karte 12e04c98 Drift-Fix — Live-Schema-Anpassung ISIN-Pfad)

Zwei Schema-Drifts zwischen Mock und Live, die der Live-Smoke gegen
U25235077 aufgedeckt hat:

### Geaendert
- ``_entry_exchange_code`` (Helfer im Adapter) erkennt jetzt zusaetzlich
  Klammer-Suffix-Notation ``"SAP SE (IBIS)"`` aus dem ``companyHeader``.
  Live-CP setzt beim ISIN-Pfad (``name=true``) ``description=null`` und
  packt das Exchange-Kuerzel in Klammern. Bisheriger Bindestrich-Pfad
  ``"SAP SE - IBIS"`` (symbol-Pfad) bleibt als Fallback.
- ``InstrumentsService.search_by_isin`` normalisiert die CP-Antwort
  ``{"error": "No contracts found"}`` (Object statt Liste) auf leeres
  Array + HTTP 200. Karten-Vertrag verlangt ``[]`` bei unbekannter ISIN.
- ``ReplayCPGatewayMock`` ``_ISIN_LISTINGS`` an Live-Schema kalibriert:
  3 Cross-Listings (IBIS, EBS, MEXI) statt 2, ``description=null``,
  ``companyHeader="SAP SE (IBIS)"``, ``secType`` Top-Level. No-Match
  liefert jetzt den Error-Wrapper.

## [1.27.0] — 2026-05-05 (Karte 12e04c98 — ISIN-Filter auf /v1/instruments/search)

Marktneutraler Instrumenten-Lookup per ISIN. Trigger ist Karte
dc9577d0 in trading_robot, die einen ISIN-basierten Conid-Resolver
einfuehrt; mit dem Server-seitigen Filter kann der Bot den Stub +
preload_isin-Cache abloesen und exchange-blind die richtige IBKR-conid
finden.

### Hinzugefuegt
- `GET /v1/instruments/search` akzeptiert zusaetzlich zum bisherigen
  ``?symbol=&exchange=`` jetzt auch ``?isin=&mic=`` (mutually
  exclusive). Format-Validation ueber ISO-6166-Regex (12 Zeichen:
  2 Land + 9 alphanumerisch + 1 Pruefziffer); ungueltiges Format -> 422.
- Antwort-Items im Schema ``Instrument`` um Feld ``isin: str | null``
  ergaenzt. Symbol-Pfad: ``null`` (CP liefert keine ISIN). ISIN-Pfad:
  Echo des Query-Werts in jedem Cross-Listing-Eintrag.
- ``InstrumentsService.search_by_isin(isin, mic=None)`` mit eigenem
  TTL-Cache (Default 7 Tage), Cache-Key ``(isin, mic)``. Mappt intern
  auf ``/iserver/secdef/search?symbol={isin}&name=true&secType=STK``.
- Optionaler ``?mic=`` (ISO 10383, z.B. ``XETR``/``XNYS``) filtert
  Cross-Listings auf eine Boerse. Ohne MIC bleibt die CP-Reihenfolge
  unveraendert (IBKR sortiert nach interner Liquiditaets-Heuristik).
- ``ReplayCPGatewayMock`` erkennt ``name=true`` und liefert
  Cross-Listings aus einer ISIN-Tabelle (synthetisch aus dem
  bestehenden SAP-Recording zusammengestellt; echte Live-Recordings
  folgen, sobald die IBKR-Session wieder verfuegbar ist).
- 14 neue Tests in ``tests/test_instruments.py``: Happy-Path,
  Caching, MIC-Filter, ungueltiges Format, Lowercase-Normalisierung,
  Mutual-Exclusion symbol/isin, MIC-ohne-ISIN, exchange-mit-ISIN.
- ``docs/api/v1.md`` Section 4.1 dokumentiert beide Pfade inkl.
  Cross-Listing-Hinweis und MIC-Disambiguation.

### Bekannte offene Punkte
- Live-Smoke gegen ``http://cma-pi-1:4000/v1/instruments/search?isin=DE0007164600``
  steht noch aus; beide Pi-Stacks waren beim Implementieren auf
  ``auth_status=cp_down``. Karte ist als ``partial`` markiert, der
  Live-Test wird nach Wiederherstellung der IBKR-Session nachgezogen.

## [1.23.0] — 2026-05-02 (AP-11 K8 — /v1/status + 150-Symbol-Stresstest-Skelett, AP-11 Code-komplett)

Status-Endpoint plus Stresstest-Skelett. Damit ist AP-11 (WS-Adapter
Implementation) Code-seitig komplett - alle 8 Karten der Phasen A
und B sind gemerged und auf cma-pi-1 deployed. Die Live-Aktivierung
des WS-Pfads (CPWebSocketClient im FastAPI-Lifespan + Default-Flip
auf `BG_QUOTES_SOURCE=ws` + Live-Smoke gegen U25235077) bleibt als
einzelne K3-Folgekarte offen; bis dahin laeuft `/v1/quotes/stream`
weiter ueber den Polling-Pfad und der Status-Endpoint zeigt
``last_frame_age_seconds=null`` plus ``subscriptions_active=0``.

### Hinzugefuegt
- `src/broker_gateway/api/v1/status.py` mit ``GET /v1/status``,
  Scope ``instruments:read``. Felder: ``cp_gateway_connected``,
  ``last_frame_age_seconds``, ``reconnect_attempt``,
  ``subscriptions_active``. Klasse ``StatusProbe`` haelt einen
  monotonischen Last-Frame-Marker (``mark_frame()``) und liest die
  drei anderen Felder ueber Lifecycle-/Registry-/Reconnect-
  Callbacks.
- `src/broker_gateway/cp/ws_client.py` ``CPWebSocketClient``
  bekommt ein public ``reconnect_attempt: int``-Attribut, das im
  Backoff-Loop hochgezaehlt und nach erfolgreichem Reconnect auf
  0 zurueckgesetzt wird.
- `src/broker_gateway/main.py` instanziiert ``StatusProbe`` im
  Lifespan und registriert den Dependency-Override.
- `tests/test_status_endpoint.py` mit 6 Tests: Endpoint-Schema,
  Cold-Start-Werte (None / 0 / 0), Scope-403 ohne
  ``instruments:read``, Probe-Mark-Frame, Probe-Registry-Count,
  Probe-Reconnect-Callback.
- `tests/integration/test_smd_stresstest_150.py` als Skelett mit
  Markern ``live`` + ``stresstest`` (default deselected).
- `docs/runbooks/smd-stresstest.md` als Runbook fuer den manuellen
  Stresstest gegen U25235077.
- `pyproject.toml` ``[tool.pytest.ini_options]`` deselected
  default ``live`` und ``stresstest`` Marker, registriert die drei
  Marker (``live``, ``stresstest``, ``integration``) ohne
  PytestUnknownMarkWarning.

### AP-11 Status nach K8
- Phase A: K1 (SmdTopicAdapter), K2 (SubscriptionRegistry +
  Reconnect-Hook), K3 (WSPushSource + ENV-Schalter), K4
  (CalendarService + /v1/exchanges), K5 (Tradeability-Felder).
- Phase B: K6 (sor-Adapter + /v1/orders/stream), K7 (WS-Egress
  /v1/quotes/ws + /v1/orders/ws), K8 (/v1/status + Stresstest-
  Skelett).
- Offen: K3-Folgekarte (Lifespan-Wiring + Live-Smoke).

---

## [1.22.0] — 2026-05-02 (AP-11 K7 — WS-Egress /v1/quotes/ws + /v1/orders/ws)

WebSocket-Egress als zweites Ziel-Tier neben SSE. Beide Endpunkte
schoepfen aus demselben StreamHub wie die SSE-Pendants und liefern
das gleiche Frame-Schema, lediglich gewrappt in ein JSON-Objekt
``{id, event, data}`` (kein SSE-Header-Format).

### Hinzugefuegt
- `src/broker_gateway/api/v1/ws_auth.py` mit
  ``authenticate_websocket(websocket, store, required_scope)``.
  Akzeptiert Bearer-Token aus drei Quellen: Authorization-Header,
  Sec-WebSocket-Protocol als ``bearer.<token>`` (Browser-Pattern;
  Subprotokoll wird im accept echoed), Query-Param ``?token=``.
  Bei Auth-Fehler wird der Socket mit Code 1008 (Policy Violation)
  geschlossen.
- `src/broker_gateway/api/v1/quotes_ws.py` mit `/v1/quotes/ws`-
  WebSocket-Endpoint, Scope ``quotes:read``. Frames werden als
  Text-Frames im Wrapper-Format gesendet, Quelle ist der
  ``SubscriptionManager`` (gleicher StreamHub wie ``/v1/quotes/stream``).
- `src/broker_gateway/api/v1/orders_ws.py` mit `/v1/orders/ws`-
  WebSocket-Endpoint, Scope ``orders:read``. Bootstrap-Frame plus
  Live-Push aus dem ``OrdersBroadcaster``.
- `tests/test_quotes_ws_endpoint.py` mit 5 Tests: Auth via Header,
  Auth via Query-Token, Auth via Sec-WebSocket-Protocol mit
  Subprotokoll-Echo, Token fehlt -> 1008, falscher Scope -> 1008.
- `tests/test_orders_ws_endpoint.py` mit 2 Tests: Token fehlt ->
  1008, Query-Token akzeptiert (Auth-Pfad isoliert).

### Naechste Schritte (AP-11 Phase B)
- K8: ``/v1/status``-Endpoint plus 150-Symbol-Stresstest.
- K3-Folge: Lifespan-Wiring fuer ``CPWebSocketClient`` plus Live-Smoke.

---

## [1.21.0] — 2026-05-02 (AP-11 K6 — sor-Adapter + OrdersBroadcaster + /v1/orders/stream)

Phase B startet. SorTopicAdapter normalisiert IBKR-sor-Frames auf
das Anhang-A-Schema, OrdersBroadcaster fan-outet pro Account mit
Bootstrap-Frame voraus und Live-Push danach. Neuer SSE-Endpoint
/v1/orders/stream mit Scope orders:read.

### Hinzugefuegt
- `src/broker_gateway/cp/topics/sor.py` mit `SorFrame` und
  `SorTopicAdapter`. Field-Mapping nach K6-Anhang A
  (orderId/cOID/parentId/acct/ticker/side/totalSize/filledQuantity/
  avgPrice/status/timeInForce/lastExecutionTime/orderRejectReason/
  conid). Status-Normalisierung: IBKR-Werte werden auf
  pending/accepted/partial_fill/filled/cancelled/rejected reduziert
  (defensiver Default ``pending`` fuer unbekannte Status).
  timeInForce-Quirk: ``CLOSE`` wird auf ``DAY`` gemappt.
  ``bgColor``/``fgColor`` werden gefiltert. Bootstrap-Pfad
  ``adapter.bootstrap(rest_orders)`` reicht REST-Bodies durch
  denselben Decode-Pfad.
- `src/broker_gateway/streams/orders.py` mit `OrdersBroadcaster`
  und ``OrderStreamEvent``: pro Account eine Subscription mit
  Refcount, asyncio-Queue und Ringpuffer fuer Last-Event-ID-
  Reconnect; Slow-Consumer-Drop analog zum Quotes-Pfad.
- `src/broker_gateway/api/v1/orders_stream.py` mit dem SSE-
  Endpoint und der `OrdersBootstrapLoader`-Klasse, die einmal
  beim Subscribe-Start ``GET /iserver/account/orders`` ruft und
  das Ergebnis durch den Adapter-Bootstrap-Pfad leitet.
- Neuer Scope `SCOPE_ORDERS_READ = "orders:read"` in
  `auth/models.py` (analog zu `quotes:read`).
- `main.py`-Lifespan instanziiert `OrdersBroadcaster`,
  `SorTopicAdapter` und `OrdersBootstrapLoader` und registriert die
  Dependency-Overrides. Router `/v1/orders/stream` wird im
  v1-Aggregations-Router eingebunden.
- `tests/test_topic_adapter_sor.py` mit 8 Tests: Field-Mapping,
  Status-Normalisierung-Lifecycle, timeInForce-CLOSE-Quirk,
  UI-Filter, Snapshot-Merge, Bootstrap-Pfad, Non-sor-Frame-None,
  fehlende order_id-None.
- `tests/test_orders_stream.py` mit 5 Tests: Bootstrap-Frame
  zuerst, Live-Frame nach Subscribe, Last-Event-ID-Replay-Skip,
  Publish ohne Subscriber stumm, Scope-403 ohne `orders:read`.

### Naechste Schritte (AP-11 Phase B)
- K7: WS-Egress-Endpunkte `/v1/quotes/ws` und `/v1/orders/ws`.
- K8: `/v1/status`-Endpoint plus 150-Symbol-Stresstest.
- K3-Folge: Lifespan-Wiring fuer `CPWebSocketClient` plus
  `WSPushSource` plus Live-Smoke gegen U25235077.

---

## [1.20.0] — 2026-05-02 (AP-11 K5 — Tradeability-Felder im smd-Frame, Phase A komplett)

`SmdTopicAdapter` reichert pro Frame zwei neue Felder an:
`is_tradeable_now: bool` und `current_session ∈ {rth, pre, post,
closed, halted}`. Wahrheits-Tabelle aus K6-Sektion 5.2; `exchange_id`
ergaenzt das Tripel. Ohne Calendar-Service-Verdrahtung bleibt der
Adapter zu 100 % rueckwaerts-kompatibel zu K1.

Damit ist die smd-Phase A des WS-Adapters Code-seitig komplett.
Live-Aktivierung (Lifespan-Wiring + Default-Flip auf `ws`) bleibt als
K3-Folgekarte offen.

### Hinzugefuegt
- `src/broker_gateway/cp/tradeability.py` mit reiner Funktion
  `derive_tradeability(now_utc, calendar, availability_code)`. Halted-
  Codes (`H*`/`Z*`/`Y*`) ueberstimmen den Schedule. R/D plus aktive
  Session liefert `is_tradeable_now=true` mit der Session als
  `current_session`. Feiertag und ausserhalb-Sessions liefert
  `closed`. Naive datetime wird mit TypeError abgelehnt - bewusst
  defensiv, weil Tradeability-Logik zeitzonenkritisch ist.
- `src/broker_gateway/cp/topics/smd.py` `SmdTopicAdapter.__init__`
  um optionale Parameter `calendar_service`, `conid_to_exchange`
  und `clock` erweitert. Neue Methode `preload_for_conid(conid)`
  loest pro conid einmalig die exchange_id auf, holt den Schedule
  aus dem CalendarService und legt beide in einem lokalen Cache.
  `feed()` bleibt sync und greift bei vorhandenem Cache auf
  `derive_tradeability` zurueck. Fehlt Calendar-Service oder
  Lookup, bleiben die drei Felder None.
- `src/broker_gateway/streams/ws_source.py` `subscribe_quotes`
  ruft `await adapter.preload_for_conid(conid)` vor dem
  Subscribe-Frame, damit der erste Push-Frame bereits Tradeability-
  Felder hat. Lookup-Fehler werden geloggt, nicht propagiert.
- `tests/test_tradeability_derivation.py` mit 13 Tests entlang der
  Wahrheits-Tabelle: rth/pre/post + R/D liefern (true, <session>),
  Halted/Frozen-Familie (`H*`/`Z*`/`Y*`) liefert (false, halted),
  Feiertag und ausserhalb-Sessions liefert (false, closed),
  Halbtages-Session-Ende greift, unbekannte Codes fallen defensiv
  auf closed, leerer Code in RTH liefert closed (statt true),
  naive datetime wirft TypeError, lowercase-Code wird normalisiert.
- `tests/test_topic_adapter_smd.py` um drei End-to-End-Tests
  erweitert: Adapter mit Fake-CalendarService befuellt die drei
  neuen Felder; ohne CalendarService bleiben sie None;
  None-Lookup laesst Tradeability stumm.
- `docs/06-glossary.md` Block 6509-Code ausgebaut mit `H*`-, `Z*`-,
  `Y*`-Praefixen plus dem Tradeability-Verknuepfungs-Block fuer
  AP-11 K5.

### Naechste Schritte
- AP-11 Phase B: K6 (sor-Adapter + REST-Bootstrap +
  /v1/orders/stream), K7 (WS-Egress), K8 (/v1/status + Stresstest).
- AP-11 K3-Folge (Lifespan-Wiring + Live-Smoke des WS-Pfads) bleibt
  parallel offen.

---

## [1.19.2] — 2026-05-02 (AP-11 K4 hotfix2 — Live-IBKR-Schema fuer trsrv/secdef/schedule)

Live-Smoke nach 1.19.1 zeigte: das echte CP-Gateway-Antwortschema
unterscheidet sich vom Karten-Anhang in zwei Punkten. (1) Die
Time-Zone heisst ``timezone`` (lowercase), nicht ``timeZoneId``.
(2) Die echten Trading-Slots liegen in ``tradingtimes[]``, das
``sessions``-Array bleibt meistens leer. Zusaetzlich liefert IBKR
mehrere Boersen-Eintraege in der Top-Level-Liste (alle Boersen, an
denen das Symbol gehandelt wird) - der Adapter muss den passenden
Eintrag selektieren.

`CalendarService` akzeptiert jetzt beide Schema-Varianten (Karten-
Anhang und Live), waehlt aus der Top-Level-Liste den passenden
``exchange``-Eintrag und faellt sonst auf das erste Element zurueck.
Tests um einen End-to-End-Smoke gegen das Live-Schema erweitert
(insgesamt 10 Tests in test_calendar_service.py).

---

## [1.19.1] — 2026-05-02 (AP-11 K4 hotfix — symbol-Pflichtparameter fuer trsrv/secdef/schedule)

Live-Smoke gegen U25235077 zeigte: IBKR ``/trsrv/secdef/schedule``
verlangt zusaetzlich zum ``exchange`` einen ``symbol``-Parameter,
sonst antwortet das CP-Gateway mit ``HTTP 400`` (laut K6-Anhang
C.1 dokumentiert, beim ersten Implementierungs-Wurf uebersehen).
Schedule selbst gilt boersenweit, das Symbol ist nur ein Aufhaenger.

`CalendarService.get(exchange_id, *, symbol="AAPL")` schickt jetzt
einen Default-Symbol-Wert mit; AAPL als US-Aktie deckt NASDAQ/NYSE
ab. Aufrufer koennen pro Call ein anderes Symbol angeben (z.B.
fuer asiatische Boersen, wo AAPL nicht gelistet ist). Tests
unveraendert; Live-Smoke greift in 1.19.1.

---

## [1.19.0] — 2026-05-02 (AP-11 K4 — CalendarService + /v1/exchanges + Symbol-zu-Boerse-Mapping)

CalendarService haelt 14-Tage-Boersenschedules im 12h-Cache pro
`exchange_id` und exponiert sie ueber zwei neue Endpunkte. Damit
liegt die Vorarbeit fuer AP-11 K5 (Tradeability-Felder im smd-Frame)
an: der `SmdTopicAdapter` kann ueber den Service `is_tradeable_now`
und `current_session` aus `availability_code` plus Live-Schedule
ableiten.

### Hinzugefuegt
- `src/broker_gateway/cp/calendar.py` mit `CalendarService` und den
  Pydantic-Modellen `ExchangeCalendar`/`CalendarDay`/`CalendarSession`.
  `get(exchange_id)` cached pro Boerse, Cache-Miss zieht
  `/trsrv/secdef/schedule` (assetClass=STK). LIQUID-Sessions werden
  zu `rth`, NON_LIQUID anhand der zeitlichen Position relativ zur
  RTH-Session zu `pre`/`post`. Halbtages-Sessions bleiben mit der
  IBKR-eigenen kuerzeren `closingTime`. Feiertage = Tag mit leerer
  `sessions`-Liste und `is_holiday=true`. Time-Zone aus dem Schedule-
  Response (`timeZoneId`); ungueltige Zonen (Mars/Olympus_Mons-Stil)
  werden als 502 abgewiesen. `cached_exchanges`-Property liefert die
  Liste aller nicht-abgelaufenen Eintraege fuer den `/v1/exchanges`-
  Endpoint.
- `src/broker_gateway/api/v1/exchanges.py` mit zwei FastAPI-Endpunkten:
  - `GET /v1/exchanges` listet die Boersen aus dem Schedule-Cache mit
    `exchange_id`/`time_zone`/`description` und einem
    `cached_calendars`-Counter. Scope `read:instruments`.
  - `GET /v1/exchanges/{exchange_id}/calendar?days=N` liefert den
    14-Tage-Schedule (Default 14, Range 1..14, 422 ausserhalb). Scope
    `read:instruments`.
- `src/broker_gateway/cp/instruments.py` `InstrumentDetail` um zwei
  Felder erweitert: `exchange_id` (Heimat-Boerse aus
  `listingExchange`, Fallback erster Token aus `validExchanges`) und
  `calendar_url` als Convenience-Link auf den neuen Endpoint. Public
  API: `GET /v1/instruments/{conid}` liefert beide Felder mit.
- `src/broker_gateway/main.py` instanziiert `CalendarService` im
  Lifespan und registriert den Dependency-Override.
- `tests/test_calendar_service.py` mit 9 Tests: Cache-Hit/Miss,
  TTL-Ablauf, LIQUID-zu-rth/NON_LIQUID-zu-pre-post,
  Halbtages-Session, Feiertag, `cached_exchanges`-Live-Filter,
  empty-exchange-id-422, CP-Fehler-zu-502, ungueltige Time-Zone-zu-502.
- `tests/test_exchanges_api.py` mit 6 Tests: leere Liste, Liste nach
  Cache-Befuellung, days=0/15/-Range-422 (zwei Tests), Scope-403 ohne
  `read:instruments`, `/v1/instruments/{conid}` liefert `exchange_id`
  (mit IBKR-Wert `NASDAQ.NMS` fuer AAPL) und `calendar_url`.
- `docs/05-api.md`: Endpunkt-Landkarte um Exchanges-Zeile ergaenzt.

### Naechste Schritte (AP-11 Phase A)
- K5 Tradeability-Felder im smd-Frame nutzt CalendarService und das
  exchange_id-Mapping.
- K3-Folgekarte (Lifespan-Wiring + Live-Smoke) bleibt parallel offen.

---

## [1.18.0] — 2026-05-02 (AP-11 K3 — WSPushSource + ENV-Schalter BG_QUOTES_SOURCE)

WSPushSource-Bruecke verbindet `SmdTopicAdapter` (K1) und
`SubscriptionRegistry` (K2) mit dem bestehenden `SubscriptionManager`-
Refcount/Fan-Out-Pfad. Pro WS-Frame laeuft ein Snapshot durch den
Adapter; das Ergebnis wird zu einem `Quote` (Public-API-Schema mit
String-Feldern) konvertiert und ueber `SubscriptionManager.publish`
an die Consumer-Queues gefan-outet. ENV-Schalter `BG_QUOTES_SOURCE`
mit Werten `ws|polling` (Default `polling`); Wechsel auf `ws` ist
opt-in und im `.env.example` mit Lehrtext dokumentiert.

### Hinzugefuegt
- `src/broker_gateway/config.py` mit `quotes_source()` als zentralem
  ENV-Reader. Default `polling`; ungueltige Werte fallen mit Warning
  zurueck. `Literal`-Typing, schmale API-Schicht ohne Pydantic-
  Settings (Service-Scope rechtfertigt keine Hierarchie).
- `src/broker_gateway/streams/ws_source.py` mit `WSPushSource`-Klasse:
  haelt einen Reader-Task der vom `CPWebSocketClient` iteriert, jedes
  Frame durch den `SmdTopicAdapter` schickt und das resultierende
  Snapshot an `SubscriptionManager.publish(conid, quote)` weitergibt.
  Konstruktor verdrahtet automatisch einen
  `add_on_connected_callback` an den Client - nach jedem Reconnect
  replayt die Registry alle aktiven Subscriptions. `subscribe_quotes`/
  `unsubscribe_quotes` als Manager-Callbacks pflegen Registry und
  senden `smd+<conid>+{json}` bzw. `usmd+<conid>+{}`. Send-Fehler
  werden geschluckt; Replay holt den Subscribe nach Reconnect nach.
- `src/broker_gateway/streams/manager.py` erweitert:
  - Konstruktor-Parameter `quotes_source: "polling"|"ws"` (Default
    `polling`) plus `ws_subscribe`/`ws_unsubscribe` als Callables.
    Im `ws`-Modus startet `_ConidSubscription.start_poll` keinen
    Poll-Task mehr; stattdessen feedet der WSPushSource Frames via
    neuer Methode `publish(conid, quote)`. Der `subscribe()`-Pfad
    ruft das `ws_subscribe`-Callback ausserhalb des Locks auf - ein
    Send-Fehler darf den Consumer-Stream nicht killen, weil die
    Registry den Soll-State haelt und der Replay nachzieht.
  - `_ConidSubscription.push_quote(quote)` als interner Fan-Out-
    Helper, der Ringpuffer und Consumer-Queues mit dem extern
    gelieferten Quote-Wert versorgt. Slow-Consumer-Drop-Logik
    identisch zum Poll-Pfad.
  - `_teardown` ruft im `ws`-Modus optional `ws_unsubscribe(conid)`
    auf, damit ein leerer Refcount auch serverseitig die
    Subscription beendet.
- `tests/test_ws_push_source.py` mit 14 Tests: Adapter-zu-Manager-
  Bruecke (Frame-Dispatch, Iterator-Event), `quotes_source='ws'`
  ohne `ws_subscribe` wirft (Konstruktor-Validierung),
  Polling-Modus startet Poll-Task / WS-Modus skippt Poll-Task,
  SSE-Vertragsgleichheit (Quote-Schema identisch zum Polling-Pfad),
  Reconnect triggert Registry-Replay, `subscribe_quotes` schreibt
  Registry plus sendet `smd+...`-Frame mit Default-Feldern,
  `unsubscribe_quotes` raeumt Registry plus sendet `usmd+...`,
  Frame fuer unbekannten conid wird verworfen ohne Fehler,
  Send-Fehler im Subscribe wird geschluckt, drei Tests fuer den
  ENV-Reader (`config.quotes_source` Default-Polling, ws-Lookup,
  Garbage-Fallback).
- `.env.example` um `BG_QUOTES_SOURCE`-Block mit Live-Wechsel-
  Hinweis ergaenzt.

### Default `polling` statt `ws` (Abweichung vom Karten-Soll)
Die Karte wuenscht Default `ws`; tatsaechlich ist der WS-Pfad im
laufenden Service noch nicht im FastAPI-Lifespan verdrahtet
(`CPWebSocketClient`-Instanz fehlt im Bootstrap). Mit Default
`polling` laeuft der Service auf cma-pi-1 unveraendert weiter, der
WS-Pfad ist Code-bereit und Test-abgedeckt, das Lifespan-Wiring plus
der Live-Smoke werden in einer Folgekarte angelegt. Begruendung:
saubere Defaults vor automatischem Auto-Revert; Phase A Live-
Aktivierung ist ein operatives Go-Live, nicht ein Build-Schritt.

### Naechste Schritte (AP-11 Phase A)
- K3 Folge: Lifespan-Wiring (CPWebSocketClient + WSPushSource im
  `main.py` instanziieren), Live-Smoke mit `BG_QUOTES_SOURCE=ws`
  gegen U25235077, Default-Flip auf `ws`.
- K4 `CalendarService` (parallel zu K3 umsetzbar - keine direkte
  Code-Abhaengigkeit).
- K5 Tradeability-Felder (haengt von K1 + K4).

---

## [1.17.0] — 2026-05-02 (AP-11 K2 — SubscriptionRegistry mit Reconnect-Replay-Hook)

Soll-State-Speicher fuer aktive WS-Subscriptions plus on-connected-Hook
am `CPWebSocketClient`. Hintergrund: das CP-Gateway persistiert den
Subscription-State nicht (K4-Reconnect-Befund) - jeder Reconnect wirft
alle `smd`/`sor`-Abos serverseitig weg. Damit der Push-Pfad nach
Reconnect nicht stillsteht, fuehrt die `SubscriptionRegistry` einen
expliziten Soll-State im Service und replayt nach jedem
`connect()`/erfolgreichem `_reconnect()` automatisch.

### Hinzugefuegt
- `src/broker_gateway/streams/registry.py` mit `SubscriptionRegistry`:
  `add(topic, args, owner)` haelt einen Refcount pro
  `(topic, frozenset(args))`-Schluessel; `remove` entfernt erst bei 0;
  `replay()` ruft den injizierten `subscribe`-Callable fuer jeden
  aktiven Eintrag (Subscribe-Fehler werden geloggt, aber geschluckt -
  ein Reconnect-Hook darf nicht abreissen); `pause()`/`resume()` friert
  nur `replay()` ein, `add`/`remove` bleiben funktional; `count()` fuer
  den `/v1/status`-Endpoint (AP-11 K8). State ist rein in-memory,
  asyncio.Lock-geschuetzt.
- `CPWebSocketClient.add_on_connected_callback(callback)`: registriert
  einen Async-Callback, der nach `connect()` UND nach erfolgreichem
  `_reconnect()` feuert. Callback-Exceptions werden geloggt, nicht
  propagiert.
- `tests/test_subscription_registry.py` mit 13 Tests: Refcount-
  Inkrement/Idempotenz pro Owner, Mehrfach-Owner-Zaehlung,
  remove-bei-0-Loeschung, idempotenter Remove auf unbekannte Owner/
  Schluessel, replay-Abdeckung aller Eintraege, pause-blockiert/
  resume-haebt-auf, Subscribe-Fehler-Schluck-Verhalten, count()-
  Reflektion, args-spezifische Schluesselbildung (verschiedene `conid`
  sind verschiedene Eintraege), Listen-Args-Hashable-Normalisierung,
  on-connected-Hook-Integration mit `CPWebSocketClient` (Replay nach
  Connect, Callback-Fehler-Robustheit).

### Naechste Schritte (AP-11 Phase A)
- K3 `WSPushSource` haengt `SmdTopicAdapter` (K1) und
  `SubscriptionRegistry` (K2) in den vorhandenen `SubscriptionManager`-
  Refcount ein und schaltet `/v1/quotes/stream` via
  `BG_QUOTES_SOURCE=ws` auf den WS-Push-Pfad um.

---

## [1.16.0] — 2026-05-02 (AP-11 K1 — SmdTopicAdapter, AP-11 Phase A startet)

Neue `cp/topics`-Schicht mit dem ersten Topic-Adapter fuer `smd`
(Single-Market-Data). Der Adapter parst rohe IBKR-WebSocket-Frames in
semantisch normierte `SmdFrame`-Voll-Snapshots, dekodiert Mixed-Types
gemaess `docs/architecture/ws-adapter-design.md` Anhang B
(String-Preise zu `Decimal`, String-Sizes zu `int`, Float-Change-Pct
zu `float`), merged Delta-Frames in den pro-`conid` gehaltenen
Snapshot-State, dedupliziert via `(conid, _updated)` und ignoriert
unbekannte Field-IDs forward-compat. Tradeability- und
`exchange_id`-Felder bleiben in dieser Karte konstant `None` und
werden in K4 (`CalendarService`) bzw. K5 (Tradeability) gefuellt.
Karte ist reine Frame-Transformation — kein REST-Call, kein
SSE-Endpoint angefasst (das ist K3 `WSPushSource`).

### Hinzugefuegt
- `src/broker_gateway/cp/topics/__init__.py` als Modul-Marker.
- `src/broker_gateway/cp/topics/smd.py` mit `SmdFrame`-`dataclass` und
  `SmdTopicAdapter`. Stateful pro-`conid`-Snapshot-Cache, Dedup-Map
  `(conid -> last_updated)`, defensive Konversion (`C271.55`-Praefix
  bei "Close-Last" wird gestrippt, ungueltige Werte werden zu `None`
  ohne Adapter-Crash).
- `tests/test_topic_adapter_smd.py` mit 11 Tests: Mixed-Type-
  Dekodierung, Voll-Snapshot beim ersten Frame, Delta-Merge mit Erhalt
  alter Werte, Dedup via `(conid, _updated)`, Replay gegen
  `tests/fixtures/recorded/ws/spike-baseline.jsonl` (Negativ-Garantie:
  keine `SmdFrame`s aus reinem Lifecycle-Recording), Forward-Compat
  fuer unbekannte Field-IDs, non-`smd`-Topics werden ignoriert,
  conid-Extraktion aus dem Topic-Suffix als Fallback,
  `C`-Praefix-Decimal-Robustheit, zwei `conid`s halten unabhaengige
  Snapshots.

### Naechste Schritte (AP-11 Phase A)
- K2 `SubscriptionRegistry` mit Replay nach `connect()` (parallel zu
  K1 umsetzbar).
- K3 `WSPushSource` haengt Adapter und Registry in den vorhandenen
  `SubscriptionManager`-Refcount ein und schaltet `/v1/quotes/stream`
  via `BG_QUOTES_SOURCE=ws` auf den WS-Push-Pfad.

---

## [1.15.0] — 2026-05-02 (AP-10 K1 — tokens.json Permission-Check, AP-10 abgeschlossen)

`FileTokenStore` haertet die persistente Token-Datei: beim Init wird
gewarnt bei zu offenen Permissions, beim Schreiben werden neue Dateien
per `os.chmod(..., 0o600)` reduziert. Schliesst die offene Sicherheits-
Frage 12.2 zu `tokens.json` ab; AP-10 (Security-Hardening) ist mit
dieser Karte vollstaendig abgeschlossen.

### Hinzugefuegt
- `src/broker_gateway/auth/store.py` `FileTokenStore._check_permissions`:
  per `os.stat()` werden `S_IRGRP|S_IWGRP|S_IXGRP|S_IROTH|S_IWOTH|S_IXOTH`
  geprueft. Bei Treffer eine `WARNING` an Logger `broker_gateway`
  (Strang `app.log`) mit konkretem `chmod 0600 <pfad>`-Hinweis. Auf
  Windows (`sys.platform == "win32"`) wird die Pruefung mit Debug-Log
  uebersprungen. Crash-frei: zu offene Permissions sind kein Service-
  Stop, nur eine Diagnose-Warnung.
- `_persist_locked` setzt vor dem atomaren `os.replace` ein
  `os.chmod(tmp, 0o600)` - neue/aktualisierte Token-Dateien sind
  damit auf POSIX dauerhaft 0600. `chmod`-Fehler sind auch hier
  best-effort: wird geloggt, blockiert das Schreiben aber nicht.
- `tests/test_auth_token_file_permissions.py` (5 Tests, 4 davon
  POSIX-only via `pytest.mark.skipif(sys.platform == "win32")`):
  existierende 0644-Datei -> Warning; neu geschriebene Datei ->
  Mode 0600; existierende 0600-Datei -> kein Warning; 0644-Datei
  wird beim ersten `put()` auf 0600 reduziert; nicht-existente
  Datei -> kein Warning (plattform-uebergreifend).

### Geaendert
- `docs/04-security.md` Sektion 2.2: vollstaendige Beschreibung des
  Permission-Checks und der Selbstheilung beim Schreiben. Sektion
  12.2: `tokens.json`-Frage als geklaert markiert (Bezug AP-10 K1).
- `tests/test_health.py`: `test_health_version_matches_package_version`
  entfernt - der hardcoded Versions-String brach bei jedem Bump und
  duplizierte den ersten Test (`test_health_returns_ok_and_version`),
  der `__version__` direkt vergleicht.

## [1.14.0] — 2026-05-02 (AP-05 K5 — Body-Token-Scan-Test, AP-05 abgeschlossen)

Letzte Karte des AP-05: automatischer Test, dass kein Bearer-Token-Wert
versehentlich als JSON-String in einem Read-Endpunkt-Body oder einem
Response-Header auftaucht. Schliesst die offene Sicherheits-Frage 12.2
in `docs/04-security.md`. Mit dieser Karte ist AP-05
(Logging-Infrastruktur) vollstaendig erledigt.

### Hinzugefuegt
- `tests/test_no_token_leak_in_bodies.py` — erzeugt via
  `POST /v1/auth/token` einen frischen Bearer-Token mit allen Scopes,
  faehrt damit gegen 10 Read-Endpunkte (`/v1/health`,
  `/v1/internal/health`, `/v1/instruments/search`,
  `/v1/instruments/{conid}`, `/v1/quotes/snapshot`,
  `/v1/portfolio/{accountId}` Summary/Positions/Ledger,
  `/v1/orders/{order_id}` mit synthetischer ID -> 404,
  `/v1/trades`) und verifiziert, dass der Token-Wert weder im
  Response-Body noch in einem Response-Header auftaucht. Zweiter
  Test sichert die explizite Auslassung von `POST /v1/auth/token`
  (Token-Echo ist dort designiertes Verhalten, gegen Drift). SSE-
  Stream-Endpunkte sind out-of-scope dieser Karte.
- Negativ-Smoke (manuell verifiziert): wenn `/v1/health` kuenstlich
  so manipuliert wird, dass es den ``Authorization``-Header in den
  Response-Body echo't, schlaegt der Test fehl. Damit ist die
  Test-Wirkung belegt.

### Geaendert
- `docs/04-security.md` Sektion 4.3 — "nicht automatisch geprueft"-
  Aussage durch Verweis auf `tests/test_no_token_leak_in_bodies.py`
  ersetzt; Endpunkt-Liste plus Auslassungs-Begruendung
  dokumentiert. Sektion 12.2 — Body-Token-Scan-Frage als geklaert
  markiert (Bezug AP-05 K5). Zusaetzliche Aufraeum-Edits:
  Sektion 3 Recording-Leak-Zeile (Pre-Commit-Hook AP-05 K4
  erwaehnt); Sektion 4.2 (CPWireLogger statt "kommend"); Sektion
  5.1 Tabelle (cp_wire.log mit ENV-Schalter statt "Hook kommt").

## [1.13.0] — 2026-05-02 (AP-05 K4 — Pre-Commit-Hook fuer Recordings)

Automatische Leak-Praevention fuer eingecheckte Recordings: Pre-Commit-
Hook scannt staged JSON/JSONL unter `tests/fixtures/recorded/` auf
Authorization-Header, URL-safe-Token-Strings (>=32 Zeichen) und
Cookie-Patterns. Schliesst die offene Sicherheits-Frage 12.2 in
`docs/04-security.md`.

### Hinzugefuegt
- `scripts/pre_commit_recording_scan.py` — Skript scannt staged
  Recording-Dateien auf REDACTED_HEADERS-Namen (importiert aus
  `broker_gateway.cp.redaction` als SSOT), URL-safe-Strings >= 32
  Zeichen (mit Allowlist fuer bekannte Hash-/Identifier-Felder wie
  `MAC`, `hardware_info`, `etag`, `server-timing`, `x-request-id`,
  `request_id`, `user-agent`, Manifest-`files`-Listen,
  whatif-`warns`/`warning_code`/`warning_message`) und Cookie-Patterns
  (`sess=`, `X-XSRF-TOKEN=`, `_csrf=`, `JSESSIONID=`). WS-Recordings
  unter `tests/fixtures/recorded/ws/` skippen die URL-safe-Heuristik
  (Frame-/Session-IDs sind dort strukturell URL-safe-32-stellig);
  Header- und Cookie-Patterns bleiben aktiv. Exit-Codes: 0 sauber, 1
  Verdacht, 2 JSON-Parse-Fehler.
- `.pre-commit-config.yaml` — lokaler Hook (`recording-token-scan`),
  triggert auf `^tests/fixtures/recorded/.*\.(json|jsonl)$`,
  ruft das Skript mit `pass_filenames: true`.
- `tests/test_pre_commit_recording_scan.py` — 12 Tests:
  Clean-Recording -> 0; Authorization/Cookie-Header -> 1;
  URL-safe-Token im Body -> 1; kurze Tokens (<32) -> 0;
  SHA256-Hash in Allowlist-Feld (`MAC`) -> 0;
  Cookie-Pattern-Substring im Body -> 1;
  Pfad ausserhalb `tests/fixtures/recorded/` -> 0;
  No-Args -> 0; WS-Recording mit 32-Hex-Frame-IDs -> 0;
  WS-Recording mit Authorization-Header -> 1;
  invalid-JSON -> 2.

### Geaendert
- `pyproject.toml` — `dev`-Extras um `pre-commit>=3.0`
  ergaenzt.
- `README.md` — `pre-commit install` als einmaliger Setup-Schritt
  nach `pip install -e .[dev]`. Kurzbeschreibung des Hooks und der
  SSOT-Konvention (REDACTED_HEADERS).
- `docs/04-security.md` Sektion 6.3 — manuelle-Sichtpruefung-
  Aussage durch Verweis auf `scripts/pre_commit_recording_scan.py`
  ersetzt; vollstaendige Beschreibung der drei Heuristiken plus
  WS-Pfad-Sonderbehandlung. Sektion 12.2 — Pre-Commit-Hook-Frage
  als geklaert markiert (Bezug AP-05 K4).
- `docs/cp-recordings.md` — neue Unter-Sektion "Pre-Commit-Hook
  gegen Token-Leaks" unter "Was NICHT gespeichert wird".

## [1.12.0] — 2026-05-02 (AP-05 K3 — CP-Wire-Log)

Forensisches IBKR-Wire-Log: jeder broker-gateway → CP-Gateway-Roundtrip
landet 1:1 in `cp_wire.log`, korreliert per `request_id` mit den
Consumer-Inbound-Events. Letzte Karte des AP-05.

### Hinzugefuegt
- `src/broker_gateway/cp/wire_log.py` — `CPWireLogger`-Klasse mit
  request- und response-event_hooks an `httpx.AsyncClient`.
  Schreibt pro Roundtrip ein `cp_wire`-Event mit `method`, `path`,
  `query`, `request_headers`, `request_body`, `status`,
  `response_headers`, `response_body`, `latency_ms`. Header werden
  ueber `cp.redaction.filter_headers` gefiltert (Authorization,
  Cookie, Set-Cookie, X-API-Key, X-Auth-Token, Proxy-Authorization).
  Bodies werden **nicht** durch `cp.normalize.normalize_response`
  geschickt — forensische Treue, Order-IDs/Timestamps/Session-IDs
  erscheinen wie tatsaechlich uebertragen.
- `tests/test_cp_wire_log.py` — 9 Tests: GET-/POST-Roundtrip,
  request_id-Korrelation via structlog-ContextVars, Token-Redaktion
  (Header und Body-Substring-Check), POST-Body 1:1 ohne Normalisierung,
  4xx/5xx-Pfad, `BG_CP_WIRE_LOG=off`-Pfad, Default-on-Pfad,
  Fault-Tolerance bei Logger-Exception, Koexistenz mit `CPRecorder`.
- ENV `BG_CP_WIRE_LOG` (Default `on`); `off`/`0`/`false`/`no`
  deaktiviert den Hook komplett. Recorder bleibt davon unberuehrt.

### Geaendert
- `src/broker_gateway/cp/client.py` — `CPGatewayClient.__init__`
  installiert per Default einen `CPWireLogger` zusaetzlich zum
  `CPRecorder`. Konstruktor-Parameter `wire_logger` erlaubt
  Test-Injection. Beide Hooks koexistieren am selben
  `httpx.AsyncClient`.
- `README.md` — Observability-Sektion erweitert: CP-Wire-Log-
  Beschreibung als Unter-Abschnitt, ENV-Tabelle um `BG_CP_WIRE_LOG`
  ergaenzt, Strang-Routing-Hinweis aktualisiert.

## [1.11.0] — Nachtrag 2026-05-01 (AP-04 K6 rev. 2 nach User-Review)

AP-04 K6 Architektur-Doku in der zweiten Iteration: vier
User-Direktiven aus dem Review eingearbeitet plus Handelszeit-/
Tradeability-/Boersenkalender-Modell ergaenzt. Doku-only.

### Geaendert
- `docs/architecture/ws-adapter-design.md` rev. 2:
  - **AP-Bezug korrigiert**: das Folge-AP heisst nicht "AP-05"
    (existiert bereits als Logging-Infrastruktur), sondern
    Vorschlag-Titel "AP-11 WS-Adapter Implementation".
  - **Multi-Tenant raus**, Single-Tenant zementiert (PSM bleibt
    Single-User per User-Direktive).
  - **WS-Egress als zweites Ziel-Tier** parallel zu SSE
    (`/v1/quotes/ws`, `/v1/orders/ws` neu in Phase B; SSE bleibt
    Pflicht-Pfad in Phase A).
  - **Failure-Mode-Header `X-Stream-Failure-Mode` zurueckgenommen**
    -- Default `fail-loud` global; PSM macht REST-Polling als
    eigene Reaktion (Consumer-Logik, nicht Adapter-Komplexitaet).
  - **N-1-Schema-Compat-Vertrag zurueckgenommen** -- Schema-Wechsel
    werden synchron zwischen broker-gateway, PSM und trading_robot
    koordiniert (User-Direktive: Consumer unter eigener Kontrolle);
    `schema_version` bleibt nur als optionales Diagnose-Feld.
  - **Per-Konto-Token-Scopes deferred** -- bleibt Coarse-Scope.

### Hinzugefuegt
- Anhang C "Handelszeit, Tradeability und Boersenkalender" mit
  drei Schichten:
  1. Boersenkalender (14 Tage Sessions inkl. Feiertage) aus
     IBKR-REST `/trsrv/secdef/schedule`, `CalendarService`-Cache
     pro `exchange_id` (TTL 12h).
  2. Symbol-zu-Boerse-Mapping aus `/iserver/contract/{conid}/info`,
     Cache pro `conid` (TTL 24h).
  3. Live-Tradeability aus `smd`-Frame-Feldern 6509/7295/7296,
     abgeleitete Felder `is_tradeable_now: bool` und
     `current_session: rth|pre|post|closed|halted`.
- Neue Endpunkte skizziert:
  - `GET /v1/exchanges` (Liste der bekannten Boersen).
  - `GET /v1/exchanges/{exchange_id}/calendar?days=N` (Default 14,
    max 14 -- IBKR-Vorrat).
  - `/v1/instruments/{conid}` um `exchange_id` und `calendar_url`
    ergaenzt.
- AP-11-Karten-Skizzen aktualisiert: 8 Karten (Failure-Mode-Header
  und Schema-Versionierungs-Karten gestrichen, dafuer eine
  CalendarService-Karte und eine WS-Egress-Karte neu).

Doku-only, kein Production-Code, kein Image-Rebuild, kein Deploy.

## [1.11.0] — Nachtrag 2026-05-01 (AP-04 K6)

AP-04 K6 - WS-Adapter-Architektur-Design als Decision-Gate zwischen
AP-04 und AP-05. Reines Doku-Update; ``pyproject.toml``, ``src/`` und
``compose.yaml`` bleiben per Karten-Constraint unveraendert (kein
Production-Code, kein Image-Rebuild, kein Deploy).

### Hinzugefuegt
- `docs/architecture/ws-adapter-design.md` NEU: 9 Sektionen +
  2 Anhaenge. Konsumiert die Phase-1-Findings aus K1-K4, die
  PSM-Antwort
  (`personal_stock_manager/docs/integrations/broker-gateway.md`,
  KanPrompt-Karte `a1037c45-b4af-42bc-abd4-8a2ac015ceeb`) und die
  trading_robot-Antwort
  (`trading_robot/docs/integrations/broker-gateway.md`,
  KanPrompt-Karte `e71623d2-bd8d-4643-a15b-6d93c1afafd5`) zu einem
  konkreten Adapter-Schnitt. Sektionen: Quellen, Zielbild
  (Topic-Entscheidungen smd/sor integriert, str/spl/smh/sbd nicht),
  Komponenten-Diagramm (SubscriptionRegistry, TopicAdapter,
  WSPushSource, OrdersStreamRouter, StatusEndpoint), Consumer-API
  (`/v1/quotes/stream`-Vertrag bleibt, `/v1/orders/stream` neu,
  `schema_version` Pflichtfeld, `X-Stream-Failure-Mode`-Header zur
  Konsumer-Konfliktaufloesung), Failure-Mode-Strategie,
  Test-Strategie (Unit + Integration + Live), Migration mit
  Phase-A/B/C und ENV-Rollback-Pfad, AP-05-Karten-Skizzen (8 Karten),
  Risiken/offene Fragen, Decision-Gate-Markierung.

### Hauptentscheidungen
- Erste AP-05-Iteration nur ``smd``; ``sor`` zweite Iteration mit
  REST-Bootstrap (IBKR liefert keinen Initial-Snapshot ueber WS).
- Konsumer-Konflikt Failure-Mode (PSM REST-Fallback fuer ``smd``,
  Robot Fail-Loud) via Per-Subscription-Header
  ``X-Stream-Failure-Mode`` aufgeloest, Default ``fail-loud``.
- Robot-Latenz-SLO ``smd`` p95 < 150 ms als Adapter-Ziel - PSM
  bekommt das geschenkt.
- WS-Egress vom Service zu Consumern bewusst ausgeschlossen,
  beide Consumer waehlen SSE.
- ``schema_version: int`` als Pflicht-Frame-Feld + optionaler
  ``X-Schema-Version``-Subscribe-Header fuer N-1-Compat.

## [1.11.0] — Nachtrag 2026-04-30 (AP-04 K4)

AP-04 K4 - Topic-Exploration smd/sor/str gegen Live-Session. Reines
Skript+Doku-Update; ``pyproject.toml`` und ``src/`` bleiben per
Karten-Constraint unveraendert (Skript ist explorativ, kein Production-
Pfad). Kein Version-Bump, kein Image-Rebuild, kein Deploy.

### Hinzugefuegt
- `scripts/ws_topic_explorer.py`: CLI mit Subcommands ``smd-single``,
  ``smd-multi``, ``smd-large``, ``str-trades``, ``sor`` (mit
  ``--with-test-order``), ``reconnect`` und ``all``. Nutzt den
  ``CPWebSocketClient`` aus K3 plus eine ``JsonlSink``-Hilfsklasse,
  die Frames im K2-kanonischen ``{ts, dir, topic, raw, parsed}``-Format
  schreibt. Robust gegen ``bytes``-Frames, die der untenliegende
  ``websockets.recv()`` liefern kann (Skript-seitiger Workaround, der
  ``CPWebSocketClient``-Type-Hint bleibt unangetastet).
- `tests/fixtures/recorded/ws/topic-explorer-2026-04-30/`: sechs
  Live-Mitschnitte (smd-single 14, smd-multi 98, smd-large 187,
  str 277, sor 15, reconnect 8 in-Frames) gegen U25235077 zur
  RTH-Boersenstunde. sor-Mitschnitt enthaelt einen vollstaendigen
  Order-Lifecycle (LMT BUY 1x AAPL @ $1.00, sofortiger Cancel, kein
  Match) inkl. der drei IBKR-Confirmation-Replies.

### Geaendert
- `docs/research/ibkr-cpapi-websockets-findings.md`: neuer Abschnitt
  ``Topic-Exploration (K4, 2026-04-30)`` mit pro-Szenario-Auswertung,
  Felder-Inventar, Pacing-Beobachtungen, sor-Order-Lifecycle-Analyse
  und einer Ranking-Tabelle ``Topic - Reife`` (smd=gruen, sor=gruen,
  str=gelb, Reconnect=rot). Konsequenzen fuer K5/K6/AP-05 ableitet.
- `README.md` Status-Footer: K4-Hinweis ergaenzt.

### Wichtigste Befunde fuer K6
- ``smd`` liefert Felder als **Delta** - jeder Frame nur Aenderungen,
  Adapter braucht State-Cache pro Symbol.
- ``sor`` liefert keinen garantierten Initial-Snapshot - ``/iserver/account/orders``
  REST als Bootstrap noetig.
- Subscription-State **persistiert nicht** ueber Reconnect - ein
  Subscription-Manager im Adapter-Layer muss alle Subs nach jedem
  Reconnect neu auspielen.
- Mixed-Type-Werte (Preise als String, Mengen als Float) sind die Regel,
  nicht die Ausnahme - Adapter normalisiert pro Feld.

## [1.11.0] — 2026-04-30

AP-04 K3 - WS-Client als wiederverwendbarer Baustein. ``CPWebSocketClient``
kapselt connect, Auth-Frame, Auth-Ack-Wait (sts.authenticated=true),
async-Frame-Iteration, send, tic-Ping-Loop und Reconnect mit exponential
backoff. Single-Owner-Konstraint: pro Instanz nur ein ``connect()``. Der
Baustein wird in dieser Karte NICHT in main.py oder einen Endpoint
eingebunden - Konsumenten (Quotes-Stream, EventBus, SSE-Mapping) entstehen
spaeter im AP-04 K6 / Folge-AP.

### Hinzugefuegt
- `src/broker_gateway/cp/ws_client.py`: Klasse ``CPWebSocketClient`` plus
  ``WSIncomingFrame``, ``WSAuthError`` und das ``WSConnection``-Protocol.
  Connect-Default ``ws://cpgateway:5000/v1/api/ws`` (Plain-HTTP wie der
  REST-Pfad im Compose-Netzwerk), Override per ENV ``BG_CP_WS_URL``.
  Cookie-Reuse aus dem REST-Client erfolgt explizit als
  Methodenparameter (kein Import-Coupling). TLS-Strategie liegt komplett
  bei der ``websockets``-Lib - kein lokaler SSL-Override.
- `src/broker_gateway/cp/__init__.py`: Re-Export der drei oeffentlichen
  Symbole + ``WSConnection``.
- `tests/test_ws_client.py`: 10 Tests gegen einen In-Memory-FakeConnection-
  Stub (kein echter Socket noetig). Deckt connect+auth, Auth-Failure
  (``sts.authenticated=false``), Auth-Timeout, tic-Ping-Loop, Reconnect
  bei broken pipe, Aufgabe nach max-Reconnect-Attempts, Single-Owner-
  Doppel-Connect-Reject, Frame-Iteration mit der spike-baseline-Fixture
  aus K2, send-vor-connect und send-nach-close.

### Geaendert
- `pyproject.toml`: neue Runtime-Dep ``websockets>=12``. Begruendung
  (Implementation-Log): pure-async, gut testbar via injizierbarer
  connect-Factory, kein C-Extension-Build noetig auf cma-pi-1.
- `compose.yaml`, `src/broker_gateway/__init__.py`,
  `tests/test_health.py`, README-Footer: 1.10.0 -> 1.11.0.

### Bekannte Einschraenkungen
- Der WS-Client ist nirgends im App-Lifespan instanziiert - das ist die
  bewusste Karten-Abgrenzung. Naechste Schritte: AP-04 K4 (Topic-
  Exploration), K5 (Consumer-Fragebogen), K6 (Architektur-Decision-Gate).
- ``tic``-Multiplikator (4 Server-Antworten pro Client-Ping, dokumentiert
  in `docs/research/ibkr-cpapi-websockets-findings.md`) wird vom Frame-
  Iterator unveraendert durchgereicht. Dedup ist Konsumenten-Logik (z.B.
  EventBus) und kommt in einer Folge-Karte.

## [1.10.0] — 2026-04-29

AP-05 Karte 2/3 - Inbound-Body-Logging. Die ObservabilityMiddleware
schreibt jetzt zusaetzlich zu den Metadaten `request_headers`,
`request_body`, `response_headers`, `response_body` und
`response_streaming` ins `http_request`-Event. Bodies werden 1:1
abgelegt (kein Normalize, keine Truncation); Header werden ueber
`broker_gateway.cp.redaction.filter_headers` gefiltert. SSE-Antworten
(`text/event-stream`) bleiben unangetastet und werden mit
`response_streaming=true` markiert. `request_id` wird via
`structlog.contextvars.bind_contextvars` an den ContextVar-Stack
gebunden - damit landet sie automatisch in jedem nachgelagerten Event
derselben Request-Verarbeitung (Vorbereitung fuer Karte 3 CP-Wire).

### Hinzugefuegt
- `BG_LOG_INBOUND_BODIES` (default `on`): Notfall-Schalter zur
  Deaktivierung der Body-/Header-Erfassung. `off`/`0`/`false`/`no`
  schaltet ab; Metadaten bleiben in beiden Modi unveraendert.
- Neue Test-Cases in `tests/test_observability.py`: response_body bei
  GET, request_body bei POST (`/v1/auth/token`), 422-Pfad mit Body,
  Authorization/Cookie/X-API-Key/X-Auth-Token nie im Log,
  `BG_LOG_INBOUND_BODIES=off`-Verhalten, Stream-Replacement (Endpunkt
  sieht den Body nach Middleware-Read), SSE-Endpunkt mit
  `response_streaming=true`.

### Geaendert
- `src/broker_gateway/middleware/observability.py`: Stream-Replacement
  fuer Request-Body via `request._receive`-Replay, Response-Body durch
  Materialisierung von `body_iterator` (neuer Response gebaut, damit
  der Iterator nicht zweimal konsumiert wird), Streaming-Erkennung via
  `Content-Type: text/event-stream`. Pre-Read nur fuer Requests mit
  `Content-Length > 0` oder `Transfer-Encoding: chunked` - sonst
  bleibt der ASGI-receive-Stream unberuehrt (sonst kollidiert das
  Replay mit `BaseHTTPMiddleware.wrapped_receive` bei
  GET-/SSE-Endpunkten).
- `compose.yaml`, `pyproject.toml`, `__init__.py`, `test_health.py`,
  README-Footer: 1.9.0 -> 1.10.0.
- README Observability-Section: Body-/Header-/Streaming-Felder, neue
  ENV-Variable `BG_LOG_INBOUND_BODIES`.

### Bekannte Einschraenkungen
- `cp_wire.log` bleibt leer, bis Karte 3 (CP-Wire-Log) den
  `broker_gateway.cp.wire`-Logger befuellt. Die Korrelation per
  `request_id` ist bereits vorbereitet.
- Bodies werden ohne Truncation geschrieben - bei einzelnen sehr
  grossen Payloads (Bulk-Order, grosse Quotes-Snapshots) kann
  `inbound.log` schnell wachsen. Rotation via
  `BG_LOG_INBOUND_MAX_BYTES`/`BG_LOG_INBOUND_BACKUP_COUNT` greift
  trotzdem; pro-Event-Truncation ist out-of-scope dieser Karte.

## [1.9.0] — 2026-04-29

AP-05 Karte 1/3 - Logging-Backbone. structlog/stdlib-Pipeline auf einen
gemeinsamen JSONRenderer harmonisiert; Routing per Logger-Name auf drei
Straenge (`broker_gateway.http` -> `inbound.log`, `broker_gateway.cp.wire`
-> `cp_wire.log`, `broker_gateway` -> `app.log`) wenn `BG_LOG_DIR`
gesetzt ist. Backwards-kompatibel - ohne `BG_LOG_DIR` schreiben alle
drei Logger weiter auf stdout. Die Inhalte der Logs aendern sich noch
nicht; Bodies kommen mit Karte 2 (Inbound) und 3 (CP-Wire).

### Hinzugefuegt
- `src/broker_gateway/cp/redaction.py`: `REDACTED_HEADERS` (frozenset,
  lower-case) und `filter_headers()` als Single Source of Truth fuer
  Header-Redaktion. Wird vom CPRecorder bereits genutzt; CP-Wire-Logger
  und Inbound-Body-Middleware werden ebenfalls darauf importieren.
- `src/broker_gateway/logging_setup.reset_for_testing()`: setzt das
  `_CONFIGURED`-Flag und structlog-Defaults zurueck, damit Tests mit
  geaenderten ENV-Variablen arbeiten koennen.
- ENV-Variablen `BG_LOG_DIR`, `BG_LOG_LEVEL`, `BG_LOG_ROTATE_MAX_BYTES`,
  `BG_LOG_ROTATE_BACKUP_COUNT` sowie pro-Strang-Overrides
  `BG_LOG_INBOUND_*`, `BG_LOG_CP_WIRE_*`, `BG_LOG_APP_*`.
- `tests/test_cp_redaction.py`, `tests/test_logging_setup.py`.

### Geaendert
- `src/broker_gateway/logging_setup.py`: structlog nutzt jetzt
  `structlog.stdlib.LoggerFactory()` statt `PrintLoggerFactory`, und
  formatiert via `structlog.stdlib.ProcessorFormatter` mit
  `foreign_pre_chain`. Damit laufen stdlib-Logger (Throttle, Streams,
  CP-Lifecycle, Recorder) durch denselben JSONRenderer wie
  Bound-Logger - die README-Aussage "jede Log-Zeile ist ein JSON-Dict"
  ist jetzt tatsaechlich wahr. stdout-Default-Pfad nutzt `_LazyStdout`,
  damit pytest-`capsys` die Reference auch nach Modul-Import noch
  patchen kann.
- `src/broker_gateway/cp/recorder.py`: importiert `REDACTED_HEADERS`
  und `filter_headers` aus `cp/redaction.py`; lokale Kopie entfernt.
- `compose.yaml`, `src/broker_gateway/__init__.py`,
  `tests/test_health.py`, README-Footer: 1.8.0 -> 1.9.0.

### Bekannte Einschraenkungen
- Inhalte der Logs sind noch unveraendert (Bodies fehlen weiter in
  `inbound.log`, der CP-Wire-Strang ist noch leer). Das ist Scope von
  Karten 2 und 3 in AP-05.

## [1.8.0] — 2026-04-28

Release-Karte AP-03 - duale Drift-Detection. Doku-Drift-Check als
Frueh-Warner (taeglich, ohne Auth) plus Live-Drift als Build-Acceptance-
Test. Karte forderte 1.7.0; tatsaechlich 1.8.0, weil 1.7.0 schon durch
AP-02 #07-4 belegt war.

### Hinzugefuegt
- `tests/cp_doc/diff.py`: `diff_openapi(actual, expected) -> SpecDiffReport`.
  Klassifiziert OpenAPI-/Swagger-Spec-Aenderungen in vier Stufen:
  `no drift`, `minor (additive)`, `value (irrelevant)`, `breaking`. Behandelt
  Pfad-/Operation-/Status-Code-/Schema-/Enum-/Required-Aenderungen,
  unterscheidet Request- und Response-Mode (z.B. neues required Request-
  Feld = breaking, neues Response-Feld = minor).
- `scripts/check_doc_drift.py`: CLI-Skript, das die Live-IBKR-OpenAPI-Spec
  laedt und gegen `docs/research/ibkr-cpapi-doc.json` vergleicht. Schreibt
  Bericht nach `reports/doc-drift/<YYYY-MM-DD>.md`. Exit-Codes 0/1/2/3
  (no/breaking/minor/unreachable). Mit `--auto-card` legt es eine
  KanPrompt-Karte via REST an; Spam-Schutz: max. 1 Karte pro Tag pro
  Drift-Klasse, Praefix-Check via `GET /api/v1/projects/.../cards`.
- `ops/systemd/doc-drift.{service,timer}` plus `doc-drift.env.example` und
  `ops/systemd/README.md`: taeglicher Lauf um 06:00 Europe/Berlin auf
  cma-pi-1. KANPROMPT_API_KEY kommt aus `/etc/default/doc-drift`, niemals
  aus dem Repo.
- `ops/build-gateway.sh`: Build-Wrapper `docker compose build` ->
  `check_mock_drift --build-acceptance` -> `docker compose up -d`. Bricht
  ab, wenn der Drift-Check fehlschlaegt. `SKIP_ACCEPTANCE=1` als Notfall-
  Bypass.
- `scripts/check_mock_drift.py`: neuer `--build-acceptance`-Modus mit 90s
  Warmup-Pause vor dem ersten Replay (project_ibkr_session_resume),
  strengerem Exit-Code (auch value drift = exit 1) und Berichts-Pfad
  `reports/drift/build-<commit-sha>.md`. Ohne den Flag: bestehendes
  Verhalten unveraendert.
- `tests/test_doc_drift.py`: 20 Unit-Tests fuer `diff_openapi` (alle 11
  Pflichtfaelle aus der Karte plus defensive Zusatzfaelle wie
  required-Aenderungen in Response, Enum-Removal in Request, gemischte
  Severities, Markdown-Render).
- `tests/test_check_doc_drift.py`: 11 Integrationstests mit
  `httpx.MockTransport` (Exit-Codes, Berichts-Datei, Auto-Karten-Anlage
  mit Spam-Schutz, Fehlerpfade).
- `docs/runbooks/doc-drift-check.md`: vollstaendiges Runbook fuer den
  Doku-Drift-Check inkl. Drift-Strategie-Schaubild, Reaktion pro
  Klassifikation, Spam-Schutz-Erklaerung, Baseline-Update-Workflow,
  Troubleshooting.
- `reports/doc-drift/2026-04-28.md`: erster Doku-Drift-Bericht (analog
  zur Karte AP-02 #06).

### Geaendert
- `docs/runbooks/mock-drift-check.md`: Section "Build-Acceptance-Modus"
  ergaenzt; "Wann laufen wir das" auf "bei jedem Container-Rebuild + ad
  hoc" angepasst (woechentliche Routine entfaellt).
- `docs/cp-recordings.md`: Section "Drift-Strategie" voran gestellt mit
  Schaubild Doku-Drift (Frueh-Warner) vs. Live-Drift (Build-Acceptance).

### Version-Bump
- `pyproject.toml`, `src/broker_gateway/__init__.py`, `compose.yaml`
  Image-Tag, `tests/test_health.py`, README-Footer: 1.7.0 -> 1.8.0.

## [1.7.0] — 2026-04-28

Release-Karte AP-02 #07-4 - Live-Recording-Lauf gegen die in
1.6.1/1.6.2/1.6.3 korrigierten Service-Pfade. Aggregiert die vier
AP-02 #07-Sub-Karten in einen Minor-Release. Karte sprach urspruenglich
von 1.4.0; tatsaechliche Versionsnummer ist 1.7.0, weil 1.6.x in den
Sub-Karten 1-3 verbraucht wurde.

### Hinzugefuegt
- `scripts/recording_session.py happy-path` zeichnet zusaetzlich
  `GET /sso/validate` (Schritt `a++)`) auf - Replay-Mock kann jetzt auf
  reale Bodies fuer den primaeren Keep-Alive zurueckgreifen.
- `src/broker_gateway/cp/normalize.py`: neues `_SECRET_FIELDS_LOWER`
  redacts `TOKEN`, `CREDENTIAL`, `IP`, `USER_NAME`, `USER_ID`,
  `UNIQUE_LOGIN_ID`, `MAC`, `hardware_info`, `userId` (tickle) und
  weitere sensible Felder aus `sso/validate`/`auth/status`/`tickle` auf
  `<REDACTED>`. Erstes 07-4-Recording hatte einen Auth-Token im
  `sso/validate`-Body geleakt; geleakte Files wurden nicht commited
  und durch redacted Live-Recordings ersetzt.

### Geaendert
- 23 Live-Recordings unter `tests/fixtures/recorded/live/` neu
  aufgezeichnet (broker-gateway 1.6.3, IBKR Build 10.45.1a). Alle
  v1-Service-Pfade liefern HTTP 200; die 7 dokumentarischen 404er sind
  alte Probe-Calls (Service-Code ruft sie nicht mehr) plus
  unsubscribe-Live-Artefakt.
- 5 synthetische Seeds aus AP-02 #07-1/3 entfernt
  (`portfolio/{summary,positions/0,ledger}`, `iserver/accounts`,
  `sso/validate`); alle haben nun reale Live-Pendants.
- `docs/runbooks/recording-session-happy-path.md`: Diff-Report
  2026-04-28 ergaenzt mit Verification-Tabelle aller korrigierten
  Pfade, geloeschte Seeds, Recorder-Filter-Erweiterung und
  IBKR-Server-Build-Drift `JifZ28031/10.44.1h -> JifZ20074/10.45.1a`.

### Hinweis
- Drift-Detection-Smoke-Test (zweiter Lauf, warm): 0 breaking, 1 minor
  (`sso/validate.isGw` additive), 4 value (Timestamps + FX-Bruchteile -
  normale Live-Schwankung). Erster Lauf zeigte 1 breaking
  (`marketdata/snapshot.6509: DPB -> ZB`), das war Cold-Session-Effekt
  - im zweiten Lauf bestaetigt sich das nicht. Verhalten ist im
  Auto-Memory `project_ibkr_session_resume` dokumentiert.
- v1-API-Vertrag unveraendert. Schliesst AP-02 Karte 07
  (Service-Code-an-reale-IBKR-Pfade) ab.

## [1.6.3] — 2026-04-28

### Hinzugefuegt
- `CPGatewayClient.sso_validate()` und `CPGatewayClient.iserver_accounts()`
  als neue Lifecycle-Endpunkte (GET /sso/validate, GET /iserver/accounts).
- `AuthLifecycle._maybe_init_accounts()` ruft `GET /iserver/accounts`
  beim ersten erfolgreichen Tickle nach Login auf und persistiert das
  Ergebnis als `accounts_initialized=True`. IBKR setzt diesen Call vor
  dem ersten Order- oder Portfolio-Aufruf voraus.
- `AuthLifecycle._heartbeat_sso()`: primaerer Keep-Alive geht jetzt
  ueber `GET /sso/validate` (Spec-Empfehlung). Tickle bleibt als
  sekundaerer CP-Health-Indicator und Backward-Compat-Pfad erhalten -
  ein Tickle-Fehler bei gueltigem sso/validate landet nicht mehr in
  CP_DOWN.
- `AuthLifecycle.reauthenticate(force=False)`: oeffentliche Methode
  fuer manuelle Reauth-Triggers. Mit `force=True` wird
  `POST /iserver/reauthenticate` unconditional ausgeloest und der
  Auth-Status danach geprueft - hilft im cold-tunnel-Fall (siehe
  Auto-Memory `project_ibkr_session_resume`), in dem `auth/status`
  faelschlich `authenticated=false` meldet, der Reauth aber sofort
  durchgeht.
- `LifecycleSnapshot` und `/v1/internal/health` exponieren neu
  `last_sso_validate_at`, `last_login_at`, `accounts_initialized`.

### Geaendert
- `tests/cp_mock/replay.py`: neue Mock-Routen fuer GET /sso/validate
  und GET /iserver/accounts. Bei `auth_lost=True` liefert sso/validate
  `RESULT=false`. Seed-Recordings fuer beide Endpunkte unter
  `tests/fixtures/recorded/seed/`; Live-Recording fuer
  `iserver/accounts` existiert bereits aus AP-02 #04.

### Hinweis
- v1-API-Vertrag unveraendert. `force` ist interner Lifecycle-Schalter,
  kein Vertragsfeld. Dritte von vier Sub-Karten in AP-02 #07.

## [1.6.2] — 2026-04-28

### Geaendert
- `cp/orders.py::OrdersService.get_order` ruft jetzt den IBKR-Singular-
  Pfad `GET /iserver/account/order/status/{orderId}` (vorher: nicht
  existenter Bulk-Pfad `/iserver/account/orders/{orderId}`). Quelle:
  `docs/research/ibkr-cpapi-doc.json`.
- `cp/trades.py::_map_trade` mappt das IBKR-Live-Feld `account` (sowie
  `accountCode`) auf das v1-Vertragsfeld `account_id` und leitet die
  Currency aus `listing_exchange` ab. Eine kleine Tabelle deckt die fuer
  U25235077 relevanten Boersen ab (NYSE/NASDAQ/ARCA -> USD,
  IBIS/FWB/AEB -> EUR, LSE -> GBP usw.); ohne Match faellt der Adapter
  auf den USD-Default zurueck und setzt `currency_assumed=True`.
  Ein explizit gesetztes `currency`-Feld (FX-Cash-Trades) schlaegt den
  Exchange-Lookup weiterhin.
- `availability.py`: Prefix `Z` (Frozen) und `Y` (Frozen Delayed) laut
  IBKR-OpenAPI-Spec ergaenzt - `ZB` taucht in realen Marketdata-
  Antworten auf und wurde bisher als unbekannt gemeldet.
- `tests/cp_mock/replay.py`: Mock-Order-Status-Pfad und Mock-Trade-Body
  an das IBKR-Live-Schema angeglichen (Singular-Pfad, `account` +
  `listing_exchange` statt `account_id` + `currency`). Flag
  `omit_trade_currency` entfernt nun zusaetzlich `listing_exchange`,
  damit der Fallback-Pfad weiterhin getestet wird.

### Hinweis
- v1-API-Vertrag unveraendert. Reine Adapter-Schicht. Zweite von vier
  Sub-Karten in AP-02 #07. Live-Recording der korrigierten Pfade folgt
  in der vierten Sub-Karte.

## [1.6.1] — 2026-04-27

### Geaendert
- Portfolio-Adapter (`cp/portfolio.py`) auf die laut IBKR-Doku korrekten
  REST-Pfade umgestellt: `GET /portfolio/{accountId}/summary` (nativ
  statt aggregiert), `GET /portfolio/{accountId}/positions/{pageId}` mit
  Pagination (Default-Pagesize 30) und `GET /portfolio/{accountId}/ledger`.
  Vorher: nicht-existente `/iserver/account/{aid}/portfolio` und
  Singular-Varianten ohne `/portfolio`-Prefix - Live-Recording in
  AP-02 #04 lieferte HTTP 404. Erste von vier Sub-Karten in AP-02 #07.
- `normalize_summary_money` in `broker_gateway.money`: konvertiert das
  IBKR-Summary-Feld-Schema `{amount, currency, value, isNull, timestamp}`
  in `Money` und respektiert `isNull=True`.
- `tests/cp_mock/replay.py` und seed-Recordings (`tests/fixtures/recorded/seed/`)
  auf die neuen Pfade umgestellt; alte `iserver_account_U25235077_*`-Seeds
  geloescht. Throttle-Klassifizierung (`throttle/manager.py`) zieht mit.

### Hinweis
- v1-API-Vertrag unveraendert. Reine Adapter-Korrektur. Live-Recordings
  fuer die korrigierten Pfade existieren bereits aus AP-02 #04 (v1.3.0)
  und werden vom Replay-Loader vorrangig gegenueber den Seeds verwendet.

## [1.6.0] — 2026-04-26

### Hinzugefuegt
- Drift-Detection: `scripts/check_mock_drift.py` vergleicht
  Live-CP-Gateway-Antworten gegen `tests/fixtures/recorded/live/` und
  schreibt `reports/drift/<YYYY-MM-DD>.md` mit Klassifikation pro
  Endpunkt (no/minor/value/breaking). Exit 0 bei nur additivem/value
  drift, Exit 1 bei breaking drift, Exit 3 wenn `/iserver/auth/status`
  nicht authentifiziert ist.
- `tests/cp_mock/diff.py` als Single Source of Truth fuer Drift-Logik
  (`DiffReport`, `diff_recording`, `DEFAULT_IGNORE_FIELDS`). 28 Unit-
  Tests in `tests/test_drift_diff.py` decken alle Klassifikationsfaelle
  ab (added/removed/type-change/value-change/null-Edges/Listen-Diff).
- `scripts/recording_session.py refresh <fixture>`-Subkommando: zeigt
  Diff vorher und ersetzt eine einzelne Fixture nur nach expliziter
  Bestaetigung. CI-Modus mit `--yes`.
- 10 Tests in `tests/test_check_mock_drift.py` (MockTransport-basiert):
  no/additive/breaking/value-Drift, Status-Code-Aenderung, Skip-Logik
  fuer Order-Endpunkte und 4xx/5xx-Recordings, Markdown-Rendering.
- `docs/runbooks/mock-drift-check.md` mit Reaktion pro Drift-Klasse,
  Refresh-Workflow und Troubleshooting.
- `docs/cp-recordings.md` Section "Drift Detection" + "Refresh".
- Erster eingecheckter Drift-Bericht unter `reports/drift/2026-04-26.md`
  (9 no drift, 6 value drift, 0 breaking drift, 7 uebersprungen).

### Geaendert
- README-Status auf v1.6.0; AP-02 (Live-IBKR-Validierung) abgeschlossen.

### Behoben
- `diff_recording`: beidseitiges `None` wird nicht mehr als
  `value drift` mit Note `filled-in` gemeldet. Vorher hat jedes
  optionale, dauerhaft leere Feld bei jedem Live-Lauf einen
  Lauer-Eintrag erzeugt.

### Bekannt
- Order-Endpunkte (`/orders`, `/order/`) und Session-Wechsler (`/logout`,
  `/reauthenticate`) werden vom Drift-Check uebersprungen - Mock fuer
  Order bleibt seed/erste-Live-Aufzeichnung.
- Service-Code-Pfad-Bugs (cp/portfolio.py, cp/orders.py, cp/lifecycle.py)
  bleiben offen unter Folgekarte 813fed62 (siehe v1.3.0-Bekannt).

## [1.5.0] — 2026-04-25

### Hinzugefuegt
- Vereinheitlichtes Error-Modell `{error: {code, message, request_id,
  retry_after_s, extra}}` fuer **alle** v1-Endpunkte (Section 1.6 final).
  Single Source of Truth: `src/broker_gateway/api/v1/errors.py`.
  Globale Exception-Handler in `main.py` uebersetzen `HTTPException`,
  `RequestValidationError` und die neue `CPGatewayError` ins Schema.
- `scripts/recording_session.py error-path` provoziert IBKR-Fehler:
  Pacing-Violation, ungueltige conid, ungueltige Order-Quantity,
  nicht-existente Order-ID, optional Reauth-Fail (`--with-reauth-fail`).
- 7 Live-Error-Recordings unter `tests/fixtures/recorded/live/errors/`
  + Manifest. Wertvollster Fund: IBKR liefert generisches HTTP 500/503
  statt 4xx — Service-Code-Mapping muss aus dem Body-Inhalt schliessen.
  Bonus aus dem Reauth-Fail-Lauf: `/iserver/auth/status` bleibt nach
  `/logout` erreichbar mit `{authenticated: false, established: false,
  competing: false, connected: false, MAC: null}` — das ist das
  zuverlaessige Signal fuer `cp/lifecycle.py`, in `AUTH_LOST` zu kippen.
  `/reauthenticate` ohne Session liefert HTML 404 (kein JSON).
- `tests/test_error_model.py` mit 14 Tests (5 Pflicht-Cases plus
  Default-Code-Mapping-Parametrisierung).
- `docs/runbooks/recording-session-error-path.md` mit Reset-Anleitung
  nach Reauth-Fail und Diff-Bewertung des ersten Live-Laufs.

### Geaendert
- `cp/quotes.py::_call_snapshot` differenziert HTTP 429 jetzt explizit
  als `cp_pacing_violation` mit `Retry-After`-Header statt allgemeines
  `cp_upstream_error`.
- `cp/lifecycle.py::require_session_ok` setzt `code: "auth_lost"` und
  `retry_after_s: 30` im Detail.
- `auth/middleware.py` setzt explizit `missing_token`, `invalid_token`,
  `missing_scope` mit `required_scope` im `extra`.
- 3 Tests strukturell angepasst: `body["detail"]` -> `body["error"]["message"]`
  (test_auth, test_quotes_snapshot, test_events_stream) — kein Test-Intent
  geaendert, nur das Lese-Schema.

### Bekannt — fuer Folgekarte 813fed62
- IBKR liefert HTTP 500/503 fuer Anwendungs-Fehler. Der Service-Code
  sollte CP-Gateway-Bodies inspizieren und in semantische `code`-Werte
  uebersetzen (z.B. Body enthaelt "is not found" -> `not_found`,
  Body enthaelt "is not valid" -> `invalid_input`).
- IBKR-Pacing griff im ersten Live-Lauf nicht (60 Calls/s = alle 200 OK).
  Re-Test sobald IBKR-Wartung vorbei ist.

## [1.3.0] — 2026-04-25

### Hinzugefuegt
- Live-Recording-Session gegen das Konto **U25235077**: 22 JSON-Fixtures
  unter `tests/fixtures/recorded/live/` mit Manifest. `scripts/recording_session.py
  happy-path` ist voll implementiert (siehe
  `docs/runbooks/recording-session-happy-path.md`).
- IBKR Client Portal Web API Swagger-Snapshot in
  `docs/research/ibkr-cpapi-doc.json` als Quelle der Wahrheit fuer
  Endpunkt-Pfade.
- Diff-Report seed vs. live mit konkreten Funden im Runbook.

### Geaendert
- `tests/cp_mock/loader.py`: live-Recordings mit HTTP 4xx/5xx fallen auf
  seed zurueck — schuetzt Tests vor dokumentarischen Beweis-Recordings,
  ohne den Single-Source-of-Truth-Anspruch fuer happy-path-Bodies aufzugeben.
- `src/broker_gateway/cp/instruments.py`: `_map_search_entry` liest
  `sections[0].secType` als Fallback (Live-Schema), `_map_info` nimmt
  `ticker`/`listingExchange` als Fallbacks, `search()` filtert auf das
  primaere STK-Listing (IBKR liefert pro Symbol mehrere Listings).
- 3 Tests strukturell gelockert (tickle session, replay-loader MAC,
  instruments exchange) — akzeptieren jetzt sowohl seed-konkreten
  als auch live-normalisierten Wert.

### Bekannt — fuer Folgekarte AP-02 #X
- `cp/portfolio.py` nutzt **falsche Pfade**, die in der IBKR-Doku gar nicht
  existieren: `/iserver/account/{acct}/{portfolio,positions,ledger}`.
  Korrekt waere `/portfolio/{acct}/{summary,positions/{pageId},ledger}`.
- `cp/orders.py:95` Order-Status-Pfad `/iserver/account/orders/{id}` ist
  ein Bulk-Endpoint, korrekt waere `/iserver/account/order/status/{id}`.
- `cp/lifecycle.py` ruft `/iserver/accounts` nicht auf — IBKR antwortet
  ohne diesen Init mit 404 auf account-spezifische Endpunkte.
- `cp/lifecycle.py` Keep-Alive ueber `/tickle` — IBKR-Doku empfiehlt
  explizit `GET /sso/validate` jede Minute. Plus: 24h-Hard-Limit fuer
  Re-Auth, das vom Service nicht signalisiert wird.
- `cp/lifecycle.py::reauthenticate` ohne `?force=true`. IBKR-Doku
  erlaubt bei `competing: true` ein Force-Reclaim — broker-gateway als
  dokumentierter Single-Owner sollte das nutzen koennen.
- Snapshot-Prime-Verhalten: bei Polling kommen Werte sofort, kein Prime.

## [1.2.0] — 2026-04-25
- Mock-Fixture liest seed-Recordings ueber Replay-Loader. ReplayCPGatewayMock
  ersetzt MockCPGateway. tests/cp_mock-Modul mit Loader (live > seed).

## [1.1.0] — 2026-04-25
- CPRecorder + normalize_response fuer Live-Recordings. ENV
  `BG_CP_RECORD_DIR` aktiviert den Recorder; Header-Filter
  (Authorization/Cookie/Set-Cookie/X-API-Key); ID-/Timestamp-Sanitisierung
  in Bodies.

## [1.0.x] — 2026-04-23 bis 2026-04-25
- 1.0.4 Doku-Patch.
- 1.0.3 cpgateway-Container laeuft als non-root mit Host-User-Mapping.
- 1.0.2 CP-Gateway-Default-Base-URL um `/v1/api`-Prefix erweitert.
- 1.0.1 CP-Gateway-Container scharfgeschaltet inkl. Browser-2FA-Login-Runbook.
- 1.0.0 Erste vollstaendige Release: Observability (structured JSON-Logs +
  Prometheus `/metrics`).

## [0.x] — Foundation
- 0.12.0 Rate-Limit-Throttle.
- 0.11.0 Events-Stream (SSE).
- 0.10.0 Trades-History + MTD-Commission-Aggregat.
- 0.9.0 Order-Lifecycle mit Idempotency-Key + Reply-Confirmation-Loop.
- 0.8.0 Portfolio-Endpunkte mit Money-Normalisierung.
- 0.7.0 SSE-Quotes-Stream mit Refcount + Fan-Out.
- 0.6.0 Quotes-Snapshot mit First-Call-Prime + Availability-Normalisierung.
- 0.5.0 Instruments-Lookup mit Symbol-Cache.
- 0.4.0 CP-Gateway-Auth-Lifecycle inkl. `/v1/internal/health`.
- 0.3.0 Auth-Modell mit Token-Management.
- 0.2.0 pytest-Mock-Fixture.
- 0.1.0 `/v1/health`.
