import { useRef, useState } from "react";
import { PluggyConnect } from "react-pluggy-connect";
import { api, errorMessage, type ConnectTokenResponse, type FoundItem } from "../api";
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
 * MeuPluggy authorizes one bank per widget run: each bank linked there becomes its own item, all
 * under the same login. The owner runs the widget once per bank and names each new item here.
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

  function failed() {
    settled.current = true;
    setError(NOT_COMPLETED);
    setPhase({ kind: "idle" });
  }

  async function found(created: string) {
    settled.current = true;
    setError(null);
    setPhase({ kind: "finding" });
    try {
      setPhase({ kind: "choose", items: (await api.discover([created])).items });
    } catch (e) {
      setError(errorMessage(e));
      setPhase({ kind: "idle" });
    }
  }

  if (phase.kind === "choose") return <ChooseItems items={phase.items} onDone={onDone} onCancel={onCancel} />;

  return (
    <section className="card">
      <h1>Connect with MeuPluggy</h1>
      <p className="muted">Sign in to MeuPluggy in the window that opens. You name the bank afterwards.</p>
      {phase.kind === "finding" && <p role="status" className="muted">Looking for your bank...</p>}
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
          onSuccess={(data) => void found(data.item.id)}
          onError={failed}
          onClose={() => {
            if (!settled.current) setPhase({ kind: "idle" });
          }}
        />
      )}
    </section>
  );
}
