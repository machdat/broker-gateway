# Runbook: 150-Symbol-smd-Stresstest

Manueller Live-Stresstest gegen das CP-Gateway auf cma-pi-1 mit 150
zufaelligen S&P-500-Symbolen. Pruefziel: PSM-Worst-Case-Skala (150
Symbole) bei trading_robot-SLO (p95 unter 150 ms) ohne
Throttling-/Drop-Eskalation. Bezug: AP-11 K8, K6-Sektion 4.4.

> **Voraussetzung:** AP-11 K3-Folgekarte (Lifespan-Wiring fuer
> `CPWebSocketClient` + `WSPushSource`) ist gemerged und live. Vor
> dieser Voraussetzung schlaegt der Stresstest fehl, weil `/v1/quotes/
> stream` keinen WS-Push als Quelle hat.

## Aufruf

```bash
ssh cma@cma-pi-1
cd /mnt/ssd/broker-gateway
BG_LIVE_STRESSTEST=1 BG_QUOTES_SOURCE=ws \
    docker compose exec gateway \
    pytest -m "live and stresstest" \
        tests/integration/test_smd_stresstest_150.py -v
```

## Erwartungswerte

| Metrik | Erwartung | Hard-Fail-Schwelle |
|--------|-----------|---------------------|
| p50-Latenz (CP-Receive bis Egress) | unter 100 ms | unter 200 ms |
| p95-Latenz | unter 150 ms | unter 250 ms |
| Drop-Counter pro Topic-Adapter | unter 1 % Frames | unter 5 % |
| 429-/Throttle-Events vom CP-Gateway | 0 | mehr als 0 = Pacing-Bug |

Robot-SLO: p95 unter 150 ms. Der Sicherheitspuffer von 100 ms in der
Hard-Fail-Schwelle deckt Netzwerkstreuung und schweren GC ab.

## Auswertung

Der Test schreibt Roh-Latenzen nach
``var/stresstest/smd_150_<timestamp>.csv`` (relativ zum Compose-
Volume). Auswertung mit `numpy` / `pandas` aus dem Repo-Notebook
``notebooks/stresstest_eval.ipynb`` (Folgekarte).

## Eskalation bei Fail

- **p95 > 250 ms:** Backpressure-Strategie pro Topic-Adapter
  ueberpruefen (Drop-Oldest reicht nicht?), Slow-Consumer-Drop pro
  Egress-Verbindung als Folgekarte aufnehmen.
- **Drop-Counter > 5 %:** Adapter-Queue-Limit zu klein gewaehlt;
  pro-Conid-Limit erhoehen oder Drop-Strategie umstellen.
- **429-Events:** ThrottleManager-Bucket fuer
  `/iserver/marketdata/snapshot` greift im Polling-Fallback ein;
  pruefen ob WS-Pfad doch nicht aktiv war.
