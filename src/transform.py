"""
Normalize raw SAP Sales Order JSON into DuckDB tables.

Tables (stubs with real shapes):
  sales_order_header  — one row per SalesOrder
  sales_order_item    — one row per (SalesOrder, SalesOrderItem)

Rules:
  - SAP identifiers kept as strings (VARCHAR)
  - OData V2 /Date(ms)/ → TIMESTAMP
  - Monetary amounts → Decimal (not float)
  - Validate primary keys and header↔item relationships
"""

from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"

# OData V2 ASP.NET JSON date: /Date(1609459200000)/ or /Date(1609459200000+0000)/
_ODATA_DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")

HEADER_MONEY_FIELDS = ("TotalNetAmount",)
ITEM_MONEY_FIELDS = ("NetAmount", "GrossAmount")


class TransformValidationError(Exception):
    """Raised when primary-key or relationship checks fail."""


def parse_odata_v2_date(value: Any) -> str | None:
    """
    Convert an OData V2 date string to an ISO-8601 UTC timestamp string.

    Example: '/Date(1609459200000)/' → '2021-01-01T00:00:00+00:00'
    Non-matching values are returned unchanged as str (or None if empty).
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return str(value)

    match = _ODATA_DATE_RE.match(value.strip())
    if not match:
        return value

    from datetime import datetime, timezone

    millis = int(match.group(1))
    dt = datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    return dt.isoformat()


def to_decimal(value: Any) -> Decimal | None:
    """Parse monetary values with decimal precision (never float)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TransformValidationError(f"Invalid monetary value: {value!r}") from exc


def _unwrap_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    d = payload.get("d", payload)
    if isinstance(d, dict) and "results" in d:
        results = d["results"]
        return results if isinstance(results, list) else []
    if isinstance(d, dict):
        return [d]
    return []


