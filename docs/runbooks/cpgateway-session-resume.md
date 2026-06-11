# Runbook: CP-Gateway Session wieder aufnehmen (Reauth-Pfad)

Wiederaufnahme einer pausierten oder verlorenen IBKR-Session **ohne**
Browser-2FA, solange das SSO-Cookie im Container noch gueltig ist
(typisch nach Laptop-Sleep, kurzem CP-Gateway-Restart oder
Tunnel-Pause).

> **Wann ist dieser Pfad richtig?** Vor dem manuellen Browser-Login.
> Erst wenn der Reauth-Pfad zwei Mal scheitert, eskalieren auf
> [`cpgateway-login.md`](cpgateway-login.md). Diese Reihenfolge spart
> die 2FA-Push/QR-Tortur in 90 Prozent der Faelle.

## 1. Anlass-Erkennung

| Trigger | Wo sichtbar |
|---------|-------------|
| `503 Service Unavailable` mit `Retry-After: 30` an Business-Endpunkten | Consumer-Side (PSM, trading_robot, eigene Curl-Calls). |
| `auth_status: "auth_lost"` bzw. `cp_reachable: false` | `GET /v1/internal/health` (mit Bootstrap-Token). |
| `authenticated: false, connected: false` direkt am CP-Gateway | `GET /v1/api/iserver/auth/status` im Container. |
| Geplante Pause-Wiederaufnahme | nach Laptop-Sleep, geplanter Wartung, Compose-Stop ohne Container-Recreate. |

Wenn der `broker-gateway`-Container kurz neu gestartet wurde, der
`broker-cpgateway`-Container aber durchgelaufen ist, ist der
Reauth-Pfad fast immer ausreichend.

## 2. Schritt 1 - Reauthenticate

Auf dem Ziel-Host (cma-pi-1) im laufenden cpgateway-Container:

```bash
docker exec broker-cpgateway curl -sS \
  --cookie-jar /tmp/cj.txt --cookie /tmp/cj.txt \
  -X POST http://localhost:5000/v1/api/iserver/reauthenticate
```

**Erfolg**: HTTP 200 mit User-Profile-JSON (`RESULT:true`,
`USER_NAME`, `TOKEN`, `EXPIRES`).

**Fehlschlag**: HTTP 401, HTTP 5xx oder leerer Body. Springe direkt zu
**Schritt 5 (Eskalation)** - der Reauth-Pfad ist nicht moeglich.

> Curl-Variante mit dem broker-gateway-Public-API:
> `POST /v1/admin/cp/reauthenticate` (existiert heute nicht; manueller
> Aufruf laeuft direkt am cpgateway-Container).

## 3. Schritt 2 - 90 s Warmup-Pause

```bash
sleep 90
```

**Begruendung:** Direkt nach `reauthenticate` liefert IBKR teils
unvollstaendige Field-Sets (Drift-Check sieht `breaking drift`,
fehlende `allExchanges`, `chineseName`, str-zu-float-Typkippe). Das
ist Cold-Session-Effekt, kein Vertrags-Bruch. Nach 60-90 s ist die
Session warm.

## 4. Schritt 3 - Erster Drift-Check

```bash
docker exec broker-cpgateway curl -sS \
  --cookie-jar /tmp/cj.txt --cookie /tmp/cj.txt \
  http://localhost:5000/v1/api/iserver/auth/status
```

**Erfolgskriterium:**
```json
{"authenticated": true, "established": true, "connected": true, ...}
```

Alternativ via broker-gateway-Service:
```bash
curl -sS -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
  http://localhost:4000/v1/internal/health
```
→ `auth_status: "ok"`, `cp_reachable: true`.

**Bei Erfolg**: fertig, Service ist wieder voll funktionsfaehig.

**Bei Misserfolg**: weiter zu Schritt 4.

## 5. Schritt 4 - Zweite 90 s Pause + zweiter Check

```bash
sleep 90
```

Dann **denselben** Drift-Check wiederholen wie in Schritt 3. Bei
Erfolg: fertig. Bei Misserfolg: weiter zu Schritt 5.

> Empirie (AP-02 #06, 2026-04-26, IBKR Build 10.44.1h): Bei einem
> Live-Lauf zeigte der erste Drift-Check 3 breaking drifts; der
> zweite Lauf (2 Minuten spaeter) lieferte 0 breaking drifts auf
> identischer Fixture. Cold-Session-Effekt, kein Schema-Bruch.

## 6. Schritt 5 - Eskalation zu Browser-2FA

Wenn beide Drift-Checks fehlschlagen, ist das SSO-Cookie wirklich
abgelaufen. Folge ab hier dem Standard-Login-Pfad in
[`cpgateway-login.md`](cpgateway-login.md):

1. SSH-Tunnel auf 5000 aufbauen.
2. Browser-Login mit Username + Passwort + 2FA des aktiven
   Live-Kontos (bis zum Cutover am 2026-06-08 war das `U25235077` —
   siehe [`account-cutover.md`](account-cutover.md)).
3. Health-Check via `/v1/internal/health`.

## 7. Wann der Reauth-Pfad **nicht** ausreicht

| Symptom | Diagnose | Aktion |
|---------|----------|--------|
| `competing: true` im `auth/status` | paralleler Login von der IBKR-Mobile-App; Cookie ungueltig. | Erst Mobile-App-Session beenden, dann Browser-Login. |
| `reauthenticate` liefert 401 | SSO-Cookie endgueltig expired (typ. > 12 h Pause). | Direkt Browser-Login. |
| `/iserver/auth/status` antwortet nicht (Connection-Refused) | cpgateway-Container down oder Compose-Netzwerk gestoert. | `docker compose ps cpgateway` pruefen, ggf. `restart`. |
| broker-gateway-Service-Logs zeigen `consecutive_reauth_failures > 3` | Der automatische Tickle-/Reauth-Loop hat schon mehrfach versagt. | Service-Logs lesen, dann diesem Runbook folgen. |

## 8. Bezug zu anderen Komponenten

- **Automatischer Reauth**: `src/broker_gateway/cp/lifecycle.py`
  hat einen `AuthLifecycle`-Loop, der bis zu 3 mal automatisch
  reauthenticate ruft. Dieses Runbook ist **nur** fuer den manuellen
  Eingriff, wenn das automatische Recovery fehlgeschlagen ist
  (`auth_status: "auth_lost"`).
- **Drift-Check-Wiederholung**: `scripts/check_mock_drift.py` sollte
  laut [`mock-drift-check.md`](mock-drift-check.md) immer zwei Mal
  laufen mit 90 s Pause - das setzt genau diesen Cold-Session-Effekt
  voraus.
- **Memory-Quelle**: `project_ibkr_session_resume` (lokal beim User);
  dieses Runbook ist die SSOT, die Memory-Notiz dient als Backup-
  Wissensquelle.
