#!/usr/bin/env python3
"""
Bulk importer for Seaspan/JiBe PMS "Spares" records.

Reads spare part rows from a CSV file and POSTs each one to:
    POST /api/technical/pms/spares/spare-details/saveSpareDetails

Before running:
  1. Log into the app in your browser, grab the current `session` token
     (query param on any XHR call, e.g. from DevTools > Network), and set
     the SESSION_TOKEN env var (or paste it into CONFIG below).
  2. Fill in input_spares.csv (a template is generated on first run if
     missing) with your part data using MAKER NAME / MODEL NAME / etc.
     -- this script resolves those human-readable names to the UIDs the
     API actually requires, so you don't need to hunt down UIDs by hand.

Usage:
    export SESSION_TOKEN="eyJhbGciOi..."
    python3 bulk_import_spares.py input_spares.csv results.csv

Requires: pip install requests --break-system-packages
"""

import csv
import os
import sys
import time
import json
import requests

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
BASE_URL = "https://seaspan-j3.jibe.solutions"
SESSION_TOKEN = os.environ.get("SESSION_TOKEN", "")  # paste token here if not using env var

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": BASE_URL,
    "referer": BASE_URL + "/",
    "jibe-trace-id": "",  # filled per-request below
}

REQUEST_DELAY_SECONDS = 15  # required delay AFTER each successful POST, before the next
MAX_RETRIES = 3


def _session_qs():
    if not SESSION_TOKEN:
        sys.exit("ERROR: SESSION_TOKEN is not set. Export it or edit CONFIG in this script.")
    return {"session": SESSION_TOKEN}


