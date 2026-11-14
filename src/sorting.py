"""
Natural sort helpers for SAP business identifiers kept as VARCHAR/string.

Convention (use for every table/query/dataframe that displays these IDs)
----------------------------------------------------------------------
``sales_order`` and ``sales_order_item`` are SAP business identifiers.
They must remain VARCHAR/string in DuckDB, joins, filters, and displayed
values — never permanently cast to integers.

When ordering for display (SQL):

    ORDER BY TRY_CAST(<id> AS BIGINT) NULLS LAST, <id>

Effects:
- Numeric-only IDs sort numerically: 1, 2, 10, 13 (not 1, 10, 13, 2).
- Leading zeros are preserved in the displayed string (e.g. ``0002``).
- Non-numeric IDs sort lexicographically after all numeric IDs
  (``TRY_CAST`` yields NULL → ``NULLS LAST``, then string tie-break).

Helpers
-------
- ``sql_natural_id_order(*cols)`` — fragment for SQL ``ORDER BY``
- ``natural_id_sort_key(value)`` — key for Python / pandas sorting
- ``sort_dataframe_by_natural_ids(df, columns)`` — pandas convenience

Future tables that show ``sales_order`` / ``sales_order_item`` should use
these helpers (or the same SQL pattern) so ordering stays consistent.
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd


def sql_natural_id_order(*column_exprs: str) -> str:
    """
    Build an ORDER BY fragment for one or more VARCHAR business identifiers.

    Example::

        f"ORDER BY h.sales_order_date, {sql_natural_id_order('h.sales_order', 'i.sales_order_item')}"
    """
    if not column_exprs:
        raise ValueError("sql_natural_id_order requires at least one column expression")
    parts = [f"TRY_CAST({col} AS BIGINT) NULLS LAST, {col}" for col in column_exprs]
    return ", ".join(parts)


def natural_id_sort_key(value: Any) -> tuple[int, int, str]:
    """
    Sort key matching DuckDB ``TRY_CAST(id AS BIGINT) NULLS LAST, id``.

    Returns ``(0, int_value, original_text)`` for numeric-only strings
    (including leading zeros) and ``(1, 0, original_text)`` for
    non-numeric / empty / null-like values so they sort after all numeric
    IDs. The string component matches the SQL tie-break (e.g. ``0002``
    before ``2``). The original string is never mutated.
    """
    if value is None:
        return (1, 0, "")
    if isinstance(value, float) and pd.isna(value):
        return (1, 0, "")
    text = str(value)
    if text == "" or text.lower() == "nan" or text.lower() == "<na>":
        return (1, 0, "")
    try:
        # int("0002") == 2 — numeric rank; display still uses original text
        return (0, int(text), text)
    except (TypeError, ValueError):
        return (1, 0, text)


def sort_dataframe_by_natural_ids(
    df: pd.DataFrame,
    columns: Sequence[str],
    *,
    ascending: bool | Sequence[bool] = True,
) -> pd.DataFrame:
    """
    Return a copy of ``df`` sorted by VARCHAR id columns with natural order.

    Identifier column values are left as strings (leading zeros preserved).
    """
    if df.empty:
        return df.copy()
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise KeyError(f"Columns not in dataframe: {missing}")
    return df.sort_values(
        by=list(columns),
        ascending=ascending,
        key=lambda series: series.map(natural_id_sort_key),
        kind="mergesort",
    ).reset_index(drop=True)
