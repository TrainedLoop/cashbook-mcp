import { useRef, useState } from "react";
import { PluggyConnect } from "react-pluggy-connect";
import { api, errorMessage, existingItemIds, type ConnectTokenResponse, type FoundItem } from "../api";
import ChooseItems from "../components/ChooseItems";

interface Props {
  onDone: () => void;
  onCancel: () => void;
}

type Phase =
  | { kind: "idle" }
  | { kind: "widget"; token: ConnectTokenResponse }
  | { kind: "finding" }
  | { kind: "choose"; items: FoundItem[] };

const NOT_COMPLETED = "The connection did not complete. Try again.";

/**
 * MeuPluggy shares every bank the owner linked there, one item each, under a single login. The
 * widget creates them the first time; afterwards Pluggy refuses a new item and names the existing
 * ones. Either way the owner then gets the whole list and picks an alias per bank.
 */
export default function Connect({ onDone, onCancel }: Props) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [error, setError] = useState<string | null>(null);
  // Set once the widget reported success or an error, so its closing afterwards changes nothing.
  const settled = useRef(false);

  async function start() {
    setError(null);
    setPhase({ kind: "finding" });
    try {
      const token = await api.connectToken();
      settled.current = false;
      setPhase({ kind: "widget", token });
    } catch (e) {
      setError(errorMessage(e));
      setPhase({ kind: "idle" });
    }
  }

  async function find(token: ConnectTokenResponse, created: string | null) {
    settled.current = true;
    setPhase({ kind: "finding" });
    const ids = await existingItemIds(token.accessToken, token.connectorIds[0]);
    if (created && !ids.includes(created)) ids.unshift(created);
    if (ids.length === 0) {
      setError(NOT_COMPLETED);
      setPhase({ kind: "idle" });
      return;
    }
    try {
      setPhase({ kind: "choose", items: (await api.discover(ids)).items });
    } catch (e) {
      setError(errorMessage(e));
      setPhase({ kind: "idle" });
    }
  }

  if (phase.kind === "choose") return <ChooseItems items={phase.items} onDone={onDone} onCancel={onCancel} />;

  return (
    <section className="card">
      <h1>Connect with MeuPluggy</h1>
      <p className="muted">Sign in to MeuPluggy in the window that opens. You name each bank afterwards.</p>
      {phase.kind === "finding" && <p role="status" className="muted">Looking for your banks...</p>}
      {error && <p role="alert" className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={onCancel}>Back</button>
        <button type="button" className="primary" onClick={start} disabled={phase.kind !== "idle"}>
          Connect with MeuPluggy
        </button>
      </div>
      {phase.kind === "widget" && (
        <PluggyConnect
          connectToken={phase.token.accessToken}
          includeSandbox={false}
          connectorIds={phase.token.connectorIds}
          onSuccess={(data) => void find(phase.token, data.item.id)}
          onError={() => void find(phase.token, null)}
          onClose={() => {
            if (!settled.current) setPhase({ kind: "idle" });
          }}
        />
      )}
    </section>
  );
}
