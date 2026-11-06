import { useCallback, useEffect, useState } from "react";
import { api, ApiError, onUnauthorized } from "./api";
import ServerFooter from "./components/ServerFooter";
import AddExisting from "./screens/AddExisting";
import Connect from "./screens/Connect";
import Connections from "./screens/Connections";
import SignIn from "./screens/SignIn";

type State = { kind: "loading" } | { kind: "signed-out" } | { kind: "error" } | { kind: "in"; email: string };
type Route = { view: "list" | "connect" | "add"; disconnect: string | null };

/** The screen lives in the URL so a re-auth round trip (login?next=...) returns to it. */
function readRoute(): Route {
  const path = window.location.pathname.replace(/\/+$/, "");
  const disconnect = new URLSearchParams(window.location.search).get("disconnect");
  if (path === "/admin/connect") return { view: "connect", disconnect: null };
  if (path === "/admin/add") return { view: "add", disconnect: null };
  return { view: "list", disconnect: disconnect || null };
}

export function routeUrl(r: Route): string {
  if (r.view === "connect") return "/admin/connect";
  if (r.view === "add") return "/admin/add";
  return r.disconnect ? `/admin?disconnect=${encodeURIComponent(r.disconnect)}` : "/admin";
}

export default function App() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [route, setRoute] = useState<Route>(readRoute);

  const go = useCallback((r: Route) => {
    window.history.pushState({}, "", routeUrl(r));
    setRoute(r);
  }, []);

  useEffect(() => {
    const onPop = () => setRoute(readRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => onUnauthorized(() => setState({ kind: "signed-out" })), []);

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

  const list: Route = { view: "list", disconnect: null };
  return (
    <main className="page">
      {state.kind === "loading" && <p role="status" className="muted">Loading...</p>}
      {state.kind === "error" && <p role="alert" className="error">Could not reach the server. Reload the page.</p>}
      {state.kind === "signed-out" && <SignIn />}
      {state.kind === "in" && (
        <>
          <header className="topbar">
            <span className="muted">{state.email}</span>
            <button type="button" onClick={signOut}>Sign out</button>
          </header>
          {route.view === "list" && (
            <Connections
              disconnecting={route.disconnect}
              onDisconnect={(alias) => go({ view: "list", disconnect: alias })}
              onConnect={() => go({ view: "connect", disconnect: null })}
              onAddExisting={() => go({ view: "add", disconnect: null })}
            />
          )}
          {route.view === "connect" && <Connect onDone={() => go(list)} onCancel={() => go(list)} />}
          {route.view === "add" && <AddExisting onDone={() => go(list)} onCancel={() => go(list)} />}
          <ServerFooter />
        </>
      )}
    </main>
  );
}
