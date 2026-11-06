import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import ServerFooter from "./components/ServerFooter";
import AddExisting from "./screens/AddExisting";
import Connect from "./screens/Connect";
import Connections from "./screens/Connections";
import SignIn from "./screens/SignIn";

type State = { kind: "loading" } | { kind: "signed-out" } | { kind: "error" } | { kind: "in"; email: string };
type View = "list" | "connect" | "add";

export default function App() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [view, setView] = useState<View>("list");

  useEffect(() => {
    api.session().then(
      (s) => setState({ kind: "in", email: s.email }),
      (e) => setState(e instanceof ApiError && e.status === 401 ? { kind: "signed-out" } : { kind: "error" }),
    );
  }, []);

  async function signOut() {
    try {
      await api.logout();
    } finally {
      setState({ kind: "signed-out" });
    }
  }

  return (
    <main className="page">
      {state.kind === "loading" && <p className="muted">Loading...</p>}
      {state.kind === "error" && <p role="alert" className="error">Could not reach the server. Reload the page.</p>}
      {state.kind === "signed-out" && <SignIn />}
      {state.kind === "in" && (
        <>
          <header className="topbar">
            <span className="muted">{state.email}</span>
            <button type="button" onClick={signOut}>Sign out</button>
          </header>
          {view === "list" && <Connections onConnect={() => setView("connect")} onAddExisting={() => setView("add")} />}
          {view === "connect" && <Connect onDone={() => setView("list")} onCancel={() => setView("list")} />}
          {view === "add" && <AddExisting onDone={() => setView("list")} onCancel={() => setView("list")} />}
          <ServerFooter />
        </>
      )}
    </main>
  );
}
