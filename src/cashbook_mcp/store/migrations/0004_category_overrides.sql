-- Category overrides: the agent can relabel a transaction's category (never its kind).
-- A batch in classification_changes sets a kind, a category or both.
CREATE TABLE category_overrides (
    transaction_id VARCHAR PRIMARY KEY,
    alias          VARCHAR NOT NULL,
    category       VARCHAR NOT NULL,
    change_id      VARCHAR NOT NULL
);

ALTER TABLE classification_changes ALTER COLUMN kind DROP NOT NULL;
ALTER TABLE classification_changes ADD COLUMN category VARCHAR;

-- v_flows: pluggy_category is now the effective category; the provider's value stays in
-- pluggy_category_original. Classification still reads the original from transactions.
DROP VIEW v_flows;

CREATE VIEW v_flows AS
WITH conn_accounts AS (
    SELECT a.alias, c.role,
           regexp_replace(a.owner_tax_id, '[^0-9]', '', 'g') AS tax_digits
    FROM v_accounts a
    JOIN connections c ON c.alias = a.alias
    WHERE a.owner_tax_id IS NOT NULL AND a.type = 'BANK'
),
-- One connection per tax id; when it exists on both roles, a CNPJ (14 digits)
-- prefers business and a CPF (11 digits) prefers personal.
tax_peer AS (
    SELECT tax_digits, alias, role
    FROM (
        SELECT tax_digits, alias, role,
               row_number() OVER (
                   PARTITION BY tax_digits
                   ORDER BY (role = CASE WHEN length(tax_digits) = 14
                                         THEN 'business' ELSE 'personal' END) DESC,
                            alias
               ) AS rn
        FROM conn_accounts
        WHERE tax_digits <> ''
    )
    WHERE rn = 1
),
base AS (
    SELECT t.* REPLACE (coalesce(co.category, t.pluggy_category) AS pluggy_category),
           t.pluggy_category AS pluggy_category_original,
           (co.category IS NOT NULL) AS category_overridden,
           c.role AS role,
           CASE
               WHEN t.kind NOT IN ('internal_transfer', 'card_bill_payment') THEN NULL
               WHEN t.kind_reason LIKE 'paired_with:%' THEN
                   (SELECT p.alias FROM v_transactions p
                    WHERE p.id = substr(t.kind_reason, length('paired_with:') + 1))
               WHEN t.kind_reason LIKE 'counterpart_is_own_account:%' THEN
                   substr(t.kind_reason, length('counterpart_is_own_account:') + 1)
               -- Bank side: the card account the bill belongs to. Card side: the
               -- bank account that paid it (lowest id when several matched).
               WHEN t.kind_reason LIKE 'matched_card_bill:%' THEN
                   CASE WHEN (SELECT a.type FROM v_accounts a WHERE a.id = t.account_id) = 'BANK'
                   THEN (SELECT ca.alias FROM v_bills b
                         JOIN v_accounts ca ON ca.id = b.account_id
                         WHERE b.id = substr(t.kind_reason, length('matched_card_bill:') + 1))
                   ELSE (SELECT bt.alias FROM v_transactions bt
                         JOIN v_accounts ba ON ba.id = bt.account_id AND ba.type = 'BANK'
                         WHERE bt.kind_reason = t.kind_reason
                         ORDER BY bt.id LIMIT 1)
                   END
               WHEN t.kind_reason = 'counterpart_is_owner_tax_id' THEN
                   (SELECT tp.alias FROM tax_peer tp
                    WHERE tp.tax_digits = regexp_replace(t.counterpart_tax_id, '[^0-9]', '', 'g'))
           END AS peer_alias
    FROM v_transactions t
    LEFT JOIN connections c ON c.alias = t.alias
    LEFT JOIN category_overrides co ON co.transaction_id = t.id
),
peered AS (
    SELECT b.*,
           (SELECT pc.role FROM connections pc WHERE pc.alias = b.peer_alias) AS peer_role
    FROM base b
),
flowed AS (
    SELECT *,
        CASE
            WHEN currency <> 'BRL' THEN 'foreign'
            WHEN kind IN ('internal_transfer', 'card_bill_payment')
                 AND peer_role IS NOT NULL AND peer_role <> role THEN
                CASE
                    WHEN role = 'personal' AND amount < 0 THEN 'to_business'
                    WHEN role = 'personal' THEN 'from_business'
                    WHEN amount < 0 THEN 'to_personal'
                    ELSE 'from_personal'
                END
            WHEN kind = 'investment' THEN 'investment'
            WHEN kind = 'card_bill_payment' THEN 'card_payment'
            WHEN kind = 'internal_transfer' THEN 'internal'
            -- Every other outflow is spending, including a reversed income or refund.
            WHEN amount < 0 THEN 'spending'
            WHEN kind = 'income' THEN 'income'
            -- Borrowed money is neither income nor spending.
            WHEN kind = 'loan' THEN 'internal'
            -- Remaining inflows (refund, purchase, fee, other) reduce spending.
            ELSE 'refund'
        END AS flow
    FROM peered
)
SELECT * EXCLUDE (flow),
       flow,
       CASE WHEN flow = 'spending' OR flow = 'refund' THEN -amount ELSE 0::DECIMAL(18,2) END
           AS spend_amount,
       CASE WHEN flow = 'income' THEN amount ELSE 0::DECIMAL(18,2) END AS income_amount
FROM flowed;
