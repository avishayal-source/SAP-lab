"""
SAP Sales Analytics — Streamlit dashboard (POC).

Read-only DuckDB analytics over SAP Sandbox sample sales orders.
Order Value here is sales-order net amount, not recognized revenue.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb
import pandas as pd
import streamlit as st

from src.order_metrics import (
    build_filter_clause,
    delivery_status_code_from_label,
    format_delivery_status,
    load_data_quality,
    load_delivery_status_distribution,
    load_order_completeness,
    load_orders_without_items,
)
from src.sorting import sql_natural_id_order

DB_PATH = PROJECT_ROOT / "data" / "sales_analytics.duckdb"


@contextmanager
def duckdb_readonly(path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open DuckDB in read-only mode and always close the connection."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def run_df(conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    if params:
        return conn.execute(sql, params).fetchdf()
    return conn.execute(sql).fetchdf()


def format_money(amount: float | None, currency: str) -> str:
    if amount is None or pd.isna(amount):
        return "—"
    return f"{currency} {amount:,.2f}"


def load_filter_options(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    customers = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT sold_to_party
            FROM sales_order_header
            WHERE sold_to_party IS NOT NULL AND sold_to_party <> ''
            ORDER BY 1
            """
        ).fetchall()
    ]
    materials = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT material
            FROM sales_order_item
            WHERE material IS NOT NULL AND material <> ''
            ORDER BY 1
            """
        ).fetchall()
    ]
    delivery_status_labels = sorted(
        {
            format_delivery_status(r[0])
            for r in conn.execute(
                """
                SELECT DISTINCT overall_delivery_status
                FROM sales_order_header
                """
            ).fetchall()
        }
    )
    date_row = conn.execute(
        """
        SELECT
            MIN(CAST(sales_order_date AS DATE)),
            MAX(CAST(sales_order_date AS DATE))
        FROM sales_order_header
        WHERE sales_order_date IS NOT NULL
        """
    ).fetchone()
    min_date = date_row[0] if date_row else None
    max_date = date_row[1] if date_row else None
    if isinstance(min_date, datetime):
        min_date = min_date.date()
    if isinstance(max_date, datetime):
        max_date = max_date.date()
    return {
        "customers": customers,
        "materials": materials,
        "delivery_status_labels": delivery_status_labels,
        "min_date": min_date,
        "max_date": max_date,
    }


def load_kpis(conn: duckdb.DuckDBPyConnection, where_sql: str, params: list[Any]) -> pd.DataFrame:
    """
    Currency-safe item-value KPIs after filters.

    Uses filtered *item* net amounts (not header totals) so a material filter
    does not inflate value with full-order header amounts. Totals stay per
    currency — never mixed. ``orders_with_matching_items`` is the item-join
    count for this currency slice, not the header-based Total Sales Orders.
    """
    sql = f"""
        SELECT
            i.transaction_currency AS currency,
            COUNT(DISTINCT h.sales_order) AS orders_with_matching_items,
            COUNT(DISTINCT h.sold_to_party) AS unique_customers,
            COUNT(DISTINCT i.material) AS unique_materials,
            COALESCE(SUM(i.net_amount), 0) AS filtered_item_value
        FROM sales_order_header h
        INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
        WHERE {where_sql}
        GROUP BY i.transaction_currency
        ORDER BY i.transaction_currency
    """
    return run_df(conn, sql, params)


def load_value_by_customer(
    conn: duckdb.DuckDBPyConnection, where_sql: str, params: list[Any]
) -> pd.DataFrame:
    sql = f"""
        SELECT
            h.sold_to_party AS customer,
            i.transaction_currency AS currency,
            COALESCE(SUM(i.net_amount), 0) AS filtered_item_value
        FROM sales_order_header h
        INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
        WHERE {where_sql}
        GROUP BY h.sold_to_party, i.transaction_currency
        ORDER BY filtered_item_value DESC, customer
    """
    return run_df(conn, sql, params)


def load_value_by_material(
    conn: duckdb.DuckDBPyConnection, where_sql: str, params: list[Any]
) -> pd.DataFrame:
    sql = f"""
        SELECT
            i.material AS material,
            i.transaction_currency AS currency,
            COALESCE(SUM(i.net_amount), 0) AS filtered_item_value
        FROM sales_order_header h
        INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
        WHERE {where_sql}
        GROUP BY i.material, i.transaction_currency
        ORDER BY filtered_item_value DESC, material
    """
    return run_df(conn, sql, params)


def load_value_by_month(
    conn: duckdb.DuckDBPyConnection, where_sql: str, params: list[Any]
) -> pd.DataFrame:
    sql = f"""
        SELECT
            strftime(h.sales_order_date, '%Y-%m') AS order_month,
            i.transaction_currency AS currency,
            COALESCE(SUM(i.net_amount), 0) AS filtered_item_value
        FROM sales_order_header h
        INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
        WHERE {where_sql}
          AND h.sales_order_date IS NOT NULL
        GROUP BY 1, 2
        ORDER BY order_month, currency
    """
    return run_df(conn, sql, params)


def load_detail(
    conn: duckdb.DuckDBPyConnection, where_sql: str, params: list[Any]
) -> pd.DataFrame:
    """
    One row per sales-order item. Header total is shown for context only and
    is NOT summed in the UI — value metrics use item net_amount.
    Orders without items are not listed here (see Orders without items).
    """
    sql = f"""
        SELECT
            h.sales_order,
            i.sales_order_item,
            h.sold_to_party AS customer,
            i.material,
            i.requested_quantity AS quantity,
            i.requested_quantity_unit AS unit,
            i.net_amount AS item_net_amount,
            i.transaction_currency AS currency,
            h.sales_order_date,
            CASE
                WHEN h.overall_delivery_status IS NULL
                     OR h.overall_delivery_status = ''
                    THEN 'Unknown / Not provided'
                WHEN h.overall_delivery_status = 'C' THEN 'Completed delivery'
                ELSE h.overall_delivery_status
            END AS overall_delivery_status,
            h.total_net_amount AS header_full_order_value
        FROM sales_order_header h
        INNER JOIN sales_order_item i ON h.sales_order = i.sales_order
        WHERE {where_sql}
        ORDER BY h.sales_order_date,
                 {sql_natural_id_order("h.sales_order", "i.sales_order_item")}
    """
    return run_df(conn, sql, params)


def render_empty_state(message: str) -> None:
    st.info(message)


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """
    Encode a DataFrame as CSV bytes for st.download_button.

    Uses UTF-8 with BOM so Excel on Windows opens Hebrew/special characters
    correctly. index=False keeps the export aligned with the on-screen table.
    """
    return df.to_csv(index=False).encode("utf-8-sig")


def render_csv_download(
    df: pd.DataFrame,
    *,
    label: str,
    file_name: str,
    key: str,
) -> None:
    """
    Reliable CSV export via browser download (works on Windows).

    Prefer this over the st.dataframe toolbar Download CSV control, which
    often opens a save dialog on Windows without writing the file.
    """
    if df.empty:
        return
    st.download_button(
        label=label,
        data=dataframe_to_csv_bytes(df),
        file_name=file_name,
        mime="text/csv",
        key=key,
        help="Downloads the table currently shown (after sidebar filters).",
    )


def main() -> None:
    st.set_page_config(
        page_title="SAP Sales Analytics",
        page_icon="📊",
        layout="wide",
    )

    st.title("SAP Sales Analytics")
    st.caption(
        "SAP Sandbox sample data · **Order Value** = sales-order net amount "
        "(not recognized / billed revenue)"
    )

    st.sidebar.header("Filters")
    st.sidebar.markdown(
        "Customer, date, and delivery-status filters apply to order counts and "
        "item analytics. Material filters scope product-specific item results only "
        "— they do not change Total Sales Orders. Data-quality checks always use "
        "the full database."
    )

    if not DB_PATH.exists():
        st.error(
            f"DuckDB database not found at `{DB_PATH}`.\n\n"
            "Run the project ingest/transform pipeline first, then refresh this page. "
            "This dashboard only reads existing local data (no SAP API calls)."
        )
        st.sidebar.warning("Database file missing — filters unavailable.")
        st.stop()

    try:
        with duckdb_readonly(DB_PATH) as conn:
            options = load_filter_options(conn)

            customers = st.sidebar.multiselect(
                "Customer (sold-to party)",
                options=options["customers"],
                default=[],
                help="Empty = all customers",
            )
            materials = st.sidebar.multiselect(
                "Material",
                options=options["materials"],
                default=[],
                help="Empty = all materials. Scopes item/product metrics only.",
            )
            delivery_labels = st.sidebar.multiselect(
                "Overall delivery status",
                options=options["delivery_status_labels"],
                default=[],
                help=(
                    "Empty = all statuses. C = Completed delivery; "
                    "blank/NULL = Unknown / Not provided (not 'Not delivered')."
                ),
            )
            delivery_statuses = [
                delivery_status_code_from_label(label) for label in delivery_labels
            ]

            min_d = options["min_date"]
            max_d = options["max_date"]
            if min_d is not None and max_d is not None:
                date_range = st.sidebar.date_input(
                    "Order date range",
                    value=(min_d, max_d),
                    min_value=min_d,
                    max_value=max_d,
                    help="Based on sales_order_date",
                )
                if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
                    start_date, end_date = date_range[0], date_range[1]
                elif isinstance(date_range, date):
                    start_date, end_date = date_range, date_range
                else:
                    start_date, end_date = min_d, max_d
            else:
                st.sidebar.caption("No order dates available in the database.")
                start_date, end_date = None, None

            where_sql, params = build_filter_clause(
                customers, materials, start_date, end_date, delivery_statuses
            )

            completeness = load_order_completeness(
                conn, customers, materials, start_date, end_date, delivery_statuses
            )
            without_items = load_orders_without_items(
                conn, customers, start_date, end_date, delivery_statuses
            )
            delivery_dist = load_delivery_status_distribution(
                conn, customers, start_date, end_date, delivery_statuses
            )
            kpis = load_kpis(conn, where_sql, params)
            by_customer = load_value_by_customer(conn, where_sql, params)
            by_material = load_value_by_material(conn, where_sql, params)
            by_month = load_value_by_month(conn, where_sql, params)
            detail = load_detail(conn, where_sql, params)
            dq = load_data_quality(conn)
    except duckdb.Error as exc:
        st.error(f"Could not read DuckDB database: {exc}")
        st.stop()

    st.markdown(
        """
