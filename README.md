# SAP Sales Analytics

Small, production-minded Python data engineering learning project. It pulls Sales Order data from the **SAP Business Accelerator Hub** sandbox (OData V2), saves raw JSON, and loads normalized header/item tables into DuckDB.

> **Scope:** skeleton + stubs. No live API calls are required to explore the layout. Wire a real sandbox key only when you are ready to ingest.

## Stack

- Python 3.11+
- `requests` — HTTP client (GET only)
- `python-dotenv` — load secrets from `.env`
- DuckDB — local analytical store
- `pytest` — unit tests

No Airflow, Spark, Docker, Kubernetes, or cloud infra.

## SAP sandbox patterns

API Hub sandbox host (typical):

```text
https://sandbox.api.sap.com/s4hanacloud
```

Sales Order OData V2 service path:

```text
/sap/opu/odata/sap/API_SALES_ORDER_SRV
```

Authenticated with the `APIKey` header (value from Business Accelerator Hub → your sandbox application).

Documented GET endpoints used by this project:

| Purpose | Method | Relative path |
| --- | --- | --- |
| Sales order headers | `GET` | `/A_SalesOrder` |
| Items for one order | `GET` | `/A_SalesOrder('{SalesOrder}')/to_Item` |

Example absolute URLs (do not call until configured):

```text
GET https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder?$format=json
GET https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder('5000001')/to_Item?$format=json
```

## Design rules reflected in the code

1. **GET only** — client exposes read methods; no POST/PATCH/DELETE.
2. **APIKey from `.env`** — never hard-coded; never logged.
3. **Timeouts + retries** — transient failures (timeouts, connection errors, 5xx) are retried with backoff.
4. **Raw JSON landing** — responses written under `data/raw/`.
5. **Normalize to DuckDB** — headers → `sales_order_header`, items → `sales_order_item`.
6. **Identifiers as strings** — SAP keys (e.g. `SalesOrder`) stay `VARCHAR`.
7. **OData V2 dates** — `/Date(milliseconds)/` → timestamps.
8. **Money as Decimal** — amounts keep decimal precision (not float).
9. **Validation** — primary keys present; every item references an existing header.
10. **Secrets hygiene** — `.gitignore` excludes `.env`; `.env.example` has placeholders only.

## Project layout

```text
sap-sales-analytics/
  src/
    __init__.py
    sap_client.py    # Sandbox GET client (APIKey, timeout, retries)
    ingest.py        # Fetch + save raw JSON under data/raw/
    transform.py     # Normalize JSON → DuckDB tables
  tests/
    test_transform.py
  data/raw/          # Raw API JSON landing zone
  sql/               # Optional SQL notes / DDL sketches
  .env.example
  .gitignore
  requirements.txt
  README.md
```

## Setup

```bash
cd sap-sales-analytics
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set SAP_API_KEY from the Business Accelerator Hub sandbox app
```

## Run

Ingest (writes raw JSON; requires a valid sandbox key when you enable live calls):

```bash
python -m src.ingest
```

Transform (reads raw JSON → DuckDB):

```bash
python -m src.transform
```

Tests (no network):

```bash
pytest -q
```

## Data notes

- Raw files land as `data/raw/a_sales_order_*.json` and `data/raw/a_sales_order_items_*.json`.
- DuckDB default path: `data/sales_analytics.duckdb` (gitignored).
- Monetary fields (e.g. `TotalNetAmount`, `NetAmount`) are stored with `DECIMAL` precision.
- Date/time fields (e.g. `CreationDate`, `SalesOrderDate`) are converted from OData V2 `/Date(...)/` literals.

## Security

- Put credentials only in `.env` (never in source or commits).
- The client logs status codes and URLs without the API key value.
- Rotate the sandbox key if it is ever exposed.
