-- v0.2 schema. This migration deliberately DISCARDS ALL v0.1 DATA: 0.2.0 is a
-- fresh start (reconnect the banks after upgrading). The migrator's .bak-1
-- backup is the only copy of the old store. schema_version is owned by the
-- migrator and is never touched here.
-- Timestamps are naive UTC. Money is DECIMAL(18,2), signed (negative = money out).

DROP VIEW v_spending;
DROP VIEW v_loans;
DROP VIEW v_investment_transactions;
DROP VIEW v_investments;
DROP VIEW v_bills;
DROP VIEW v_transactions;
DROP VIEW v_accounts;
DROP TABLE sync_runs;
DROP SEQUENCE sync_runs_id_seq;
DROP TABLE loans;
DROP TABLE investment_transactions;
DROP TABLE investments;
DROP TABLE bills;
DROP TABLE transactions;
DROP TABLE accounts;
DROP TABLE connections;

CREATE TABLE connections (
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

CREATE TABLE accounts (
    id                VARCHAR PRIMARY KEY,
    alias             VARCHAR NOT NULL,
    item_id           VARCHAR NOT NULL,
    type              VARCHAR NOT NULL CHECK (type IN ('BANK', 'CREDIT')),
    subtype           VARCHAR NOT NULL,
    name              VARCHAR NOT NULL,
    number            VARCHAR,
    owner_tax_id      VARCHAR,
    balance           DECIMAL(18,2) NOT NULL,
    currency          VARCHAR NOT NULL,
    credit_limit      DECIMAL(18,2),
    available_credit  DECIMAL(18,2),
    first_seen_at     TIMESTAMP NOT NULL,
    last_seen_at      TIMESTAMP NOT NULL,
    removed_at        TIMESTAMP NULL
);

CREATE TABLE transactions (
    id                          VARCHAR PRIMARY KEY,
    alias                       VARCHAR NOT NULL,
    account_id                  VARCHAR NOT NULL,
    date                        DATE NOT NULL,
    amount                      DECIMAL(18,2) NOT NULL,
    currency                    VARCHAR NOT NULL,
    original_amount             DECIMAL(18,2),
    original_currency           VARCHAR,
    description                 VARCHAR NOT NULL,
    counterpart_name            VARCHAR,
    counterpart_tax_id          VARCHAR,
    counterpart_account_number  VARCHAR,
    merchant_name               VARCHAR,
    pluggy_category             VARCHAR,
    bill_id                     VARCHAR,
    installment_number          INTEGER,
    installment_total           INTEGER,
    purchase_date               DATE,
    kind                        VARCHAR NOT NULL DEFAULT 'other',
    kind_reason                 VARCHAR NOT NULL DEFAULT '',
    text_suspicious             BOOLEAN NOT NULL DEFAULT false,
    suspicion_reasons           VARCHAR[] NOT NULL DEFAULT [],
    first_seen_at               TIMESTAMP NOT NULL,
    last_seen_at                TIMESTAMP NOT NULL,
    removed_at                  TIMESTAMP NULL
);

CREATE TABLE bills (
    id               VARCHAR PRIMARY KEY,
    alias            VARCHAR NOT NULL,
    account_id       VARCHAR NOT NULL,
    due_date         DATE NOT NULL,
    closing_date     DATE,
    total_amount     DECIMAL(18,2) NOT NULL,
    minimum_payment  DECIMAL(18,2),
    currency         VARCHAR NOT NULL,
    first_seen_at    TIMESTAMP NOT NULL,
    last_seen_at     TIMESTAMP NOT NULL,
    removed_at       TIMESTAMP NULL
);

CREATE TABLE investments (
    id             VARCHAR PRIMARY KEY,
    alias          VARCHAR NOT NULL,
    item_id        VARCHAR NOT NULL,
    type           VARCHAR NOT NULL,
    name           VARCHAR NOT NULL,
    balance        DECIMAL(18,2) NOT NULL,
    currency       VARCHAR NOT NULL,
    due_date       DATE,
    issuer         VARCHAR,
    first_seen_at  TIMESTAMP NOT NULL,
    last_seen_at   TIMESTAMP NOT NULL,
    removed_at     TIMESTAMP NULL
);

CREATE TABLE investment_transactions (
    id             VARCHAR PRIMARY KEY,
    alias          VARCHAR NOT NULL,
    investment_id  VARCHAR NOT NULL,
    date           DATE NOT NULL,
    trade_date     DATE,
    type           VARCHAR NOT NULL,
    movement_type  VARCHAR,
    -- quantity (unit count, e.g. fund shares) and value (unit price) are not
    -- totals: keep extra scale so fund quotas are not rounded.
    quantity       DECIMAL(24,8),
    value          DECIMAL(24,8),
    amount         DECIMAL(18,2) NOT NULL,
    first_seen_at  TIMESTAMP NOT NULL,
    last_seen_at   TIMESTAMP NOT NULL,
    removed_at     TIMESTAMP NULL
);

CREATE TABLE loans (
    id                      VARCHAR PRIMARY KEY,
    alias                   VARCHAR NOT NULL,
    item_id                 VARCHAR NOT NULL,
    product_name            VARCHAR,
    contract_amount         DECIMAL(18,2),
    outstanding_balance     DECIMAL(18,2),
    currency                VARCHAR NOT NULL,
    installments_total      INTEGER,
    installments_remaining  INTEGER,
    next_due_date           DATE,
    first_seen_at           TIMESTAMP NOT NULL,
    last_seen_at            TIMESTAMP NOT NULL,
    removed_at              TIMESTAMP NULL
);

CREATE SEQUENCE sync_runs_id_seq START 1;

CREATE TABLE sync_runs (
    id             BIGINT PRIMARY KEY DEFAULT nextval('sync_runs_id_seq'),
    alias          VARCHAR NOT NULL,
    started_at     TIMESTAMP NOT NULL,
    finished_at    TIMESTAMP,
    outcome        VARCHAR CHECK (outcome IN ('ok', 'error', 'discarded')),
    rows_upserted  INTEGER NOT NULL DEFAULT 0,
    rows_removed   INTEGER NOT NULL DEFAULT 0,
    error          VARCHAR
);

CREATE VIEW v_accounts AS SELECT * FROM accounts WHERE removed_at IS NULL;
CREATE VIEW v_transactions AS SELECT * FROM transactions WHERE removed_at IS NULL;
CREATE VIEW v_bills AS SELECT * FROM bills WHERE removed_at IS NULL;
CREATE VIEW v_investments AS SELECT * FROM investments WHERE removed_at IS NULL;
CREATE VIEW v_investment_transactions AS
    SELECT * FROM investment_transactions WHERE removed_at IS NULL;
CREATE VIEW v_loans AS SELECT * FROM loans WHERE removed_at IS NULL;

CREATE VIEW v_spending AS
    SELECT * FROM v_transactions
    WHERE amount < 0
      AND kind NOT IN ('internal_transfer', 'card_bill_payment', 'investment');

-- A row exists only when the owner set a purpose; no row means 'regular'.
-- alias lets disconnect delete a connection's settings.
CREATE TABLE account_settings (
    account_id VARCHAR PRIMARY KEY,
    alias      VARCHAR NOT NULL,
    purpose    VARCHAR NOT NULL CHECK (purpose IN ('regular', 'investment'))
);

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
