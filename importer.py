"""
Seaspan J3 (Jibe Solutions PMS) — Spares Bulk Importer
========================================================

Reverse-engineered from seaspan_jibe_solutions.har. See HAR_ANALYSIS.md for
the full write-up of every request, the JSON schema, and the CSV mapping.

Usage:
    pip install aiohttp pyyaml
    python importer.py --config config.yaml

Key design notes (see HAR_ANALYSIS.md for the "why"):
  * Auth is a single JWT passed as `?session=...` on every request — no
    cookies, no CSRF token, no Authorization header exist in this app.
  * The login flow itself was NOT captured in the HAR, so this script cannot
    obtain a session token on its own. You must paste a live one into
    config.yaml. The script fails fast and clearly on 401/403.
  * `jibe-trace-id` is a client-generated UUID4 used for server-side tracing
    only — freshly generated per request here, exactly like the real app.
  * Per Phase 5 of the spec, rows are processed strictly sequentially with a
    configurable delay between requests (default 15s) — no concurrency.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yaml

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    base_url: str
    session_token: str
    lookup_tables: dict[str, str]
    delay_between_requests_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    request_timeout_seconds: float
    csv_input: str
    output_dir: str

    @staticmethod
    def load(path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return Config(
            base_url=raw["base_url"].rstrip("/"),
            session_token=raw["session_token"],
            lookup_tables=raw["lookup_tables"],
            delay_between_requests_seconds=float(raw.get("delay_between_requests_seconds", 15)),
            max_retries=int(raw.get("max_retries", 3)),
            retry_backoff_seconds=float(raw.get("retry_backoff_seconds", 5)),
            request_timeout_seconds=float(raw.get("request_timeout_seconds", 30)),
            csv_input=raw["csv_input"],
            output_dir=raw.get("output_dir", "output"),
        )


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("importer")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(output_dir / "execution.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(message)s"))
    ch.setLevel(logging.INFO)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #


class SessionExpiredError(Exception):
    pass


class JibeClient:
    """Thin wrapper around the J3 REST API, replaying exactly what the HAR shows."""

    def __init__(self, config: Config, session: aiohttp.ClientSession, logger: logging.Logger):
        self.config = config
        self.http = session
        self.log = logger

    def _headers(self) -> dict[str, str]:
        # Mirrors the header set seen on every /api/technical|master call in
        # the HAR. jibe-trace-id is regenerated per request, same as the app.
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": self.config.base_url,
            "referer": self.config.base_url + "/",
            "jibe-trace-id": str(uuid.uuid4()),
        }

    def _url(self, path: str, extra_query: str = "") -> str:
        sep = "&" if extra_query else ""
        return f"{self.config.base_url}{path}?session={self.config.session_token}{sep}{extra_query}"

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                        json_body: Optional[dict] = None) -> tuple[int, Any]:
        url = self._url(path)
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        async with self.http.request(
            method, url, params=params, json=json_body, headers=self._headers(), timeout=timeout
        ) as resp:
            status = resp.status
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = await resp.text()
            if status in (401, 403):
                raise SessionExpiredError(
                    f"{method} {path} returned {status} — the session token in config.yaml "
                    f"is expired or invalid. Log into the J3 app, grab a fresh `session` "
                    f"token, update config.yaml, and re-run."
                )
            return status, body

    # ---- Lookups (called once at startup, cached) ---- #

    async def get_brands(self) -> dict[str, str]:
        """Returns {maker_name_lower: uid}."""
        status, body = await self._request("GET", "/api/technical/pms/lib/brands/get-brands")
        if status != 200 or not isinstance(body, list):
            raise RuntimeError(f"get-brands failed: HTTP {status}: {body}")
        return {row["name"].strip().lower(): row["uid"] for row in body if row.get("name")}

    async def get_table_lookup(self, table_name: str) -> dict[str, str]:
        """Generic j3_pms_lib_* lookup, used for criticality / unit_of_measurement / material."""
        status, body = await self._request(
            "GET", "/api/master/master/get_data", params={"$orderby": "name asc", "$table": table_name}
        )
        if status != 200 or not isinstance(body, list):
            raise RuntimeError(
                f"Lookup table '{table_name}' failed (HTTP {status}). This table name was "
                f"NOT confirmed by the HAR — check HAR_ANALYSIS.md and correct config.yaml."
            )
        return {row["name"].strip().lower(): row["uid"] for row in body if row.get("name")}

    async def search_model(self, model_name: str) -> Optional[str]:
        """Replicates the get-all-lib-model search call; returns model_uid or None."""
        payload = {
            "gridFilters": [
                {"type": "dropdown", "odataKey": "model_status", "includeFilter": True, "selectedValues": 1},
                {"type": "dropdown", "odataKey": "model_verification_status", "includeFilter": True, "selectedValues": 1},
            ],
            "gridSearch": {"value": model_name, "columns": ["modelName", "series", "tag_name", "version"]},
            "odata": {
                "$skip": "0",
                "$filter": (
                    "(model_status eq 1) and (model_verification_status eq 1) and "
                    f"((contains(modelName, '{model_name}')) or (contains(series, '{model_name}')) or "
                    f"(contains(tag_name, '{model_name}')))"
                ),
                "$top": "25",
            },
        }
        status, body = await self._request(
            "POST", "/api/technical/pms/lib/model/get-all-lib-model", json_body=payload
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get-all-lib-model failed: HTTP {status}: {body}")
        records = body.get("records", [])
        for rec in records:
            if rec.get("modelName", "").strip().lower() == model_name.strip().lower():
                return rec.get("model_uid")
        # fall back to first contains-match if no exact match
        return records[0]["model_uid"] if records else None

    # ---- The create call ---- #

    async def save_spare_details(self, payload: dict) -> tuple[int, Any]:
        return await self._request(
            "POST", "/api/technical/pms/spares/spare-details/saveSpareDetails", json_body=payload
        )


# --------------------------------------------------------------------------- #
# CSV -> payload
# --------------------------------------------------------------------------- #

REQUIRED_COLUMNS = [
    "part_number", "part_name", "maker_name", "model_name",
    "criticality_name", "unit_of_measurement_name",
]


@dataclass
class RowResult:
    row_number: int
    part_number: str
    payload: Optional[dict] = None
    status_code: Optional[int] = None
    response_body: Any = None
    success: bool = False
    error: str = ""
    retry_count: int = 0
    execution_time_seconds: float = 0.0


def validate_row(row: dict, row_number: int) -> list[str]:
    errors = []
    for col in REQUIRED_COLUMNS:
        if not row.get(col, "").strip():
            errors.append(f"Row {row_number}: missing required column '{col}'")
    for numeric_col in ("minimum_quantity", "maximum_quantity", "price_oem", "price_original", "price_replacement"):
        val = row.get(numeric_col, "").strip()
        if val:
            try:
                float(val)
            except ValueError:
                errors.append(f"Row {row_number}: '{numeric_col}' must be numeric, got '{val}'")
    dg = row.get("dangerous_goods", "").strip().lower()
    if dg not in ("", "true", "false"):
        errors.append(f"Row {row_number}: 'dangerous_goods' must be true/false/blank, got '{dg}'")
    return errors


async def resolve_row_to_payload(
    row: dict, client: JibeClient, caches: dict, log: logging.Logger
) -> dict:
    maker_uid = caches["makers"].get(row["maker_name"].strip().lower())
    if not maker_uid:
        raise ValueError(f"Maker '{row['maker_name']}' not found via get-brands")

    crit_uid = caches["criticality"].get(row["criticality_name"].strip().lower())
    if not crit_uid:
        raise ValueError(f"Criticality '{row['criticality_name']}' not found")

    uom_uid = caches["uom"].get(row["unit_of_measurement_name"].strip().lower())
    if not uom_uid:
        raise ValueError(f"Unit of measurement '{row['unit_of_measurement_name']}' not found")

    material_uid = None
    material_name = row.get("material_name", "").strip()
    if material_name:
        material_uid = caches["material"].get(material_name.lower())
        if not material_uid:
            raise ValueError(f"Material '{material_name}' not found")

    model_name = row["model_name"].strip()
    model_uid = caches["models"].get(model_name.lower())
    if model_uid is None:
        model_uid = await client.search_model(model_name)
        caches["models"][model_name.lower()] = model_uid
    if not model_uid:
        raise ValueError(f"Model '{model_name}' not found via get-all-lib-model")

    def _num(key: str) -> str:
        return row.get(key, "").strip()

    tags = [t.strip() for t in row.get("tags", "").split("|") if t.strip()]

    return {
        "basic_information": {
            "part_name": row["part_name"].strip(),
            "part_number": row["part_number"].strip(),
            "description": row.get("description", "").strip(),
            "unit_of_measurement": uom_uid,
            "critical_status": crit_uid,
            "maker": maker_uid,
            "drawing_number": row.get("drawing_number", "").strip(),
            "drawing_position": row.get("drawing_position", "").strip(),
            "material": material_uid,
            "dimensions": row.get("dimensions", "").strip(),
        },
        "additional_information": {
            "minimum_quantity": _num("minimum_quantity"),
            "maximum_quantity": _num("maximum_quantity"),
            "kit": [],
            "price_OEM": _num("price_oem"),
            "price_original": _num("price_original"),
            "price_replacement": _num("price_replacement"),
            "dangerous_goods": row.get("dangerous_goods", "").strip().lower() == "true",
            "expiry_range": row.get("expiry_range", "").strip(),
            "operational_category": row.get("operational_category", "").strip(),
            "tag": tags,
            "attachments": {},
            "operationalCategory": [],
            "model": [model_uid],
            "modelname": None,
        },
    }


# --------------------------------------------------------------------------- #
# Main import loop
# --------------------------------------------------------------------------- #


async def run_import(config: Config) -> None:
    output_dir = Path(config.output_dir)
    log = setup_logging(output_dir)

    log.info("=== Seaspan J3 Spares Bulk Importer ===")
    log.info(f"Reading CSV: {config.csv_input}")

    with open(config.csv_input, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        log.error("CSV has no data rows. Nothing to do.")
        return

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing_cols:
        log.error(f"CSV is missing required columns: {missing_cols}")
        return

    validation_errors: list[str] = []
    for i, row in enumerate(rows, start=2):  # row 1 = header
        validation_errors.extend(validate_row(row, i))
    if validation_errors:
        for e in validation_errors:
            log.error(e)
        log.error(f"{len(validation_errors)} validation error(s) found. Fix the CSV and re-run.")
        return

    log.info(f"{len(rows)} row(s) validated OK. Connecting to API...")

    async with aiohttp.ClientSession() as http:
        client = JibeClient(config, http, log)

        try:
            log.info("Fetching lookup caches (brands, criticality, unit of measurement, material)...")
            caches = {
                "makers": await client.get_brands(),
                "criticality": await client.get_table_lookup(config.lookup_tables["criticality"]),
                "models": {},
            }
            try:
                caches["uom"] = await client.get_table_lookup(config.lookup_tables["unit_of_measurement"])
            except RuntimeError as e:
                log.error(str(e))
                log.error("Cannot proceed without a working unit-of-measurement lookup. Aborting.")
                return
            try:
                caches["material"] = await client.get_table_lookup(config.lookup_tables["material"])
            except RuntimeError as e:
                log.warning(f"Material lookup unavailable ({e}). Rows with material_name set will fail; "
                            f"rows without it are unaffected.")
                caches["material"] = {}
        except SessionExpiredError as e:
            log.error(str(e))
            return

        log.info(f"Loaded {len(caches['makers'])} makers, {len(caches['criticality'])} criticality levels, "
                  f"{len(caches['uom'])} units of measurement.")

        results: list[RowResult] = []

        for i, row in enumerate(rows, start=2):
            part_number = row["part_number"].strip()
            result = RowResult(row_number=i, part_number=part_number)
            start_time = datetime.now(timezone.utc)

            try:
                payload = await resolve_row_to_payload(row, client, caches, log)
                result.payload = payload
                log.info(f"[Row {i}] Payload:\n{json.dumps(payload, indent=2)}")

                attempt = 0
                while True:
                    attempt += 1
                    result.retry_count = attempt - 1
                    try:
                        status, body = await client.save_spare_details(payload)
                        result.status_code = status
                        result.response_body = body
                        if status == 200:
                            result.success = True
                            log.info(f"[Row {i}] SUCCESS (HTTP {status}): part_number={part_number}")
                        else:
                            result.error = f"HTTP {status}: {body}"
                            log.warning(f"[Row {i}] Non-200 response (attempt {attempt}): {result.error}")
                        break
                    except SessionExpiredError:
                        raise
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        result.error = f"Transient error: {e}"
                        log.warning(f"[Row {i}] Attempt {attempt} failed: {e}")
                        if attempt >= config.max_retries:
                            log.error(f"[Row {i}] Giving up after {attempt} attempts.")
                            break
                        await asyncio.sleep(config.retry_backoff_seconds)

            except SessionExpiredError as e:
                log.error(str(e))
                results.append(result)
                break
            except Exception as e:
                result.error = str(e)
                log.error(f"[Row {i}] FAILED before request could be sent: {e}")

            result.execution_time_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()
            results.append(result)

            if i < len(rows) + 1:
                log.info(f"Waiting {config.delay_between_requests_seconds}s before next row...")
                await asyncio.sleep(config.delay_between_requests_seconds)

    write_reports(output_dir, results, log)


def write_reports(output_dir: Path, results: list[RowResult], log: logging.Logger) -> None:
    success_path = output_dir / "success.csv"
    failed_path = output_dir / "failed.csv"

    with open(success_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row_number", "part_number", "http_status", "spare_uid", "execution_time_seconds"])
        for r in results:
            if r.success:
                spare_uid = ""
                if isinstance(r.response_body, dict):
                    spare_uid = (
                        r.response_body.get("resultObject", {})
                        .get("spareResult", {})
                        .get("uid", "")
                    )
                w.writerow([r.row_number, r.part_number, r.status_code, spare_uid, f"{r.execution_time_seconds:.2f}"])

    with open(failed_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row_number", "part_number", "http_status", "error", "retry_count"])
        for r in results:
            if not r.success:
                w.writerow([r.row_number, r.part_number, r.status_code or "", r.error, r.retry_count])

    total = len(results)
    ok = sum(1 for r in results if r.success)
    log.info(f"=== Done: {ok}/{total} succeeded. See {success_path} and {failed_path} ===")


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description="Seaspan J3 spares bulk importer")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    config = Config.load(args.config)
    if config.session_token == "PASTE_YOUR_SESSION_JWT_HERE" or not config.session_token:
        print("ERROR: config.yaml still has the placeholder session_token. "
              "Log into the J3 app, copy the `session` query-string value from any "
              "API call in DevTools -> Network, and paste it into config.yaml.")
        sys.exit(1)

    asyncio.run(run_import(config))


if __name__ == "__main__":
    main()