# --------------------------------------------------------------------------
# LOOKUP RESOLUTION (name -> UID)
# --------------------------------------------------------------------------
def fetch_brands():
    """maker name -> uid"""
    url = f"{BASE_URL}/api/technical/pms/lib/brands/get-brands"
    resp = requests.get(url, params=_session_qs(), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return {row["name"].strip().upper(): row["uid"] for row in resp.json()}


def fetch_models():
    """model name -> uid  (paginated grid endpoint; pulls all pages)"""
    url = f"{BASE_URL}/api/technical/pms/lib/model/get-all-lib-model"
    mapping = {}
    skip = 0
    page_size = 200
    while True:
        body = {
            "gridFilters": [
                {"type": "dropdown", "odataKey": "model_status", "includeFilter": True, "selectedValues": 1},
                {"type": "dropdown", "odataKey": "model_verification_status", "includeFilter": True, "selectedValues": 1},
            ],
            "gridSearch": None,
            "odata": {
                "$skip": str(skip),
                "$top": str(page_size),
                "$filter": "(model_status eq 1) and (model_verification_status eq 1)",
            },
        }
        resp = requests.post(url, params=_session_qs(), headers=HEADERS, json=body, timeout=30)
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if not records:
            break
        for r in records:
            key = r["modelName"].strip().upper()
            mapping[key] = r["model_uid"]
        if len(records) < page_size:
            break
        skip += page_size
    return mapping


def fetch_master_list(data_type_hint):
    """
    Generic fetch for /api/master/master/get_data lookups
    (units of measurement, criticality, etc).

    NOTE: the real frontend encrypts the query params that select *which*
    master list to return, inside the `session`-style token itself, so this
    endpoint can't be called generically the way brands/models can. Easiest
    reliable approach: open the relevant dropdown in the app once, copy the
    resulting name->uid pairs from the Network tab response, and paste them
    into the LOOKUPS dict below as a static table. They rarely change.
    """
    raise NotImplementedError(
        "See docstring: capture unit_of_measurement / criticality lists manually "
        "from the app once and hardcode them in LOOKUPS below."
    )


# Static lookups you fill in once (name -> uid), since they're resolved via
# an endpoint whose selector params aren't trivially replayable (see above).
LOOKUPS = {
    "unit_of_measurement": {
        "EACH": "84E76ED2-AD3F-47E9-9B68-EFE47B2EFA4A",
    },
    "critical_status": {
        "CRITICAL": "0834A81B-E41E-4C9C-9960-1D9EEE4122E3",
        "ESSENTIAL": "495CDB64-5728-4D21-9DD8-09676AB45772",
        "NOT CRITICAL": "1F3A9F87-3970-454B-97BB-BAFA7CB1EC71",
        # Alias: source spreadsheets often say "Non Critical" -- the system's
        # actual label is "Not Critical". Mapped to the same UID so either
        # spelling in your CSV resolves correctly.
        "NON CRITICAL": "1F3A9F87-3970-454B-97BB-BAFA7CB1EC71",
    },
}


# --------------------------------------------------------------------------
# PAYLOAD BUILDING
# --------------------------------------------------------------------------
def build_payload(row, brands, models):
    def uid_lookup(table, name, field_label):
        if not name:
            return None
        key = name.strip().upper()
        uid = table.get(key)
        if uid is None:
            raise ValueError(f"Could not resolve {field_label} '{name}' to a UID")
        return uid

    model_names = [m.strip() for m in row.get("model_names", "").split(";") if m.strip()]
    model_uids = [uid_lookup(models, m, "model") for m in model_names]

    payload = {
        "basic_information": {
            "part_name": row.get("part_name", "").strip(),
            "part_number": row.get("part_number", "").strip(),
            "description": row.get("description", "").strip(),
            "unit_of_measurement": uid_lookup(
                LOOKUPS["unit_of_measurement"], row.get("unit_of_measurement", ""), "unit_of_measurement"
            ),
            "critical_status": uid_lookup(
                LOOKUPS["critical_status"], row.get("critical_status", ""), "critical_status"
            ),
            "maker": uid_lookup(brands, row.get("maker", ""), "maker"),
            "drawing_number": row.get("drawing_number", "").strip(),
            "drawing_position": row.get("drawing_position", "").strip(),
            "material": None,
            "dimensions": row.get("dimensions", "").strip(),
        },
        "additional_information": {
            "minimum_quantity": row.get("minimum_quantity", "").strip(),
            "maximum_quantity": row.get("maximum_quantity", "").strip(),
            "kit": [],
            "price_OEM": row.get("price_OEM", "").strip(),
            "price_original": row.get("price_original", "").strip(),
            "price_replacement": row.get("price_replacement", "").strip(),
            "dangerous_goods": row.get("dangerous_goods", "").strip().lower() in ("true", "yes", "1"),
            "expiry_range": row.get("expiry_range", "").strip(),
            "operational_category": "",
            "tag": [],
            "attachments": {},
            "operationalCategory": [],
            "model": model_uids,
            "modelname": None,
        },
    }
    return payload


# --------------------------------------------------------------------------
# SAVE CALL
# --------------------------------------------------------------------------
def save_spare(payload, trace_id):
    url = f"{BASE_URL}/api/technical/pms/spares/spare-details/saveSpareDetails"
    headers = dict(HEADERS)
    headers["jibe-trace-id"] = trace_id

    print("  --- REQUEST ---")
    print(f"  POST {url}")
    print(f"  Payload: {json.dumps(payload)}")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, params=_session_qs(), headers=headers, json=payload, timeout=30)
            print("  --- RESPONSE ---")
            print(f"  Status: {resp.status_code}")
            print(f"  Body: {resp.text[:1000]}")
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except requests.RequestException as e:
            print(f"  --- RESPONSE ---\n  Network error: {e}")
            last_error = str(e)
        if attempt < MAX_RETRIES:
            time.sleep(1.5 * attempt)
    raise RuntimeError(last_error)


# --------------------------------------------------------------------------
# CSV TEMPLATE
# --------------------------------------------------------------------------
CSV_COLUMNS = [
    "part_name", "part_number", "description",
    "unit_of_measurement", "critical_status", "maker",
    "drawing_number", "drawing_position", "dimensions",
    "minimum_quantity", "maximum_quantity",
    "price_OEM", "price_original", "price_replacement",
    "dangerous_goods", "expiry_range",
    "model_names",  # semicolon-separated list of model names
]


