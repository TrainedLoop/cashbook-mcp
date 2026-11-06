import { useEffect, useRef, useState } from "react";
import { api, errorMessage, type DisconnectImpact } from "../api";

interface Props {
  alias: string;
  onClose: () => void;
  onDone: () => void;
}

export default function DisconnectDialog({ alias, onClose, onDone }: Props) {
  const [impact, setImpact] = useState<DisconnectImpact | null>(null);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let live = true;
    api.disconnectImpact(alias).then(
      (i) => live && setImpact(i),
      (e) => live && setError(errorMessage(e)),
    );
    return () => {
      live = false;
    };
  }, [alias]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => input.current?.focus(), []);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await api.disconnect(alias);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="dc-title">
        <h2 id="dc-title">Disconnect {alias}</h2>
        {impact && (
          <>
            {impact.before_window > 0 ? (
              <p className="warning">
                {impact.before_window} transactions before {impact.window_start} are no longer provided by Pluggy and
                will be lost.
              </p>
            ) : (
              <p>Nothing stored for this connection falls outside Pluggy's current window.</p>
            )}
            <p className="muted">
              {impact.total_transactions} stored transactions will be removed from this server
              {impact.oldest_date ? `, the oldest from ${impact.oldest_date}` : ""}.
            </p>
          </>
        )}
        <label className="field">
          <span>Type {alias} to confirm</span>
          <input
            ref={input}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
          />
        </label>
        {error && <p role="alert" className="error">{error}</p>}
        <div className="actions">
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="button" className="danger" disabled={!impact || typed !== alias || busy} onClick={confirm}>
            Disconnect
          </button>
        </div>
      </div>
    </div>
  );
}
