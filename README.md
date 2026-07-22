# Running the importer

## 1. Install dependencies
```bash
pip install aiohttp pyyaml
```

## 2. Get a session token (the one gap the HAR couldn't answer)
The captured HAR starts mid-session — no login request is in it, so there's
no way to derive a login flow from this file alone. Instead:

1. Log into `https://seaspan-j3.jibe.solutions` in your browser.
2. Open DevTools → Network, do anything that hits the API (e.g. open the
   Spares library).
3. Copy the value of the `session` query-string parameter from any request
   URL — it's the long `eyJhbGci...` JWT.
4. Paste it into `config.yaml` → `session_token`.

It's valid for 48 hours from when it was issued. If the importer stops with
a "session expired/invalid" error mid-run, just repeat this and re-run —
already-succeeded rows are in `output/success.csv`, so you can remove them
from the CSV before re-running to avoid duplicates.

## 3. Verify the two unconfirmed lookup tables
`unit_of_measurement` and `material` table names in `config.yaml` are
inferred by naming convention, not confirmed by the HAR (see
`HAR_ANALYSIS.md`). The importer fetches them at startup and will fail
immediately, by design, if the name is wrong. If it does, open the "Add
Spare" form in the browser, expand the Unit of Measurement or Material
dropdown, check the Network tab for the `$table=...` value used, and correct
`config.yaml`.

## 4. Fill in the CSV
Edit `spares_import_template.csv` (columns and rules documented in
`HAR_ANALYSIS.md` → Phase 3). `maker_name`, `model_name`, `criticality_name`,
and `unit_of_measurement_name` must match existing values in the system —
the importer resolves them to UIDs live against the API, the same way the
web UI does, and will report a clear per-row error if a name doesn't match
anything.

## 5. Run it
```bash
python importer.py --config config.yaml
```

Each row is processed sequentially with a 15-second pause between requests
(configurable), retried up to 3 times on transient failures, and logged.

## 6. Check results
- `output/execution.log` — full timestamped log, including every payload sent
- `output/success.csv` — row #, part number, HTTP status, new spare UID
- `output/failed.csv` — row #, part number, HTTP status, error, retry count

## Notes on scope
- File attachments (`additional_information.attachments`) are not supported —
  no upload endpoint was captured in the HAR.
- The importer does not click any UI — it replays the exact
  `saveSpareDetails` API call the frontend makes, after resolving names to
  UIDs via the same lookup endpoints the frontend uses.
