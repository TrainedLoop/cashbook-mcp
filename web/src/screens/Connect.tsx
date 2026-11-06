import { useState } from "react";
import { PluggyConnect } from "react-pluggy-connect";
import { ALIAS_HINT, api, errorMessage, normalizeAlias, type ConnectTokenResponse } from "../api";

interface Props {
  onDone: () => void;
  onCancel: () => void;
}

export default function Connect({ onDone, onCancel }: Props) {
  const [raw, setRaw] = useState("");
  const [alias, setAlias] = useState<string | null>(null);
  const [token, setToken] = useState<ConnectTokenResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function start() {
    const a = normalizeAlias(raw);
    if (!a) {
      setError(ALIAS_HINT);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      setToken(await api.connectToken(a));
      setAlias(a);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function onSuccess(data: { item: { id: string } }) {
    setToken(null);
    try {
      await api.addConnection(alias!, data.item.id);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <section className="card">
      <h1>Connect with MeuPluggy</h1>
      <p className="muted">Choose a short alias, then sign in to MeuPluggy in the window that opens.</p>
      <label className="field">
        <span>Alias</span>
        <input
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
        />
      </label>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={onCancel}>Back</button>
        <button type="button" className="primary" onClick={start} disabled={busy}>
          Connect with MeuPluggy
        </button>
      </div>
      {token && (
        <PluggyConnect
          connectToken={token.accessToken}
          includeSandbox={false}
          connectorIds={token.connectorIds}
          onSuccess={onSuccess}
          onError={() => {
            setToken(null);
            setError("The connection did not complete. Try again.");
          }}
          onClose={() => setToken(null)}
        />
      )}
    </section>
  );
}
