import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { resetSession, type Connection } from "../api";

function reply(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const connection: Connection = {
  alias: "nubank",
  role: "personal",
  bank: "Meu Banco",
  status: "UPDATED",
  execution_status: "SUCCESS",
  error_code: null,
  last_updated_at: null,
  last_synced_at: null,
  consent_expires_at: null,
  accounts: [],
  history: { transactions: 1500, date_from: "2025-01-01", date_to: "2026-11-01" },
  hint: null,
  sync_failed: false,
  syncing: false,
  badge: "ok",
};

let disconnectReply: () => Response;
const assign = vi.fn();

beforeEach(() => {
  resetSession();
  assign.mockReset();
  vi.stubGlobal("location", { pathname: "/admin", search: "", assign });
  disconnectReply = () => reply(200, { disconnected: "nubank" });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/admin/api/session") return reply(200, { email: "o@example.com", csrf: "t", reauth_fresh: false });
      if (url === "/admin/api/connections" && method === "GET") return reply(200, { connections: [connection] });
      if (url === "/admin/api/connections/nubank/disconnect-impact")
        return reply(200, {
          alias: "nubank",
          total_transactions: 1500,
          before_window: 1240,
          oldest_date: "2025-01-01",
          window_start: "2026-03-01",
        });
      if (url === "/admin/api/server")
        return reply(200, { last_full_refresh: null, next_full_refresh: null, db_size_bytes: 1, schema_version: 1, app_version: "0.1.0" });
      if (url === "/admin/api/connections/nubank" && method === "DELETE") return disconnectReply();
      return reply(404, { error: "not_found" });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "Disconnect" }));
  return await screen.findByRole("dialog");
}

describe("disconnect dialog", () => {
  it("shows the impact with numbers and gates the button on the typed alias", async () => {
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    expect(dialog).toHaveTextContent(
      "1240 transactions before 2026-03-01 are no longer provided by Pluggy and will be lost.",
    );
    const buttons = screen.getAllByRole("button", { name: "Disconnect" });
    const action = buttons[buttons.length - 1];
    expect(action).toBeDisabled();
    const input = screen.getByLabelText("Type nubank to confirm");
    await user.type(input, "nuban");
    expect(action).toBeDisabled();
    await user.type(input, "k");
    expect(action).toBeEnabled();
    await user.type(input, "x");
    expect(action).toBeDisabled();
  });

  it("redirects to login with next on reauth_required", async () => {
    const user = userEvent.setup();
    disconnectReply = () => reply(403, { error: "reauth_required" });
    await openDialog(user);
    await user.type(screen.getByLabelText("Type nubank to confirm"), "nubank");
    const buttons = screen.getAllByRole("button", { name: "Disconnect" });
    await user.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/admin/auth/login?next=%2Fadmin"));
  });
});

describe("screen survives re-auth", () => {
  it("opens the dialog for ?disconnect=<alias> and uses it as next on reauth", async () => {
    const user = userEvent.setup();
    window.history.pushState({}, "", "/admin?disconnect=nubank");
    vi.stubGlobal("location", { pathname: "/admin", search: "?disconnect=nubank", assign });
    disconnectReply = () => reply(403, { error: "reauth_required" });
    render(<App />);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Type nubank to confirm"), "nubank");
    const buttons = screen.getAllByRole("button", { name: "Disconnect" });
    await user.click(buttons[buttons.length - 1]);
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/admin/auth/login?next=%2Fadmin%3Fdisconnect%3Dnubank"),
    );
  });
});

describe("expired session", () => {
  it("shows sign in when a call after the initial session returns 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/admin/api/session"
          ? reply(200, { email: "o@example.com", csrf: "t", reauth_fresh: true })
          : reply(401, { error: "signed_out" }),
      ),
    );
    render(<App />);
    expect(await screen.findByRole("link", { name: "Continue with Google" })).toBeInTheDocument();
  });
});
