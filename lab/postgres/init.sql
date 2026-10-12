CREATE TABLE IF NOT EXISTS orders (
    id          UUID PRIMARY KEY,
    customer_id TEXT        NOT NULL,
    sku         TEXT        NOT NULL,
    qty         INT         NOT NULL,
    amount_usd  NUMERIC(10,2) NOT NULL,
    tier        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory (
    sku   TEXT PRIMARY KEY,
    stock INT  NOT NULL
);

INSERT INTO inventory (sku, stock) VALUES
    ('SKU-TELESCOPE', 1000000),
    ('SKU-TRIPOD',    1000000),
    ('SKU-EYEPIECE',  1000000),
    ('SKU-FILTER',    1000000),
    ('SKU-STARMAP',   1000000)
ON CONFLICT (sku) DO NOTHING;