def load_raw_json_files(raw_dir: Path = RAW_DIR) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load header and item payloads from data/raw/*.json."""
    headers: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    if not raw_dir.exists():
        return headers, items

    for path in sorted(raw_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = _unwrap_results(payload)
        name = path.name.lower()
        if "items" in name or "to_item" in name:
            items.extend(rows)
        elif "a_sales_order" in name or "salesorder" in name:
            headers.extend(rows)
        else:
            logger.debug("Skipping unrecognized raw file: %s", path.name)

    return headers, items


def normalize_headers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project header fields; preserve ids as str; convert dates and money."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        sales_order = str(row.get("SalesOrder", "")).strip()
        if not sales_order:
            raise TransformValidationError("Header missing primary key SalesOrder")

        record = {
            "sales_order": sales_order,
            "sales_order_type": _as_str(row.get("SalesOrderType")),
            "sold_to_party": _as_str(row.get("SoldToParty")),
            "sales_organization": _as_str(row.get("SalesOrganization")),
            "creation_date": parse_odata_v2_date(row.get("CreationDate")),
            "sales_order_date": parse_odata_v2_date(row.get("SalesOrderDate")),
            "transaction_currency": _as_str(row.get("TransactionCurrency")),
            "total_net_amount": to_decimal(row.get("TotalNetAmount")),
            "overall_delivery_status": _as_str(row.get("OverallDeliveryStatus")),
        }
        normalized.append(record)
    return normalized


def normalize_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project item fields; composite key (sales_order, sales_order_item) as strings."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        sales_order = str(row.get("SalesOrder", "")).strip()
        sales_order_item = str(row.get("SalesOrderItem", "")).strip()
        if not sales_order or not sales_order_item:
            raise TransformValidationError(
                "Item missing primary key fields SalesOrder / SalesOrderItem"
            )

        record = {
            "sales_order": sales_order,
            "sales_order_item": sales_order_item,
            "material": _as_str(row.get("Material")),
            "requested_quantity": to_decimal(row.get("RequestedQuantity")),
            "requested_quantity_unit": _as_str(row.get("RequestedQuantityUnit")),
            "net_amount": to_decimal(row.get("NetAmount")),
            "transaction_currency": _as_str(row.get("TransactionCurrency")),
        }
        normalized.append(record)
    return normalized


def _as_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _records_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare normalized records field-by-field (Decimal-safe via ==)."""
    if left.keys() != right.keys():
        return False
    return all(left[k] == right[k] for k in left)


def deduplicate_headers(headers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Deduplicate headers by sales_order.

    Identical normalized duplicates collapse to one row; conflicting values for the
    same key raise TransformValidationError.
    """
    by_key: dict[str, dict[str, Any]] = {}
    removed = 0
    for record in headers:
        key = record["sales_order"]
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = record
            continue
        if _records_equal(existing, record):
            removed += 1
            continue
        raise TransformValidationError(
            f"Conflicting duplicate header for sales_order={key!r}: "
            f"existing={existing!r} vs incoming={record!r}"
        )

    if removed:
        logger.info("Removed %s duplicate header record(s)", removed)
    return list(by_key.values())


def deduplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Deduplicate items by (sales_order, sales_order_item).

    Identical normalized duplicates collapse to one row; conflicting values for the
    same key raise TransformValidationError.
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    removed = 0
    for record in items:
        key = (record["sales_order"], record["sales_order_item"])
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = record
            continue
        if _records_equal(existing, record):
            removed += 1
            continue
        raise TransformValidationError(
            f"Conflicting duplicate item for "
            f"(sales_order, sales_order_item)={key!r}: "
            f"existing={existing!r} vs incoming={record!r}"
        )

    if removed:
        logger.info("Removed %s duplicate item record(s)", removed)
    return list(by_key.values())


def validate_relationships(
    headers: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> None:
    """Ensure header PKs are unique and every item references an existing header."""
    header_ids = [h["sales_order"] for h in headers]
    if len(header_ids) != len(set(header_ids)):
        raise TransformValidationError("Duplicate SalesOrder primary keys in headers")

    header_set = set(header_ids)
    item_keys: set[tuple[str, str]] = set()
    for item in items:
        key = (item["sales_order"], item["sales_order_item"])
        if key in item_keys:
            raise TransformValidationError(f"Duplicate item primary key: {key}")
        item_keys.add(key)
        if item["sales_order"] not in header_set:
            raise TransformValidationError(
                f"Orphan item: SalesOrder={item['sales_order']!r} has no header"
            )


def ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create target tables if they do not exist; migrate missing columns in place."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_order_header (
            sales_order              VARCHAR PRIMARY KEY,
            sales_order_type         VARCHAR,
            sold_to_party            VARCHAR,
            sales_organization       VARCHAR,
            creation_date            TIMESTAMP,
            sales_order_date         TIMESTAMP,
            transaction_currency     VARCHAR,
            total_net_amount         DECIMAL(18, 2),
            overall_delivery_status  VARCHAR
        )
        """
    )
    # Existing DBs created before this column: add in place (do not delete the DB file).
    conn.execute(
        """
        ALTER TABLE sales_order_header
        ADD COLUMN IF NOT EXISTS overall_delivery_status VARCHAR
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_order_item (
            sales_order              VARCHAR,
            sales_order_item         VARCHAR,
            material                 VARCHAR,
            requested_quantity       DECIMAL(18, 3),
            requested_quantity_unit  VARCHAR,
            net_amount               DECIMAL(18, 2),
            transaction_currency     VARCHAR,
            PRIMARY KEY (sales_order, sales_order_item)
        )
        """
    )


def load_into_duckdb(
    headers: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    db_path: Path | str,
) -> None:
    """Replace table contents with the normalized rows (atomic BEGIN/COMMIT/ROLLBACK)."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(path))
    try:
        ensure_tables(conn)
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM sales_order_item")
            conn.execute("DELETE FROM sales_order_header")

            for row in headers:
                conn.execute(
                    """
                    INSERT INTO sales_order_header VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        row["sales_order"],
                        row["sales_order_type"],
                        row["sold_to_party"],
                        row["sales_organization"],
                        row["creation_date"],
                        row["sales_order_date"],
                        row["transaction_currency"],
                        row["total_net_amount"],
                        row["overall_delivery_status"],
                    ],
                )

            for row in items:
                conn.execute(
                    """
                    INSERT INTO sales_order_item VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        row["sales_order"],
                        row["sales_order_item"],
                        row["material"],
                        row["requested_quantity"],
                        row["requested_quantity_unit"],
                        row["net_amount"],
                        row["transaction_currency"],
                    ],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        logger.info(
            "Loaded %s headers and %s items into %s",
            len(headers),
            len(items),
            path,
        )
    finally:
        conn.close()


def run_transform(*, raw_dir: Path = RAW_DIR, db_path: Path | str | None = None) -> Path:
    """End-to-end: raw JSON → normalize → dedupe → validate → DuckDB."""
    load_dotenv()
    if db_path is None:
        db_path = PROJECT_ROOT / os.getenv("DUCKDB_PATH", "data/sales_analytics.duckdb")

    raw_headers, raw_items = load_raw_json_files(raw_dir)
    headers = normalize_headers(raw_headers)
    items = normalize_items(raw_items)
    headers = deduplicate_headers(headers)
    items = deduplicate_items(items)
    validate_relationships(headers, items)
    load_into_duckdb(headers, items, db_path=db_path)
    return Path(db_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db = run_transform()
    logger.info("Transform complete: %s", db)


if __name__ == "__main__":
    main()
