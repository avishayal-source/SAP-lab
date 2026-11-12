"""
Ingest SAP Sales Order sandbox responses into data/raw/ as JSON.

Flow:
  1. GET A_SalesOrder (headers)
  2. Persist raw JSON under data/raw/
  3. For each SalesOrder id, GET .../to_Item and persist raw JSON

This module never prints or logs the API key. Live calls only happen when
`run_ingest()` is invoked with a configured .env — importing this module
does not hit the network.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sap_client import SapClient

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def save_raw_json(payload: dict[str, Any], filename: str, *, raw_dir: Path = RAW_DIR) -> Path:
    """Write a raw API payload to data/raw/. Returns the written path."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("Wrote raw JSON: %s", path)
    return path


def extract_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Unwrap OData V2 JSON: { "d": { "results": [ ... ] } } or { "d": { ... } }.
    """
    d = payload.get("d", payload)
    if isinstance(d, dict) and "results" in d:
        results = d["results"]
        return results if isinstance(results, list) else []
    if isinstance(d, dict):
        return [d]
    return []


def run_ingest(*, top: int | None = 50, raw_dir: Path = RAW_DIR) -> dict[str, list[Path]]:
    """
    Fetch headers + items from the sandbox and land raw JSON files.

    Returns a mapping of entity → list of written paths.
    Requires SAP_API_KEY / SAP_BASE_URL in the environment (.env).
    """
    client = SapClient()
    written: dict[str, list[Path]] = {"headers": [], "items": []}
    ts = _timestamp()

    headers_payload = client.get_sales_orders(top=top)
    header_path = save_raw_json(headers_payload, f"a_sales_order_{ts}.json", raw_dir=raw_dir)
    written["headers"].append(header_path)

    for row in extract_results(headers_payload):
        # Preserve SAP identifier as string
        sales_order_id = str(row.get("SalesOrder", "")).strip()
        if not sales_order_id:
            logger.warning("Skipping header row without SalesOrder key")
            continue

        items_payload = client.get_sales_order_items(sales_order_id)
        safe_id = sales_order_id.replace("/", "_")
        item_path = save_raw_json(
            items_payload,
            f"a_sales_order_items_{safe_id}_{ts}.json",
            raw_dir=raw_dir,
        )
        written["items"].append(item_path)

    return written


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    paths = run_ingest()
    logger.info(
        "Ingest complete: %s header file(s), %s item file(s)",
        len(paths["headers"]),
        len(paths["items"]),
    )


if __name__ == "__main__":
    main()