**Metric notes**
- **Total Sales Orders** — `COUNT(DISTINCT sales_order)` from `sales_order_header` after customer/date/delivery-status filters (not item-join).
- **Orders With / Without Items** — item presence via `EXISTS` / `NOT EXISTS` (separate from money).
- **Overall delivery status** — SAP `OverallDeliveryStatus` on the header (`C` = Completed delivery; blank/NULL = Unknown / Not provided).
- **Filtered item value** — sum of item `net_amount` after filters (default for charts). Currency-safe; never mixes currencies.
- **Full-order value** — header `total_net_amount`; context only on detail rows; never summed after joining items.
- **Order Value ≠ recognized revenue.**
        """
    )

    st.subheader("Overall delivery status")
    st.caption(
        "Header distribution of SAP OverallDeliveryStatus. "
        "`C` → Completed delivery; blank/NULL → Unknown / Not provided "
        "(not 'Not delivered'). Other codes show as returned by SAP."
    )
    if delivery_dist.empty:
        render_empty_state("No headers match the current filters.")
    else:
        render_csv_download(
            delivery_dist,
            label="Download delivery-status CSV",
            file_name="overall_delivery_status_distribution.csv",
            key="download_delivery_status_csv",
        )
        chart_df = delivery_dist.set_index("delivery_status")[["order_count"]]
        st.bar_chart(chart_df, horizontal=True)
        st.dataframe(delivery_dist, use_container_width=True, hide_index=True)

    st.subheader("Order completeness")
    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Total Sales Orders",
        completeness["total_sales_orders"],
        help="COUNT(DISTINCT sales_order) from sales_order_header (customer/date filters only)",
    )
    c2.metric(
        "Orders With Items",
        completeness["orders_with_items"],
        help="Headers that have at least one sales_order_item row",
    )
    c3.metric(
        "Orders Without Items",
        completeness["orders_without_items"],
        help="Headers with zero item rows (NOT EXISTS) — missing items, not a money mismatch",
    )

    if completeness["material_filter_active"]:
        st.info(
            f"**Material filter active.** Total Sales Orders (header filters) = "
            f"{completeness['total_sales_orders']}. "
            f"Orders matching selected material(s) = "
            f"{completeness['orders_matching_material']}. "
            "Charts, currency KPIs, and the item detail table include only matching-item "
            "orders — unmatched headers are not forced into product-specific results."
        )

    if completeness["orders_without_items"] > 0:
        st.warning(
            f"{completeness['orders_without_items']} order(s) have **zero item rows**. "
            "This is an item-presence / completeness issue even when header "
            "`total_net_amount` and the item sum are both zero — it is **not** a "
            "monetary header-vs-item mismatch."
        )

    st.subheader("Key metrics (filtered item value)")
    if kpis.empty:
        render_empty_state(
            "No item rows match the current filters. Clear or widen customer, "
            "material, or date filters. Header-only orders still appear under "
            "Order completeness / Orders without items."
        )
    else:
        for _, row in kpis.iterrows():
            currency = row["currency"] or "—"
            st.markdown(f"**Currency: {currency}**")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(
                "Orders with matching items",
                int(row["orders_with_matching_items"]),
                help="Distinct orders in this currency after item-level filters — not Total Sales Orders",
            )
            k2.metric("Unique customers", int(row["unique_customers"]))
            k3.metric("Unique materials", int(row["unique_materials"]))
            k4.metric(
                "Total filtered item value",
                format_money(float(row["filtered_item_value"]), currency),
                help="Sum of item net amounts after filters — not recognized revenue",
            )

    st.subheader("Order value by customer")
    st.caption("Filtered item value by sold-to party (per currency).")
    if by_customer.empty:
        render_empty_state("No customer values for the current filters.")
    else:
        render_csv_download(
            by_customer,
            label="Download customer values CSV",
            file_name="order_value_by_customer.csv",
            key="download_by_customer_csv",
        )
        for currency, grp in by_customer.groupby("currency", dropna=False):
            st.markdown(f"**{currency}**")
            chart_df = grp.set_index("customer")[["filtered_item_value"]]
            st.bar_chart(chart_df, horizontal=True)
            st.dataframe(
                grp.rename(columns={"filtered_item_value": "filtered_item_value"}),
                use_container_width=True,
                hide_index=True,
            )

    st.subheader("Order value by material")
    st.caption("Filtered item value by material (per currency).")
    if by_material.empty:
        render_empty_state("No material values for the current filters.")
    else:
        render_csv_download(
            by_material,
            label="Download material values CSV",
            file_name="order_value_by_material.csv",
            key="download_by_material_csv",
        )
        for currency, grp in by_material.groupby("currency", dropna=False):
            st.markdown(f"**{currency}**")
            chart_df = grp.set_index("material")[["filtered_item_value"]]
            st.bar_chart(chart_df, horizontal=True)
            st.dataframe(grp, use_container_width=True, hide_index=True)

    st.subheader("Order value by month")
    st.caption("Filtered item value by sales order month (per currency).")
    if by_month.empty:
        render_empty_state("No monthly values for the current filters.")
    else:
        render_csv_download(
            by_month,
            label="Download monthly values CSV",
            file_name="order_value_by_month.csv",
            key="download_by_month_csv",
        )
        for currency, grp in by_month.groupby("currency", dropna=False):
            st.markdown(f"**{currency}**")
            chart_df = grp.set_index("order_month")[["filtered_item_value"]]
            st.bar_chart(chart_df)
            st.dataframe(grp, use_container_width=True, hide_index=True)

    st.subheader("Order / item detail")
    st.caption(
        "One row per item. Sum **item_net_amount** for value analytics. "
        "**header_full_order_value** is the complete order total (context only — do not sum it here). "
        "Orders without items are listed separately below. "
        "Use **Download detail CSV** below (not the table toolbar) to save on Windows."
    )
    if detail.empty:
        render_empty_state("No detail rows for the current filters.")
    else:
        render_csv_download(
            detail,
            label="Download detail CSV",
            file_name="sales_order_item_detail.csv",
            key="download_detail_csv",
        )
        st.dataframe(detail, use_container_width=True, hide_index=True)
        totals = (
            detail.groupby("currency", dropna=False)["item_net_amount"]
            .sum()
            .reset_index(name="sum_item_net_amount")
        )
        st.markdown("**Detail table totals (item net amount only)**")
        st.dataframe(totals, use_container_width=True, hide_index=True)

    st.subheader("Orders without items")
    st.caption(
        "Headers with zero `sales_order_item` rows (NOT EXISTS). "
        "Customer/date filters apply; material filters do not (no items to match). "
        "Zero header amount + zero items = missing items, not a monetary mismatch."
    )
    if without_items.empty:
        st.success("Every filtered header has at least one item row.")
    else:
        st.info(
            f"{len(without_items)} order(s) with no item rows "
            "(item-presence issue — separate from financial reconciliation)."
        )
        render_csv_download(
            without_items,
            label="Download orders-without-items CSV",
            file_name="orders_without_items.csv",
            key="download_without_items_csv",
        )
        st.dataframe(without_items, use_container_width=True, hide_index=True)

    st.subheader("Data quality — completeness & amounts")
    st.caption(
        "Full database (ignores sidebar filters). "
        "**Missing items** = zero item rows (even when both monetary totals are 0). "
        "**Mismatch** = items exist but header vs item-sum differs by > 0.01. "
        "These statuses are mutually exclusive."
    )
    if dq.empty:
        render_empty_state("No header rows available for the data-quality check.")
    else:
        missing = int((dq["status"] == "Missing items").sum())
        mismatches = int((dq["status"] == "Mismatch").sum())
        if missing:
            st.warning(
                f"{missing} order(s) have **zero item rows** (Missing items). "
                "Flagged even when header and item monetary totals are both zero."
            )
        if mismatches:
            st.warning(
                f"{mismatches} order(s) have header vs item-sum differences (> 0.01)."
            )
        if not missing and not mismatches:
            st.success(
                "All orders have items and header total_net_amount matches "
                "sum of item net_amount."
            )
        render_csv_download(
            dq,
            label="Download data-quality CSV",
            file_name="data_quality_header_vs_items.csv",
            key="download_dq_csv",
        )
        st.dataframe(dq, use_container_width=True, hide_index=True)

    st.divider()
    st.caption(
        f"Source: `{DB_PATH.name}` · SAP Business Accelerator Hub sandbox sample · "
        "Read-only dashboard · Order Value ≠ recognized revenue"
    )


if __name__ == "__main__":
    main()
