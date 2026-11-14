-- DuckDB DDL sketch mirroring src/transform.py ensure_tables().
-- SAP identifiers are VARCHAR; monetary columns use DECIMAL precision.

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
);

-- In-place migration for DBs created before overall_delivery_status existed:
-- ALTER TABLE sales_order_header ADD COLUMN IF NOT EXISTS overall_delivery_status VARCHAR;

CREATE TABLE IF NOT EXISTS sales_order_item (
    sales_order              VARCHAR,
    sales_order_item         VARCHAR,
    material                 VARCHAR,
    requested_quantity       DECIMAL(18, 3),
    requested_quantity_unit  VARCHAR,
    net_amount               DECIMAL(18, 2),
    transaction_currency     VARCHAR,
    PRIMARY KEY (sales_order, sales_order_item)
);
