import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react-pluggy-connect", () => ({
  PluggyConnect: (props: {
    connectToken: string;
    includeSandbox?: boolean;
    connectorIds?: number[];
    onSuccess: (d: { item: { id: string } }) => void;
    onError: (e: { message: string }) => void;
    onClose: () => void;
  }) => (
    <div>
      <button onClick={() => props.onSuccess({ item: { id: "it1" } })}>
        widget {props.connectToken} sandbox={String(props.includeSandbox)} ids={(props.connectorIds ?? []).join(",")}
      </button>
      <button onClick={() => props.onError({ message: "There are other items with the same credentials" })}>
        widget refuses
      </button>
      <button onClick={() => props.onClose()}>widget closes</button>
    </div>
  ),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    existingItemIds: vi.fn(),
    api: {
      session: vi.fn(),
      connections: vi.fn(),
      connectToken: vi.fn(),
      preview: vi.fn(),
      discover: vi.fn(),
      addConnection: vi.fn(),
      reload: vi.fn(),
      disconnectImpact: vi.fn(),
      disconnect: vi.fn(),
      server: vi.fn(),
      logout: vi.fn(),
    },
  };
});

import App from "../App";
import { api, ApiError, existingItemIds, type Connection, type FoundItem } from "../api";

const m = vi.mocked(api);
const existing = vi.mocked(existingItemIds);

const found = (item_id: string, bank: string, alias: string | null = null): FoundItem => ({
  item_id,
  bank,
  status: "UPDATED",
  accounts: [{ name: bank, type: "BANK", number: "****1234" }],
  alias,
  error: null,
});

const conn = (alias: string): Connection => ({
  alias,
  role: "personal",
  bank: "Meu Banco",
  status: "UPDATED",
  execution_status: "SUCCESS",
  error_code: null,
  last_updated_at: null,
  last_synced_at: "2026-11-06T09:00:00+00:00",
  consent_expires_at: null,
  accounts: [{ id: "a1", type: "BANK", subtype: "CHECKING_ACCOUNT", name: "Conta" }],
  history: { transactions: 3, date_from: "2026-01-01", date_to: "2026-11-01" },
  hint: null,
  sync_failed: false,
  syncing: false,
  badge: "ok",
});

beforeEach(() => {
  vi.resetAllMocks();
  m.session.mockResolvedValue({ email: "o@example.com", csrf: "t", reauth_fresh: true });
  m.server.mockResolvedValue({
    last_full_refresh: null,
    next_full_refresh: null,
    db_size_bytes: 2048,
    schema_version: 3,
    app_version: "0.1.0",
  });
  m.connections.mockResolvedValue({ connections: [] });
  window.history.pushState({}, "", "/admin");
});

