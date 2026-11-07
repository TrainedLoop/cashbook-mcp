import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, existingItemIds, resetSession, suggestAlias } from "../api";

function reply(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetSession();
  fetchMock = vi.fn(async (url: string) =>
    url === "/admin/api/session" ? reply(200, { email: "o@example.com", csrf: "tok123", reauth_fresh: true }) : reply(200, {}),
  );
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function callFor(method: string, url: string) {
  const call = fetchMock.mock.calls.find(([u, init]) => u === url && (init?.method ?? "GET") === method);
  expect(call, `${method} ${url}`).toBeTruthy();
  return call![1] as RequestInit;
}

describe("api CSRF", () => {
  it("sends X-CSRF-Token on POST", async () => {
    await api.connectToken();
    const init = callFor("POST", "/admin/api/connect-token");
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
    expect(init.body).toBeUndefined();
    expect(init.credentials).toBe("same-origin");
  });

  it("sends X-CSRF-Token on DELETE", async () => {
    await api.disconnect("nubank");
    const init = callFor("DELETE", "/admin/api/connections/nubank");
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
  });

  it("sends it on reload, add and preview, and does not send it on GET", async () => {
    await api.reload();
    await api.reload("nubank");
    await api.addConnection("nubank", "it1");
    await api.preview("it1");
    await api.discover(["it1"]);
    for (const [m, u] of [
      ["POST", "/admin/api/connections/discover"],
      ["POST", "/admin/api/reload"],
      ["POST", "/admin/api/connections/nubank/reload"],
      ["POST", "/admin/api/connections"],
      ["POST", "/admin/api/connections/preview"],
    ]) {
      expect((callFor(m, u).headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
    }
    await api.connections();
    expect((callFor("GET", "/admin/api/connections").headers as Record<string, string>)?.["X-CSRF-Token"]).toBeUndefined();
  });

  it("sends it on logout", async () => {
    await api.logout();
    expect((callFor("POST", "/admin/auth/logout").headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
  });
});

describe("api errors", () => {
  it("throws ApiError with status and code", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url === "/admin/api/session" ? reply(200, { email: "e", csrf: "t", reauth_fresh: true }) : reply(422, { error: "alias_taken" }),
    );
    const err = await api.connectToken().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.code).toBe("alias_taken");
  });

  it("maps 401 to signed_out", async () => {
    fetchMock.mockImplementation(async () => reply(401, { error: "signed_out" }));
    const err = await api.connections().catch((e) => e);
    expect(err.status).toBe(401);
    expect(err.code).toBe("signed_out");
  });

  it("redirects to login with next on 403 reauth_required", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/admin", search: "", assign });
    fetchMock.mockImplementation(async (url: string) =>
      url === "/admin/api/session" ? reply(200, { email: "e", csrf: "t", reauth_fresh: false }) : reply(403, { error: "reauth_required" }),
    );
    const err = await api.disconnect("nubank").catch((e) => e);
    expect(err.code).toBe("reauth_required");
    expect(assign).toHaveBeenCalledWith("/admin/auth/login?next=%2Fadmin");
  });
});

describe("existingItemIds", () => {
  it("reads the item ids from Pluggy's duplicate refusal", async () => {
    fetchMock.mockImplementation(async () =>
      reply(400, { code: 400, codeDescription: "ITEM_USER_ALREADY_EXISTS", data: { items: ["a-1", "b-2", 7, "../x"] } }),
    );
    expect(await existingItemIds("ct-1", 200)).toEqual(["a-1", "b-2"]);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.pluggy.ai/items");
    expect((init.headers as Record<string, string>)["X-API-KEY"]).toBe("ct-1");
    expect(init.body).toBe(JSON.stringify({ connectorId: 200, parameters: {} }));
    expect(init.credentials).toBeUndefined();
  });

  it("gives an empty list for any other answer", async () => {
    fetchMock.mockImplementation(async () => reply(400, { codeDescription: "OTHER", data: { items: ["a-1"] } }));
    expect(await existingItemIds("ct-1", 200)).toEqual([]);
    fetchMock.mockImplementation(async () => reply(201, { id: "new" }));
    expect(await existingItemIds("ct-1", 200)).toEqual([]);
    fetchMock.mockImplementation(async () => {
      throw new TypeError("network");
    });
    expect(await existingItemIds("ct-1", 200)).toEqual([]);
  });
});

describe("suggestAlias", () => {
  it("derives a short alias from the bank name", () => {
    const none = new Set<string>();
    expect(suggestAlias("BANCO INTER", none)).toBe("inter");
    expect(suggestAlias("C6 BANK", none)).toBe("c6-bank");
    expect(suggestAlias("Caixa Econômica", none)).toBe("caixa-economica");
    expect(suggestAlias("XP", none)).toBe("xp");
    expect(suggestAlias("Banco", none)).toBe("banco");
    expect(suggestAlias("", none)).toBe("bank");
    expect(suggestAlias(null, none)).toBe("bank");
  });

  it("avoids taken aliases", () => {
    expect(suggestAlias("XP", new Set(["xp", "xp-2"]))).toBe("xp-3");
  });
});
