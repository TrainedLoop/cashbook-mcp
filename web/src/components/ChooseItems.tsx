import { useState } from "react";
import { ALIAS_HINT, ApiError, api, errorMessage, normalizeAlias, suggestAlias, type FoundItem } from "../api";

interface Props {
  items: FoundItem[];
  onDone: () => void;
  onCancel: () => void;
}

interface Row {
  include: boolean;
  alias: string;
  added: string | null;
  error: string | null;
}

const addable = (item: FoundItem) => item.alias === null && item.error === null;

function initialRows(items: FoundItem[]): Record<string, Row> {
  const taken = new Set(items.flatMap((i) => (i.alias ? [i.alias] : [])));
  const rows: Record<string, Row> = {};
  for (const item of items.filter(addable)) {
    const alias = suggestAlias(item.bank, taken);
    taken.add(alias);
    rows[item.item_id] = { include: true, alias, added: null, error: null };
  }
  return rows;
}

/** One block per MeuPluggy item: connected ones show their alias, new ones get an alias field. */
export default function ChooseItems({ items, onDone, onCancel }: Props) {
  const [rows, setRows] = useState(() => initialRows(items));
  const [busy, setBusy] = useState(false);

  const update = (id: string, patch: Partial<Row>) => setRows((r) => ({ ...r, [id]: { ...r[id], ...patch } }));
  const pending = Object.entries(rows).filter(([, r]) => r.include && !r.added);

  async function addSelected() {
    setBusy(true);
    let failed = false;
    const used = new Set<string>();
    for (const [id, row] of pending) {
      const alias = normalizeAlias(row.alias);
      if (!alias || used.has(alias)) {
        update(id, { error: alias ? "Use a different alias for each bank." : ALIAS_HINT });
        failed = true;
        continue;
      }
      used.add(alias);
      try {
        await api.addConnection(alias, id);
        update(id, { added: alias, error: null });
      } catch (e) {
        update(id, { error: errorMessage(e) });
        failed = true;
        if (e instanceof ApiError && e.status === 401) break;
      }
    }
    setBusy(false);
    if (!failed) onDone();
  }

  const anyAddable = items.some(addable);
  return (
    <section className="card">
      <h1>Choose your banks</h1>
      <p className="muted">These are the banks MeuPluggy shares with this server. Give each one you want an alias.</p>
      <ul className="list">
        {items.map((item) => {
          const row = rows[item.item_id];
          const bank = item.bank || item.item_id;
          return (
            <li key={item.item_id} className="conn">
              <h2>{bank}</h2>
              {item.accounts.length > 0 && (
                <ul className="accounts">
                  {item.accounts.map((a, i) => (
                    <li key={i}>{a.name}{a.number ? ` (${a.number})` : ""}</li>
                  ))}
                </ul>
              )}
              {item.alias && <p className="muted">Connected as <strong>{item.alias}</strong>.</p>}
              {item.error && <p className="error">{errorMessage(new ApiError(422, item.error))}</p>}
              {row?.added && <p>Added as <strong>{row.added}</strong>.</p>}
              {row && !row.added && (
                <>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={row.include}
                      onChange={(e) => update(item.item_id, { include: e.target.checked })}
                    />
                    Connect {bank}
                  </label>
                  {row.include && (
                    <label className="field">
                      <span>Alias</span>
                      <input
                        aria-label={`Alias for ${bank}`}
                        value={row.alias}
                        onChange={(e) => update(item.item_id, { alias: e.target.value, error: null })}
                        autoComplete="off"
                        autoCapitalize="none"
                        spellCheck={false}
                      />
                    </label>
                  )}
                  {row.error && <p role="alert" className="error">{row.error}</p>}
                </>
              )}
            </li>
          );
        })}
      </ul>
      {!anyAddable && <p className="muted">Every bank shared by MeuPluggy is already connected.</p>}
      <div className="actions">
        <button type="button" onClick={anyAddable ? onCancel : onDone}>Back</button>
        {anyAddable && (
          <button type="button" className="primary" onClick={addSelected} disabled={busy || pending.length === 0}>
            {pending.length === 1 ? "Add 1 bank" : `Add ${pending.length} banks`}
          </button>
        )}
      </div>
    </section>
  );
}
