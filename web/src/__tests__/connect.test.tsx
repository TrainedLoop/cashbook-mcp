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
import { api, ApiError, type Connection, type FoundItem } from "../api";

const m = vi.mocked(api);

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

  it("names the bank the widget just authorized and adds it", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    m.discover.mockResolvedValue({ items: [found("it1", "BANCO INTER")] });
    m.addConnection.mockResolvedValue({ alias: "x", started: true });
    await openWidget(user);

    expect(m.connectToken).toHaveBeenCalledWith();
    await user.click(await screen.findByRole("button", { name: /widget ct-1 sandbox=false ids=200/ }));

    expect(await screen.findByRole("heading", { name: "Add this bank" })).toBeInTheDocument();
    expect(m.discover).toHaveBeenCalledWith(["it1"]);
    const alias = screen.getByLabelText("Alias for BANCO INTER");
    expect(alias).toHaveValue("inter");
    await user.clear(alias);
    await user.type(alias, "Inter-2");

    m.connections.mockResolvedValue({ connections: [conn("inter-2")] });
    await user.click(screen.getByRole("button", { name: "Add 1 bank" }));
    await waitFor(() => expect(m.addConnection).toHaveBeenCalledWith("inter-2", "it1", "personal"));
    expect(await screen.findByRole("heading", { name: "Connections" })).toBeInTheDocument();
  });

  it("shows a bank that is already stored as connected", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    m.discover.mockResolvedValue({ items: [found("it1", "CAIXA", "caixa")] });
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: /widget ct-1/ }));

    expect(await screen.findByText("caixa")).toBeInTheDocument();
    expect(screen.queryByLabelText("Alias for CAIXA")).not.toBeInTheDocument();
    expect(screen.getByText("This bank is already connected.")).toBeInTheDocument();
  });

  it("warns, and leaves unchecked, a bank already connected under another alias", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    m.discover.mockResolvedValue({ items: [{ ...found("it1", "BANCO X"), same_bank_as: "banco-x" }] });
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: /widget ct-1/ }));

    expect(await screen.findByText(/may count its money twice/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Connect BANCO X" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Add 0 banks" })).toBeDisabled();
  });

  it("keeps the screen and shows the error when the bank could not be added", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    m.discover.mockResolvedValue({ items: [found("it1", "XP")] });
    m.addConnection.mockRejectedValue(new ApiError(422, "alias_taken"));
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: /widget ct-1/ }));
    await user.click(await screen.findByRole("button", { name: "Add 1 bank" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("That alias is already in use.");
    expect(screen.getByRole("button", { name: "Add 1 bank" })).toBeInTheDocument();
  });

  it("says the connection did not complete when the widget reports an error, without probing Pluggy", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: "widget refuses" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The connection did not complete. Try again.");
    expect(m.discover).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Connect with MeuPluggy" })).toBeEnabled();
    fetchSpy.mockRestore();
  });

  it("goes back to the start when the widget is closed without a result", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    await openWidget(user);
    await user.click(await screen.findByRole("button", { name: "widget closes" }));
    expect(screen.queryByRole("button", { name: "widget closes" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect with MeuPluggy" })).toBeEnabled();
    expect(m.discover).not.toHaveBeenCalled();
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
