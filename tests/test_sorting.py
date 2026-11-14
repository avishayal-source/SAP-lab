"""Regression tests for natural sales_order / sales_order_item sorting."""

from __future__ import annotations

import duckdb
import pandas as pd

from src.sorting import (
    natural_id_sort_key,
    sort_dataframe_by_natural_ids,
    sql_natural_id_order,
)

# Required fixture IDs: numeric order + non-numeric + leading zeros
SAMPLE_ORDER_IDS = ["1", "2", "10", "13", "A100", "0002"]
EXPECTED_ORDER_IDS = ["1", "0002", "2", "10", "13", "A100"]


def test_sql_natural_id_order_fragment() -> None:
    assert sql_natural_id_order("h.sales_order") == (
        "TRY_CAST(h.sales_order AS BIGINT) NULLS LAST, h.sales_order"
    )
    assert sql_natural_id_order("h.sales_order", "i.sales_order_item") == (
        "TRY_CAST(h.sales_order AS BIGINT) NULLS LAST, h.sales_order, "
        "TRY_CAST(i.sales_order_item AS BIGINT) NULLS LAST, i.sales_order_item"
    )


def test_natural_id_sort_key_numeric_before_non_numeric() -> None:
    values = SAMPLE_ORDER_IDS
    ordered = sorted(values, key=natural_id_sort_key)
    assert ordered == EXPECTED_ORDER_IDS
    # Identifiers remain plain strings (leading zeros preserved)
    assert all(isinstance(v, str) for v in ordered)
    assert "0002" in ordered


def test_sort_dataframe_by_natural_ids_preserves_strings() -> None:
    df = pd.DataFrame(
        {
            "sales_order": SAMPLE_ORDER_IDS,
            "label": list("abcdef"),
        }
    )
    # Shuffle intentionally then sort
    shuffled = df.sample(frac=1.0, random_state=0).reset_index(drop=True)
    sorted_df = sort_dataframe_by_natural_ids(shuffled, ["sales_order"])
    assert sorted_df["sales_order"].tolist() == EXPECTED_ORDER_IDS
    assert all(isinstance(v, str) for v in sorted_df["sales_order"].tolist())
    assert sorted_df.loc[sorted_df["sales_order"] == "0002", "sales_order"].iloc[0] == "0002"


def test_duckdb_sql_order_matches_convention() -> None:
    """SQL ORDER BY pattern used by the dashboard — in-memory only, no project DB."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE t (sales_order VARCHAR)")
    conn.executemany(
        "INSERT INTO t VALUES (?)",
        [(v,) for v in SAMPLE_ORDER_IDS],
    )
    sql = f"""
        SELECT sales_order
        FROM t
        ORDER BY {sql_natural_id_order("sales_order")}
    """
    rows = [r[0] for r in conn.execute(sql).fetchall()]
    conn.close()
    assert rows == EXPECTED_ORDER_IDS
    assert rows[1] == "0002"  # leading zeros preserved in result


def test_duckdb_order_and_item_natural_sort() -> None:
    """Order-items table: natural sort on both sales_order and sales_order_item."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE items (sales_order VARCHAR, sales_order_item VARCHAR)"
    )
    rows_in = [
        ("10", "20"),
        ("2", "10"),
        ("A100", "10"),
        ("1", "20"),
        ("1", "10"),
        ("0002", "10"),
        ("13", "10"),
    ]
    conn.executemany("INSERT INTO items VALUES (?, ?)", rows_in)
    sql = f"""
        SELECT sales_order, sales_order_item
        FROM items
        ORDER BY {sql_natural_id_order("sales_order", "sales_order_item")}
    """
    out = conn.execute(sql).fetchall()
    conn.close()
    assert out == [
        ("1", "10"),
        ("1", "20"),
        ("0002", "10"),
        ("2", "10"),
        ("10", "20"),
        ("13", "10"),
        ("A100", "10"),
    ]
