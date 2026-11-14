"""
Order-count and completeness metrics for the Streamlit dashboard.

Header presence and item presence are separate concerns from monetary
reconciliation. Order Value is sales-order net amount, not recognized revenue.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import pandas as pd

from src.sorting import sql_natural_id_order


def build_header_filter_clause(
    customers: list[str],
    start_date: date | None,
    end_date: date | None,
    *,
    alias: str = "h",
) -> tuple[str, list[Any]]:
    """WHERE fragment for header-level filters (customer + order date)."""
    clauses: list[str] = ["1=1"]
    params: list[Any] = []

    if customers:
        placeholders = ", ".join(["?"] * len(customers))
        clauses.append(f"{alias}.sold_to_party IN ({placeholders})")
        params.extend(customers)

    if start_date is not None:
        clauses.append(f"CAST({alias}.sales_order_date AS DATE) >= ?")
        params.append(start_date.isoformat())

    if end_date is not None:
        clauses.append(f"CAST({alias}.sales_order_date AS DATE) <= ?")
        params.append(end_date.isoformat())

    return " AND ".join(clauses), params


def build_material_filter_clause(
    materials: list[str],
    *,
    alias: str = "i",
) -> tuple[str, list[Any]]:
    """WHERE fragment for item material filter (empty = no material constraint)."""
    if not materials:
        return "1=1", []
    placeholders = ", ".join(["?"] * len(materials))
    return f"{alias}.material IN ({placeholders})", list(materials)


def build_filter_clause(
    customers: list[str],
    materials: list[str],
    start_date: date | None,
    end_date: date | None,
) -> tuple[str, list[Any]]:
    """
    Shared WHERE for filtered *item-level* analytics (headers ``h`` + items ``i``).

    Material filters apply only to item rows; they never invent items for
    header-only orders.
    """
    header_sql, header_params = build_header_filter_clause(
        customers, start_date, end_date, alias="h"
    )
    material_sql, material_params = build_material_filter_clause(
        materials, alias="i"
    )
    return f"{header_sql} AND {material_sql}", header_params + material_params


def load_order_completeness(
    conn: duckdb.DuckDBPyConnection,
    customers: list[str],
    materials: list[str],
    start_date: date | None,
    end_date: date | None,
) -> dict[str, Any]:
    """
    Header-based order counts, independent of item-join value KPIs.

    - ``total_sales_orders``: COUNT(DISTINCT sales_order) on ``sales_order_header``
      after customer/date filters only (no material filter; no item join).
    - ``orders_with_items`` / ``orders_without_items``: among those headers,
      via EXISTS / NOT EXISTS on ``sales_order_item``.
    - ``orders_matching_material``: only when a material filter is active —
      distinct headers (after customer/date) that have at least one matching
      item. Unmatched headers are not forced into product-specific results.
    """
    header_where, header_params = build_header_filter_clause(
        customers, start_date, end_date, alias="h"
    )

    total_sales_orders = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT h.sales_order)
            FROM sales_order_header h
            WHERE {header_where}
            """,
            header_params,
        ).fetchone()[0]
    )

    orders_with_items = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT h.sales_order)
            FROM sales_order_header h
            WHERE {header_where}
              AND EXISTS (
                  SELECT 1
                  FROM sales_order_item i
                  WHERE i.sales_order = h.sales_order
              )
            """,
            header_params,
        ).fetchone()[0]
    )

    orders_without_items = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT h.sales_order)
            FROM sales_order_header h
            WHERE {header_where}
              AND NOT EXISTS (
                  SELECT 1
                  FROM sales_order_item i
                  WHERE i.sales_order = h.sales_order
              )
            """,
            header_params,
        ).fetchone()[0]
    )

    material_filter_active = bool(materials)
    orders_matching_material: int | None = None
    if material_filter_active:
        material_sql, material_params = build_material_filter_clause(
            materials, alias="i"
        )
        orders_matching_material = int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT h.sales_order)
                FROM sales_order_header h
                INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
                WHERE {header_where}
                  AND {material_sql}
                """,
                header_params + material_params,
            ).fetchone()[0]
        )

    return {
        "total_sales_orders": total_sales_orders,
        "orders_with_items": orders_with_items,
        "orders_without_items": orders_without_items,
        "orders_matching_material": orders_matching_material,
        "material_filter_active": material_filter_active,
    }


def load_orders_without_items(
    conn: duckdb.DuckDBPyConnection,
    customers: list[str],
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    """
    Header rows with zero item rows (NOT EXISTS).

    Uses customer/date filters only — material filters do not apply because
    these orders have no items to match.
    """
    header_where, header_params = build_header_filter_clause(
        customers, start_date, end_date, alias="h"
    )
    sql = f"""
        SELECT
            h.sales_order,
            h.sold_to_party AS customer,
            h.sales_order_date,
            h.transaction_currency AS currency,
            h.total_net_amount AS header_total_net_amount
        FROM sales_order_header h
        WHERE {header_where}
          AND NOT EXISTS (
              SELECT 1
              FROM sales_order_item i
              WHERE i.sales_order = h.sales_order
          )
        ORDER BY {sql_natural_id_order("h.sales_order")}
    """
    if header_params:
        return conn.execute(sql, header_params).fetchdf()
    return conn.execute(sql).fetchdf()


def load_data_quality(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """
    Full-database DQ over every header (ignores UI filters).

    Item presence and monetary reconciliation are separate statuses:
    - ``Missing items`` — zero item rows (even when header and item sums are both 0)
    - ``Mismatch`` — items exist but header vs item-sum differs by > 0.01
    - ``OK`` — items exist and amounts reconcile
    """
    sql = f"""
        SELECT
            h.sales_order,
            h.transaction_currency AS header_currency,
            h.total_net_amount AS header_total_net_amount,
            COALESCE(SUM(i.net_amount), 0) AS item_net_amount_sum,
            COUNT(i.sales_order_item) AS item_count,
            ROUND(
                CAST(h.total_net_amount AS DOUBLE)
                - CAST(COALESCE(SUM(i.net_amount), 0) AS DOUBLE),
                2
            ) AS difference,
            CASE
                WHEN COUNT(i.sales_order_item) = 0 THEN 'Missing items'
                WHEN ABS(
                    CAST(COALESCE(h.total_net_amount, 0) AS DOUBLE)
                    - CAST(COALESCE(SUM(i.net_amount), 0) AS DOUBLE)
                ) > 0.01 THEN 'Mismatch'
                ELSE 'OK'
            END AS status
        FROM sales_order_header h
        LEFT JOIN sales_order_item i ON h.sales_order = i.sales_order
        GROUP BY
            h.sales_order,
            h.transaction_currency,
            h.total_net_amount
        ORDER BY
            CASE status
                WHEN 'Missing items' THEN 0
                WHEN 'Mismatch' THEN 1
                ELSE 2
            END,
            ABS(
                CAST(COALESCE(header_total_net_amount, 0) AS DOUBLE)
                - CAST(item_net_amount_sum AS DOUBLE)
            ) DESC,
            {sql_natural_id_order("h.sales_order")}
    """
    return conn.execute(sql).fetchdf()
