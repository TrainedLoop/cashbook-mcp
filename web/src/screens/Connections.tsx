import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage, type Connection } from "../api";
import DisconnectDialog from "../components/DisconnectDialog";
import { formatTime } from "../components/ServerFooter";
import StatusBadge from "../components/StatusBadge";

export const POLL_MS = 3000;
export const POLL_MAX_MS = 2 * 60 * 1000;

interface Props {
  disconnecting: string | null;
  onDisconnect: (alias: string | null) => void;
  onConnect: () => void;
  onAddExisting: () => void;
}

export default function Connections({ disconnecting, onDisconnect, onConnect, onAddExisting }: Props) {
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Polling runs while something syncs, until this deadline; opening the screen and every reload extends it.
  const deadline = useRef(Date.now() + POLL_MAX_MS);

  const load = useCallback(async () => {
    try {
      setRows((await api.connections()).connections);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const anySyncing = rows?.some((r) => r.syncing) ?? false;
  useEffect(() => {
    if (!anySyncing || Date.now() >= deadline.current) return;
    const t = setTimeout(() => void load(), POLL_MS);
    return () => clearTimeout(t);
  }, [anySyncing, rows, load]);

  async function reload(alias?: string) {
    setError(null);
    try {
      await api.reload(alias);
      deadline.current = Date.now() + POLL_MAX_MS;
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <>
      <section className="card">
        <div className="head">
          <h1>Connections</h1>
          <button type="button" onClick={() => reload()} disabled={!rows?.length}>Reload now</button>
        </div>
        {error && <p role="alert" className="error">{error}</p>}
        {rows && rows.length === 0 && <p className="muted">No connections yet.</p>}
        <ul className="list">
          {rows?.map((c) => (
            <li key={c.alias} className="conn">
              <div className="head">
                <h2>{c.alias}</h2>
                <StatusBadge badge={c.badge} />
              </div>
              <p className="muted">{c.bank ?? "Unknown bank"}</p>
              {c.hint && c.badge !== "ok" && c.badge !== "syncing" && <p className="hint">{c.hint}</p>}
              <dl className="facts">
                <div><dt>Last sync</dt><dd>{formatTime(c.last_synced_at)}</dd></div>
                <div>
                  <dt>History</dt>
                  <dd>
                    {c.history.transactions} transactions
                    {c.history.date_from && c.history.date_to ? `, ${c.history.date_from} to ${c.history.date_to}` : ""}
                  </dd>
                </div>
              </dl>
              {c.accounts.length > 0 && (
                <ul className="accounts">
                  {c.accounts.map((a) => (
                    <li key={a.id}>{a.name}</li>
                  ))}
                </ul>
              )}
              <div className="actions">
                <button type="button" onClick={() => reload(c.alias)} disabled={c.syncing}>Reload now</button>
                <button type="button" className="danger-outline" onClick={() => onDisconnect(c.alias)}>Disconnect</button>
              </div>
            </li>
          ))}
        </ul>
        <div className="actions">
          <button type="button" className="primary" onClick={onConnect}>Connect with MeuPluggy</button>
          <button type="button" onClick={onAddExisting}>Add existing item</button>
        </div>
      </section>
      {disconnecting && (
        <DisconnectDialog
          alias={disconnecting}
          onClose={() => onDisconnect(null)}
          onDone={() => {
            onDisconnect(null);
            void load();
          }}
        />
      )}
    </>
  );
}
