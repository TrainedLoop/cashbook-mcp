import { useState } from "react";
import { ALIAS_HINT, api, errorMessage, normalizeAlias, type ItemPreview, type Role } from "../api";

interface Props {
  onDone: () => void;
  onCancel: () => void;
}

export default function AddExisting({ onDone, onCancel }: Props) {
  const [rawAlias, setRawAlias] = useState("");
  const [itemId, setItemId] = useState("");
  const [preview, setPreview] = useState<ItemPreview | null>(null);
  const [role, setRole] = useState<Role>("personal");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function doPreview() {
    if (!normalizeAlias(rawAlias)) {
      setError(ALIAS_HINT);
      return;
    }
    return run(async () => {
      const p = await api.preview(itemId.trim());
      setPreview(p);
      setRole(p.suggested_role ?? "personal");
    });
  }

  const doAdd = () =>
    run(async () => {
      const alias = normalizeAlias(rawAlias);
      if (!alias || !preview) return;
      await api.addConnection(alias, preview.item_id, role);
      onDone();
    });

  return (
    <section className="card">
      <h1>Add existing item</h1>
      <p className="muted">Use an item that already exists in your Pluggy application.</p>
      <label className="field">
        <span>Alias</span>
        <input
          value={rawAlias}
          onChange={(e) => {
            setRawAlias(e.target.value);
            setPreview(null);
          }}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
        />
      </label>
      <label className="field">
        <span>Item id</span>
        <input
          value={itemId}
          onChange={(e) => {
            setItemId(e.target.value);
            setPreview(null);
          }}
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      {error && <p role="alert" className="error">{error}</p>}
      {preview && (
        <div className="preview">
          <p><strong>{preview.bank}</strong></p>
          <ul>
            {preview.accounts.map((a, i) => (
              <li key={i}>{a.name}{a.number ? ` (${a.number})` : ""}</li>
            ))}
          </ul>
          <label className="field">
            <span>Role</span>
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="personal">Personal</option>
              <option value="business">Business</option>
            </select>
          </label>
        </div>
      )}
      <div className="actions">
        <button type="button" onClick={onCancel}>Back</button>
        {preview ? (
          <button type="button" className="primary" onClick={doAdd} disabled={busy}>Add connection</button>
        ) : (
          <button type="button" className="primary" onClick={doPreview} disabled={busy || !itemId.trim()}>Preview</button>
        )}
      </div>
    </section>
  );
}