def write_template(path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerow({
            "part_name": "SEALING SET, LPU-D v2a",
            "part_number": "160G4281",
            "description": "SEALING SET FOR LPU-D v2a",
            "unit_of_measurement": "EACH",
            "critical_status": "CRITICAL",
            "maker": "FD51EFAB-2F27-464A-9A40-FD544EFA214F",  # or a name if you populate LOOKUPS
            "drawing_number": "",
            "drawing_position": "",
            "dimensions": "",
            "minimum_quantity": "",
            "maximum_quantity": "",
            "price_OEM": "",
            "price_original": "",
            "price_replacement": "",
            "dangerous_goods": "false",
            "expiry_range": "",
            "model_names": "0.25m3",
        })
    print(f"Template written to {path}. Fill it in and re-run.")


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        input_path = "input_spares.csv"
    else:
        input_path = sys.argv[1]

    output_path = sys.argv[2] if len(sys.argv) > 2 else "results.csv"

    if not os.path.exists(input_path):
        write_template(input_path)
        return

    print("Fetching maker (brand) lookup table...")
    brands = fetch_brands()
    print(f"  {len(brands)} makers loaded.")

    print("Fetching model lookup table...")
    models = fetch_models()
    print(f"  {len(models)} models loaded.")

    if not LOOKUPS["unit_of_measurement"] or not LOOKUPS["critical_status"]:
        print(
            "\nWARNING: LOOKUPS['unit_of_measurement'] / LOOKUPS['critical_status'] "
            "are empty. Populate them near the top of this script (see docstring "
            "on fetch_master_list) before running for real, or pass raw UIDs "
            "directly in those CSV columns instead of names.\n"
        )

    results = []
    with open(input_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Importing {len(rows)} spare(s)...\n")
    for i, row in enumerate(rows, start=1):
        part_no = row.get("part_number", f"row{i}")
        print(f"[{i}/{len(rows)}] Part Number: {part_no}")

        # Build payload first; if a lookup can't be resolved, SKIP (don't guess, don't send).
        try:
            payload = build_payload(row, brands, models)
        except ValueError as e:
            print(f"  SKIPPED - {e}\n")
            results.append({"part_number": part_no, "status": "SKIPPED", "uid": "", "error": str(e)})
            continue

        trace_id = f"bulk-import-{i:05d}"
        try:
            resp = save_spare(payload, trace_id)
            result_obj = resp.get("resultObject", {})
            spare = result_obj.get("spareResult", {})
            status = "OK" if result_obj.get("uniqueSpareStatus") else "DUPLICATE_OR_ERROR"
            print(f"  RESULT: {status} (uid={spare.get('uid')})\n")
            results.append({
                "part_number": part_no,
                "status": status,
                "uid": spare.get("uid", ""),
                "error": "",
            })
            # Required delay AFTER a successful POST, before the next one.
            if i < len(rows):
                print(f"  Waiting {REQUEST_DELAY_SECONDS}s before next request...\n")
                time.sleep(REQUEST_DELAY_SECONDS)
        except Exception as e:
            print(f"  RESULT: FAILED - {e}\n")
            results.append({
                "part_number": part_no,
                "status": "FAILED",
                "uid": "",
                "error": str(e),
            })
            # Continue immediately on failure (no successful POST to space out from).

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["part_number", "status", "uid", "error"])
        writer.writeheader()
        writer.writerows(results)

    ok = [r for r in results if r["status"] == "OK"]
    dup = [r for r in results if r["status"] == "DUPLICATE_OR_ERROR"]
    failed = [r for r in results if r["status"] == "FAILED"]
    skipped = [r for r in results if r["status"] == "SKIPPED"]

    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"Total records:     {len(results)}")
    print(f"Successful:        {len(ok)}  {[r['part_number'] for r in ok]}")
    print(f"Duplicate/Error:   {len(dup)}  {[r['part_number'] for r in dup]}")
    print(f"Failed:            {len(failed)}  {[r['part_number'] for r in failed]}")
    print(f"Skipped:           {len(skipped)}  {[r['part_number'] for r in skipped]}")
    print(f"\nFull details written to {output_path}.")


if __name__ == "__main__":
    main()