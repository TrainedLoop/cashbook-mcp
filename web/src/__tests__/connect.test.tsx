import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react-pluggy-connect", () => ({
  PluggyConnect: (props: {
    connectToken: string;
    includeSandbox?: boolean;
    connectorIds?: number[];
    onSuccess: (d: { item: { id: string } }) => void;
  }) => (
    <button onClick={() => props.onSuccess({ item: { id: "it1" } })}>
      widget {props.connectToken} sandbox={String(props.includeSandbox)} ids={(props.connectorIds ?? []).join(",")}
    </button>
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
import { api, ApiError, type Connection } from "../api";

const m = vi.mocked(api);

const conn = (alias: string): Connection => ({
  alias,
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
  it("adds the connection with the widget's item id and refreshes the list", async () => {
    const user = userEvent.setup();
    m.connectToken.mockResolvedValue({ accessToken: "ct-1", connectorIds: [200] });
    m.addConnection.mockResolvedValue({ alias: "nubank", started: true });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Connect with MeuPluggy" }));
    await user.type(screen.getByLabelText("Alias"), "  NuBank ");
    m.connections.mockResolvedValue({ connections: [conn("nubank")] });
    await user.click(screen.getByRole("button", { name: "Connect with MeuPluggy" }));

    expect(m.connectToken).toHaveBeenCalledWith("nubank");
    const widget = await screen.findByRole("button", { name: /widget ct-1 sandbox=false ids=200/ });
    await user.click(widget);

    await waitFor(() => expect(m.addConnection).toHaveBeenCalledWith("nubank", "it1"));
    expect(await screen.findByText("nubank")).toBeInTheDocument();
  });

  it("rejects an invalid alias inline without calling the API", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Connect with MeuPluggy" }));
    await user.type(screen.getByLabelText("Alias"), "-bad_alias");
    await user.click(screen.getByRole("button", { name: "Connect with MeuPluggy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/lowercase letters, digits and hyphens/i);
    expect(m.connectToken).not.toHaveBeenCalled();
  });

  it("shows a friendly message for a 422 code", async () => {
    const user = userEvent.setup();
    m.connectToken.mockRejectedValue(new ApiError(422, "alias_taken"));
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Connect with MeuPluggy" }));
    await user.type(screen.getByLabelText("Alias"), "nubank");
    await user.click(screen.getByRole("button", { name: "Connect with MeuPluggy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That alias is already in use.");
  });
});

describe("add existing item", () => {
  it("previews, then confirms", async () => {
    const user = userEvent.setup();
    m.preview.mockResolvedValue({
      item_id: "abc-123",
      connector_name: "MeuPluggy",
      status: "UPDATED",
      accounts: [{ name: "Conta", type: "BANK", number: "****1234" }],
    });
    m.addConnection.mockResolvedValue({ alias: "main", started: true });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Add existing item" }));
    await user.type(screen.getByLabelText("Alias"), "main");
    await user.type(screen.getByLabelText("Item id"), "abc-123");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("MeuPluggy")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(m.addConnection).toHaveBeenCalledWith("main", "abc-123"));
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
