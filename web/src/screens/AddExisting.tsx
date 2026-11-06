import { useState } from "react";
import { ALIAS_HINT, api, errorMessage, normalizeAlias, type ItemPreview } from "../api";

interface Props {
  onDone: () => void;
  onCancel: () => void;
}

export default function AddExisting({ onDone, onCancel }: Props) {
  const [rawAlias, setRawAlias] = useState("");
  const [itemId, setItemId] = useState("");
  const [preview, setPreview] = useState<ItemPreview | null>(null);
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
    return run(async () => setPreview(await api.preview(itemId.trim())));
  }

  const doAdd = () =>
    run(async () => {
      const alias = normalizeAlias(rawAlias);
      if (!alias || !preview) return;
      await api.addConnection(alias, preview.item_id);
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
          <p><strong>{preview.connector_name}</strong></p>
          <ul>
            {preview.accounts.map((a, i) => (
              <li key={i}>{a.name}{a.number ? ` (${a.number})` : ""}</li>
            ))}
          </ul>
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
