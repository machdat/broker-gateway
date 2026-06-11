# Live-Recordings (Cassettes)

Die Recordings in diesem Verzeichnis (inkl. `errors/`) wurden gegen
das Konto **U25235077** aufgenommen — damals aktiver Live-Account,
bis zum Konto-Cutover am 2026-06-08 (siehe
[`docs/runbooks/account-cutover.md`](../../../../docs/runbooks/account-cutover.md)).

Sie sind **deterministische Mock-Daten** fuer die Replay-Schicht
(`tests/cp_mock/replay.py`) und **kein Hinweis auf das aktive
Live-Konto**. Die `account_id`-Felder bleiben absichtlich
`U25235077`: ein Umschreiben wuerde die Mock-Replay-Schicht und alle
darauf kalibrierten Tests anfassen, ohne fachlichen Gewinn.

Neuaufnahmen erfolgen erst bei konkretem Drift-Bedarf und dann gegen
das aktive Live-Konto (dediziertes Service-Konto; ID in Pi-`.env`,
bewusst nicht im Repo). Aufnahme-Workflow:
[`docs/runbooks/recording-session-happy-path.md`](../../../../docs/runbooks/recording-session-happy-path.md)
und [`docs/runbooks/recording-session-error-path.md`](../../../../docs/runbooks/recording-session-error-path.md);
Namensschema und Manifest-Format: [`../README.md`](../README.md).
