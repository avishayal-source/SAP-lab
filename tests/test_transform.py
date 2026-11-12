"""Unit tests for transform helpers — no network, no live SAP calls."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from src.transform import (
    TransformValidationError,
    deduplicate_headers,
    deduplicate_items,
    load_into_duckdb,
    normalize_headers,
    normalize_items,
    parse_odata_v2_date,
    to_decimal,
    validate_relationships,
)


def test_parse_odata_v2_date_epoch() -> None:
    # 2021-01-01T00:00:00Z in milliseconds
    assert parse_odata_v2_date("/Date(1609459200000)/") == "2021-01-01T00:00:00+00:00"


def test_parse_odata_v2_date_none() -> None:
    assert parse_odata_v2_date(None) is None
    assert parse_odata_v2_date("") is None


def test_to_decimal_preserves_precision() -> None:
    assert to_decimal("1234.56") == Decimal("1234.56")
    assert to_decimal(None) is None


def test_normalize_headers_keeps_sales_order_as_string() -> None:
    rows = [
        {
            "SalesOrder": 5000001,  # API may send numeric-looking values
            "SalesOrderType": "OR",
            "SoldToParty": "1000",
            "SalesOrganization": "1710",
            "CreationDate": "/Date(1609459200000)/",
            "SalesOrderDate": "/Date(1609459200000)/",
            "TransactionCurrency": "USD",
            "TotalNetAmount": "250.50",
        }
    ]
    out = normalize_headers(rows)
    assert out[0]["sales_order"] == "5000001"
    assert isinstance(out[0]["sales_order"], str)
    assert out[0]["total_net_amount"] == Decimal("250.50")
    assert out[0]["creation_date"] == "2021-01-01T00:00:00+00:00"


def test_normalize_items_composite_key_as_strings() -> None:
    rows = [
        {
            "SalesOrder": "5000001",
            "SalesOrderItem": 10,
            "Material": "TG11",
            "RequestedQuantity": "2.000",
            "RequestedQuantityUnit": "EA",
            "NetAmount": "100.00",
            "TransactionCurrency": "USD",
        }
    ]
    out = normalize_items(rows)
    assert out[0]["sales_order"] == "5000001"
    assert out[0]["sales_order_item"] == "10"
    assert out[0]["net_amount"] == Decimal("100.00")


def test_validate_relationships_rejects_orphan_item() -> None:
    headers = [{"sales_order": "1"}]
    items = [{"sales_order": "999", "sales_order_item": "10"}]
    with pytest.raises(TransformValidationError, match="Orphan item"):
        validate_relationships(headers, items)


def test_validate_relationships_ok(tmp_path: Path) -> None:
    headers = [{"sales_order": "1"}]
    items = [{"sales_order": "1", "sales_order_item": "10"}]
    validate_relationships(headers, items)  # does not raise
    assert tmp_path.exists()  # pytest tmp_path fixture sanity


def _sample_header(sales_order: str = "5000001", **overrides: object) -> dict:
    row = {
        "sales_order": sales_order,
        "sales_order_type": "OR",
        "sold_to_party": "1000",
        "sales_organization": "1710",
        "creation_date": "2021-01-01T00:00:00+00:00",
        "sales_order_date": "2021-01-01T00:00:00+00:00",
        "transaction_currency": "USD",
        "total_net_amount": Decimal("250.50"),
    }
    row.update(overrides)
    return row


def _sample_item(
    sales_order: str = "5000001",
    sales_order_item: str = "10",
    **overrides: object,
) -> dict:
    row = {
        "sales_order": sales_order,
        "sales_order_item": sales_order_item,
        "material": "TG11",
        "requested_quantity": Decimal("2.000"),
        "requested_quantity_unit": "EA",
        "net_amount": Decimal("100.00"),
        "transaction_currency": "USD",
    }
    row.update(overrides)
    return row


def test_deduplicate_headers_keeps_one_identical_duplicate(caplog: pytest.LogCaptureFixture) -> None:
    first = _sample_header()
    duplicate = _sample_header()
    with caplog.at_level("INFO"):
        out = deduplicate_headers([first, duplicate, _sample_header("5000002")])
    assert len(out) == 2
    assert out[0]["sales_order"] == "5000001"
    assert out[1]["sales_order"] == "5000002"
    assert any("Removed 1 duplicate header" in msg for msg in caplog.messages)


def test_deduplicate_headers_raises_on_conflicting_duplicate() -> None:
    left = _sample_header(total_net_amount=Decimal("100.00"))
    right = _sample_header(total_net_amount=Decimal("200.00"))
    with pytest.raises(TransformValidationError, match="Conflicting duplicate header"):
        deduplicate_headers([left, right])


def test_deduplicate_items_keeps_one_identical_duplicate(caplog: pytest.LogCaptureFixture) -> None:
    first = _sample_item()
    duplicate = _sample_item()
    other = _sample_item(sales_order_item="20")
    with caplog.at_level("INFO"):
        out = deduplicate_items([first, duplicate, other])
    assert len(out) == 2
    assert {(r["sales_order"], r["sales_order_item"]) for r in out} == {
        ("5000001", "10"),
        ("5000001", "20"),
    }
    assert any("Removed 1 duplicate item" in msg for msg in caplog.messages)


def test_deduplicate_items_raises_on_conflicting_duplicate() -> None:
    left = _sample_item(net_amount=Decimal("100.00"))
    right = _sample_item(net_amount=Decimal("150.00"))
    with pytest.raises(TransformValidationError, match="Conflicting duplicate item"):
        deduplicate_items([left, right])


def test_load_into_duckdb_rolls_back_on_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "sales.duckdb"
    seed_header = _sample_header("1")
    seed_item = _sample_item("1", "10")
    load_into_duckdb([seed_header], [seed_item], db_path=db_path)

    # Duplicate primary keys force INSERT failure after DELETE inside the transaction.
    conflicting_headers = [_sample_header("2"), _sample_header("2")]
    with pytest.raises(Exception):
        load_into_duckdb(conflicting_headers, [], db_path=db_path)

    conn = duckdb.connect(str(db_path))
    try:
        headers = conn.execute(
            "SELECT sales_order FROM sales_order_header ORDER BY sales_order"
        ).fetchall()
        items = conn.execute(
            "SELECT sales_order, sales_order_item FROM sales_order_item"
        ).fetchall()
    finally:
        conn.close()

    assert headers == [("1",)]
    assert items == [("1", "10")]
