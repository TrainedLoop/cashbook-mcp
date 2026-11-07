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
  accounts: [
    { id: "a1", type: "BANK", subtype: "CHECKING_ACCOUNT", name: "Conta", purpose: "regular" },
    { id: "c1", type: "CREDIT", subtype: "CREDIT_CARD", name: "Cartao" },
  ],
  history: { transactions: 1, date_from: null, date_to: null },
  hint: null,
  sync_failed: false,
  syncing: false,
  badge: "ok",
};

let patchReply: (url: string) => Response;
let calls: { url: string; method: string; body: unknown }[];

beforeEach(() => {
  resetSession();
  calls = [];
  patchReply = () => reply(200, {});
  vi.stubGlobal("location", { pathname: "/admin", search: "", assign: vi.fn() });
  window.history.pushState({}, "", "/admin");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(init.body as string) : undefined });
      if (url === "/admin/api/session") return reply(200, { email: "o@example.com", csrf: "t", reauth_fresh: false });
      if (url === "/admin/api/connections" && method === "GET") return reply(200, { connections: [connection] });
      if (url === "/admin/api/server")
        return reply(200, { last_full_refresh: null, next_full_refresh: null, db_size_bytes: 1, schema_version: 1, app_version: "0.2.0" });
      if (method === "PATCH") return patchReply(url);
      if (url === "/admin/api/connections/preview")
        return reply(200, { item_id: "i1", bank: "Biz Bank", status: "UPDATED", accounts: [], suggested_role: "business" });
      if (url === "/admin/api/connections" && method === "POST") return reply(200, { alias: "biz", started: true });
      return reply(404, { error: "not_found" });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("role and purpose selectors", () => {
  it("PATCHes the connection role on change", async () => {
    const user = userEvent.setup();
    patchReply = () => reply(200, { alias: "nubank", role: "business" });
    render(<App />);
    await user.selectOptions(await screen.findByLabelText("Role for nubank"), "business");
    await waitFor(() =>
      expect(calls).toContainEqual({ url: "/admin/api/connections/nubank", method: "PATCH", body: { role: "business" } }),
    );
    expect(screen.getByLabelText("Role for nubank")).toHaveValue("business");
  });

  it("PATCHes the account purpose, only for bank accounts", async () => {
    const user = userEvent.setup();
    patchReply = () => reply(200, { account_id: "a1", purpose: "investment" });
    render(<App />);
    await user.selectOptions(await screen.findByLabelText("Account type for Conta"), "investment");
    await waitFor(() =>
      expect(calls).toContainEqual({ url: "/admin/api/accounts/a1", method: "PATCH", body: { purpose: "investment" } }),
    );
    expect(screen.queryByLabelText("Account type for Cartao")).toBeNull();
  });

  it("shows the error and keeps the previous value on failure", async () => {
    const user = userEvent.setup();
    patchReply = () => reply(422, { error: "role_invalid" });
    render(<App />);
    await user.selectOptions(await screen.findByLabelText("Role for nubank"), "business");
    expect(await screen.findByText("That role is not valid.")).toBeInTheDocument();
    expect(screen.getByLabelText("Role for nubank")).toHaveValue("personal");
  });

  it("add existing sends the suggested role", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Add existing item" }));
    await user.type(screen.getByLabelText("Alias"), "biz");
    await user.type(screen.getByLabelText("Item id"), "i1");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByLabelText("Role")).toHaveValue("business");
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    await waitFor(() =>
      expect(calls).toContainEqual({
        url: "/admin/api/connections",
        method: "POST",
        body: { alias: "biz", item_id: "i1", role: "business" },
      }),
    );
  });
});
