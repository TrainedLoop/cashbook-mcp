-- v_flows: one definition of income and spending, shared by every tool.
-- It replaces v_spending.
DROP VIEW v_spending;

CREATE VIEW v_flows AS
WITH conn_accounts AS (
    SELECT a.alias, c.role,
           regexp_replace(a.owner_tax_id, '[^0-9]', '', 'g') AS tax_digits
    FROM v_accounts a
    JOIN connections c ON c.alias = a.alias
    WHERE a.owner_tax_id IS NOT NULL
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
    SELECT t.*,
           c.role AS role,
           CASE
               WHEN t.kind NOT IN ('internal_transfer', 'card_bill_payment') THEN NULL
               WHEN t.kind_reason LIKE 'paired_with:%' THEN
                   (SELECT p.alias FROM v_transactions p
                    WHERE p.id = substr(t.kind_reason, length('paired_with:') + 1))
               WHEN t.kind_reason LIKE 'counterpart_is_own_account:%' THEN
                   substr(t.kind_reason, length('counterpart_is_own_account:') + 1)
               WHEN t.kind_reason = 'counterpart_is_owner_tax_id' THEN
                   (SELECT tp.alias FROM tax_peer tp
                    WHERE tp.tax_digits = regexp_replace(t.counterpart_tax_id, '[^0-9]', '', 'g'))
           END AS peer_alias
    FROM v_transactions t
    LEFT JOIN connections c ON c.alias = t.alias
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
