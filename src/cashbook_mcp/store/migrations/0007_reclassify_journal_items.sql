-- Per-transaction journal of a reclassification batch: the manual override each transaction had
-- before the batch (null = none, i.e. the automatic classification or the provider's category),
-- so undo_reclassification can put the previous manual label back instead of dropping to automatic.
-- Batches written before this migration have no rows here; undoing them falls back to removing
-- their overrides.
CREATE TABLE classification_change_items (
    change_id               VARCHAR NOT NULL,
    transaction_id          VARCHAR NOT NULL,
    kind_changed            BOOLEAN NOT NULL,
    old_kind                VARCHAR,
    old_kind_change_id      VARCHAR,
    category_changed        BOOLEAN NOT NULL,
    category_dropped        BOOLEAN NOT NULL,
    old_category            VARCHAR,
    old_category_change_id  VARCHAR,
    PRIMARY KEY (change_id, transaction_id)
);