describe("connect with MeuPluggy", () => {
  async function openWidget(user: ReturnType<typeof userEvent.setup>) {
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Connect with MeuPluggy" }));
    await user.click(screen.getByRole("button", { name: "Connect with MeuPluggy" }));
  }

  it("lists every MeuPluggy item after the widget succeeds and adds the chosen ones", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    existing.mockResolvedValue(["it2", "it3"]);
    m.discover.mockResolvedValue({
      items: [found("it1", "BANCO INTER"), found("it2", "CAIXA", "caixa"), found("it3", "XP")],
    });
    m.addConnection.mockResolvedValue({ alias: "x", started: true });
    await openWidget(user);

    expect(m.connectToken).toHaveBeenCalledWith();
    await user.click(await screen.findByRole("button", { name: /widget ct-1 sandbox=false ids=200/ }));

    expect(await screen.findByRole("heading", { name: "Choose your banks" })).toBeInTheDocument();
    expect(existing).toHaveBeenCalledWith("ct-1", 200);
    expect(m.discover).toHaveBeenCalledWith(["it1", "it2", "it3"]);
    expect(screen.getByText("caixa")).toBeInTheDocument();
    expect(screen.queryByLabelText("Alias for CAIXA")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Alias for BANCO INTER")).toHaveValue("inter");
    const xp = screen.getByLabelText("Alias for XP");
    expect(xp).toHaveValue("xp");
    await user.clear(xp);
    await user.type(xp, "XP-Invest");

    m.connections.mockResolvedValue({ connections: [conn("inter")] });
    await user.click(screen.getByRole("button", { name: "Add 2 banks" }));
    await waitFor(() => expect(m.addConnection).toHaveBeenCalledTimes(2));
    expect(m.addConnection).toHaveBeenNthCalledWith(1, "inter", "it1", "personal");
    expect(m.addConnection).toHaveBeenNthCalledWith(2, "xp-invest", "it3", "personal");
    expect(await screen.findByRole("heading", { name: "Connections" })).toBeInTheDocument();
  });

  it("uses Pluggy's refusal when the widget reports an error, and skips unchecked banks", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    existing.mockResolvedValue(["it2", "it3"]);
    m.discover.mockResolvedValue({ items: [found("it2", "CAIXA"), found("it3", "XP")] });
    m.addConnection.mockResolvedValue({ alias: "x", started: true });
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: "widget refuses" }));

    await user.click(await screen.findByRole("checkbox", { name: "Connect XP" }));
    await user.click(screen.getByRole("button", { name: "Add 1 bank" }));
    await waitFor(() => expect(m.addConnection).toHaveBeenCalledWith("caixa", "it2", "personal"));
    expect(m.addConnection).toHaveBeenCalledTimes(1);
  });

  it("keeps the screen and shows the error of a bank that could not be added", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    existing.mockResolvedValue(["it2", "it3"]);
    m.discover.mockResolvedValue({ items: [found("it2", "CAIXA"), found("it3", "XP")] });
    m.addConnection.mockImplementation(async (alias) => {
      if (alias === "xp") throw new ApiError(422, "alias_taken");
      return { alias, started: true };
    });
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: "widget refuses" }));
    await user.click(await screen.findByRole("button", { name: "Add 2 banks" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("That alias is already in use.");
    expect(screen.getByText("Added as", { exact: false })).toHaveTextContent("Added as caixa.");
    expect(screen.getByRole("button", { name: "Add 1 bank" })).toBeInTheDocument();
  });

  it("says so when nothing was found", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    existing.mockResolvedValue([]);
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: "widget refuses" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The connection did not complete. Try again.");
    expect(m.discover).not.toHaveBeenCalled();
  });

  it("goes back to the start when the widget is closed without a result", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: "widget closes" }));
    expect(screen.queryByRole("button", { name: "widget closes" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect with MeuPluggy" })).toBeEnabled();
    expect(existing).not.toHaveBeenCalled();
  });

  it("shows a friendly message when the token cannot be issued", async () => {
    const user = userEvent.setup();
    m.connectToken.mockRejectedValue(new ApiError(502, "pluggy_unavailable"));
    await openWidget(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Pluggy is unavailable right now.");
  });
});

describe("add existing item", () => {
  it("previews, then confirms", async () => {
    const user = userEvent.setup();
    m.preview.mockResolvedValue({
      item_id: "abc-123",
      bank: "BANCO TESTE",
      status: "UPDATED",
      accounts: [{ name: "Conta", type: "BANK", number: "****1234" }],
    });
    m.addConnection.mockResolvedValue({ alias: "main", started: true });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Add existing item" }));
    await user.type(screen.getByLabelText("Alias"), "main");
    await user.type(screen.getByLabelText("Item id"), "abc-123");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("BANCO TESTE")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(m.addConnection).toHaveBeenCalledWith("main", "abc-123", "personal"));
  });
});

describe("sign in and reload", () => {
  it("shows Continue with Google as a link on 401", async () => {
    m.session.mockRejectedValue(new ApiError(401, "signed_out"));
    render(<App />);
    const link = await screen.findByRole("link", { name: "Continue with Google" });
    expect(link).toHaveAttribute("href", "/admin/auth/login?next=%2Fadmin");
  });

  it("polls connections while one is syncing, after Reload now", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const syncing: Connection = { ...conn("nubank"), syncing: true, badge: "syncing" };
      m.connections.mockResolvedValue({ connections: [conn("nubank")] });
      m.reload.mockResolvedValue({ started: true });
      render(<App />);
      const reloadButton = await screen.findByRole("button", { name: "Reload now" });
      m.connections.mockResolvedValue({ connections: [syncing] });
      await user.click(reloadButton);
      await waitFor(() => expect(m.reload).toHaveBeenCalled());
      await screen.findAllByText("syncing");
      const calls = m.connections.mock.calls.length;
      m.connections.mockResolvedValue({ connections: [conn("nubank")] });
      await vi.advanceTimersByTimeAsync(3100);
      expect(m.connections.mock.calls.length).toBeGreaterThan(calls);
      const after = m.connections.mock.calls.length;
      await vi.advanceTimersByTimeAsync(10000);
      expect(m.connections.mock.calls.length).toBe(after);
    } finally {
      vi.useRealTimers();
    }
  });
});
