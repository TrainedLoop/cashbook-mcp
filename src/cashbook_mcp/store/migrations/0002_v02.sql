-- v0.2: connection roles, account purposes, card installments, daily snapshots,
-- and the reclassification journal. Timestamps are naive UTC.

-- DuckDB cannot add a column with a CHECK constraint, so connections is rebuilt
-- (nothing references it).
CREATE TABLE connections_v2 (
    alias              VARCHAR PRIMARY KEY,
    item_id            VARCHAR NOT NULL UNIQUE,
    connector_name     VARCHAR NOT NULL,
    created_at         TIMESTAMP NOT NULL,
    status             VARCHAR,
    execution_status   VARCHAR,
    error_code         VARCHAR,
    error_message      VARCHAR,
    last_updated_at    TIMESTAMP,
    consent_expires_at TIMESTAMP,
    last_synced_at     TIMESTAMP,
    last_sync_error    VARCHAR,
    role               VARCHAR NOT NULL DEFAULT 'personal' CHECK (role IN ('personal', 'business'))
);
INSERT INTO connections_v2 (
    alias, item_id, connector_name, created_at, status, execution_status, error_code,
    error_message, last_updated_at, consent_expires_at, last_synced_at, last_sync_error
)
SELECT alias, item_id, connector_name, created_at, status, execution_status, error_code,
       error_message, last_updated_at, consent_expires_at, last_synced_at, last_sync_error
FROM connections;
DROP TABLE connections;
ALTER TABLE connections_v2 RENAME TO connections;

-- A row exists only when the owner set a purpose; no row means 'regular'.
-- alias lets disconnect delete a connection's settings.
CREATE TABLE account_settings (
    account_id VARCHAR PRIMARY KEY,
    alias      VARCHAR NOT NULL,
    purpose    VARCHAR NOT NULL CHECK (purpose IN ('regular', 'investment'))
);

ALTER TABLE transactions ADD COLUMN installment_number INTEGER;
ALTER TABLE transactions ADD COLUMN installment_total INTEGER;
ALTER TABLE transactions ADD COLUMN purchase_date DATE;

CREATE TABLE balance_snapshots (
    day        DATE,
    account_id VARCHAR,
    alias      VARCHAR NOT NULL,
    balance    DECIMAL(18,2) NOT NULL,
    currency   VARCHAR NOT NULL,
    PRIMARY KEY (day, account_id)
);

CREATE TABLE investment_snapshots (
    day           DATE,
    investment_id VARCHAR,
    alias         VARCHAR NOT NULL,
    balance       DECIMAL(18,2) NOT NULL,
    currency      VARCHAR NOT NULL,
    PRIMARY KEY (day, investment_id)
);

CREATE TABLE classification_changes (
    change_id  VARCHAR PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    kind       VARCHAR NOT NULL,
    note       VARCHAR,
    count      INTEGER NOT NULL,
    undone_at  TIMESTAMP
);

CREATE TABLE classification_overrides (
    transaction_id VARCHAR PRIMARY KEY,
    alias          VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,
    change_id      VARCHAR NOT NULL
);
