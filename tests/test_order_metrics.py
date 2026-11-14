"""Regression tests for header-based order counts and item-completeness DQ."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from src.order_metrics import (
    build_filter_clause,
    load_data_quality,
    load_order_completeness,
    load_orders_without_items,
)
from src.sorting import sql_natural_id_order


HEADER_COUNT = 50
ORDERS_WITH_ITEMS = 48
ORDERS_WITHOUT_ITEMS = 2
# Orders 38 and 39 mirror the sandbox observation (zero items, zero header amount)
MISSING_ORDER_IDS = ("38", "39")
# One order carries three item rows; remaining with-items orders have one each
MULTI_ITEM_ORDER = "1"
MULTI_ITEM_COUNT = 3


def _create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE sales_order_header (
            sales_order            VARCHAR PRIMARY KEY,
            sales_order_type       VARCHAR,
            sold_to_party          VARCHAR,
            sales_organization     VARCHAR,
            creation_date          TIMESTAMP,
            sales_order_date       TIMESTAMP,
            transaction_currency   VARCHAR,
            total_net_amount       DECIMAL(18, 2)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sales_order_item (
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


def _seed_regression_fixture(conn: duckdb.DuckDBPyConnection) -> None:
    """
    50 headers, 48 with items, 2 without (38, 39), order 1 with three items.

    Item rows total = (47 * 1) + 3 = 50, matching the observed sandbox shape.
    """
    headers = []
    items = []
    for n in range(1, HEADER_COUNT + 1):
        order_id = str(n)
        # Spread customers / dates for filter tests
        customer = "CUST_A" if n % 2 == 1 else "CUST_B"
        order_date = f"2024-01-{(n % 28) + 1:02d}"
        if order_id in MISSING_ORDER_IDS:
            headers.append(
                (order_id, "OR", customer, "1710", None, order_date, "USD", 0.00)
            )
            continue
        # Multi-item order: three lines that sum to header total
        if order_id == MULTI_ITEM_ORDER:
            headers.append(
                (order_id, "OR", customer, "1710", None, order_date, "USD", 30.00)
            )
            for line, material in enumerate(("MAT_X", "MAT_Y", "MAT_Z"), start=1):
                items.append(
                    (order_id, str(line * 10), material, 1.0, "EA", 10.00, "USD")
                )
            continue
        amount = float(10 + n)
        headers.append(
            (order_id, "OR", customer, "1710", None, order_date, "USD", amount)
        )
        material = "MAT_X" if n <= 25 else "MAT_Y"
        items.append((order_id, "10", material, 1.0, "EA", amount, "USD"))

    conn.executemany(
        """
        INSERT INTO sales_order_header VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        headers,
    )
    conn.executemany(
        """
        INSERT INTO sales_order_item VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        items,
    )


@pytest.fixture()
def conn() -> duckdb.DuckDBPyConnection:
    database = duckdb.connect(":memory:")
    _create_schema(database)
    _seed_regression_fixture(database)
    yield database
    database.close()


def test_fixture_shape(conn: duckdb.DuckDBPyConnection) -> None:
    assert conn.execute(
        "SELECT COUNT(DISTINCT sales_order) FROM sales_order_header"
    ).fetchone()[0] == HEADER_COUNT
    assert conn.execute(
        "SELECT COUNT(*) FROM sales_order_item"
    ).fetchone()[0] == 50
    assert conn.execute(
        "SELECT COUNT(DISTINCT sales_order) FROM sales_order_item"
    ).fetchone()[0] == ORDERS_WITH_ITEMS
    multi = conn.execute(
        """
        SELECT COUNT(*) FROM sales_order_item WHERE sales_order = ?
        """,
        [MULTI_ITEM_ORDER],
    ).fetchone()[0]
    assert multi == MULTI_ITEM_COUNT


def test_unfiltered_completeness_counts(conn: duckdb.DuckDBPyConnection) -> None:
    result = load_order_completeness(conn, [], [], None, None)
    assert result["total_sales_orders"] == HEADER_COUNT
    assert result["orders_with_items"] == ORDERS_WITH_ITEMS
    assert result["orders_without_items"] == ORDERS_WITHOUT_ITEMS
    assert result["material_filter_active"] is False
    assert result["orders_matching_material"] is None


def test_orders_without_items_list(conn: duckdb.DuckDBPyConnection) -> None:
    df = load_orders_without_items(conn, [], None, None)
    assert sorted(df["sales_order"].tolist(), key=lambda x: int(x)) == list(
        MISSING_ORDER_IDS
    )
    assert list(df["sales_order"].tolist()) == list(MISSING_ORDER_IDS)  # natural order
    assert (df["header_total_net_amount"] == 0).all()


def test_dq_missing_items_not_monetary_mismatch(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    dq = load_data_quality(conn)
    assert len(dq) == HEADER_COUNT
    missing = dq[dq["status"] == "Missing items"]
    assert set(missing["sales_order"].tolist()) == set(MISSING_ORDER_IDS)
    assert (missing["item_count"] == 0).all()
    assert (missing["header_total_net_amount"] == 0).all()
    assert (missing["item_net_amount_sum"] == 0).all()
    # Zero+zero must not be classified as Mismatch
    assert (missing["status"] != "Mismatch").all()
    assert (dq["status"] == "Mismatch").sum() == 0
    assert (dq["status"] == "OK").sum() == ORDERS_WITH_ITEMS


def test_customer_filter_preserves_completeness_split(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = load_order_completeness(conn, ["CUST_A"], [], None, None)
    # Odd IDs 1..49 → 25 headers; missing 39 is CUST_A (odd)
    assert result["total_sales_orders"] == 25
    assert result["orders_without_items"] == 1
    assert result["orders_with_items"] == 24
    without = load_orders_without_items(conn, ["CUST_A"], None, None)
    assert without["sales_order"].tolist() == ["39"]


def test_date_filter_preserves_completeness(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    # Order 38 date = 2024-01-(38 % 28 + 1) = 2024-01-11
    result = load_order_completeness(
        conn, [], [], date(2024, 1, 11), date(2024, 1, 11)
    )
    assert result["total_sales_orders"] >= 1
    assert result["orders_without_items"] >= 1
    without = load_orders_without_items(
        conn, [], date(2024, 1, 11), date(2024, 1, 11)
    )
    assert "38" in without["sales_order"].tolist()


def test_material_filter_distinguishes_matching_from_total(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = load_order_completeness(conn, [], ["MAT_X"], None, None)
    assert result["material_filter_active"] is True
    # Total stays header-based (all 50) — material does not shrink Total Sales Orders
    assert result["total_sales_orders"] == HEADER_COUNT
    assert result["orders_with_items"] == ORDERS_WITH_ITEMS
    assert result["orders_without_items"] == ORDERS_WITHOUT_ITEMS
    # MAT_X: orders 2..25 (24) + order 1 (multi) = 25 matching
    assert result["orders_matching_material"] == 25
    assert result["orders_matching_material"] < result["total_sales_orders"]


def test_item_filter_clause_does_not_surface_header_only_orders(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    where_sql, params = build_filter_clause([], ["MAT_X"], None, None)
    rows = conn.execute(
        f"""
        SELECT DISTINCT h.sales_order
        FROM sales_order_header h
        INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
        WHERE {where_sql}
        ORDER BY {sql_natural_id_order("h.sales_order")}
        """,
        params,
    ).fetchall()
    order_ids = {r[0] for r in rows}
    assert MISSING_ORDER_IDS[0] not in order_ids
    assert MISSING_ORDER_IDS[1] not in order_ids
    assert MULTI_ITEM_ORDER in order_ids


def test_header_amounts_not_double_counted_after_item_join(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Summing header totals on a joined item result would inflate multi-item orders."""
    bad = conn.execute(
        """
        SELECT COALESCE(SUM(h.total_net_amount), 0)
        FROM sales_order_header h
        INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
        WHERE h.sales_order = ?
        """,
        [MULTI_ITEM_ORDER],
    ).fetchone()[0]
    good_header = conn.execute(
        """
        SELECT total_net_amount FROM sales_order_header WHERE sales_order = ?
        """,
        [MULTI_ITEM_ORDER],
    ).fetchone()[0]
    good_items = conn.execute(
        """
        SELECT COALESCE(SUM(net_amount), 0)
        FROM sales_order_item WHERE sales_order = ?
        """,
        [MULTI_ITEM_ORDER],
    ).fetchone()[0]
    assert float(bad) == float(good_header) * MULTI_ITEM_COUNT
    assert float(good_items) == float(good_header)
    assert float(bad) != float(good_items)
