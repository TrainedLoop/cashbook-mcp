export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(`${status} ${code}`);
    this.name = "ApiError";
  }
}

export interface SessionInfo {
  email: string;
  csrf: string;
  reauth_fresh: boolean;
}
export interface Account {
  id: string;
  type: string;
  subtype: string | null;
  name: string;
  number?: string | null;
  purpose?: Purpose;
}

const SUBTYPES: Record<string, string> = {
  CHECKING_ACCOUNT: "Checking",
  SAVINGS_ACCOUNT: "Savings",
  CREDIT_CARD: "Credit card",
};

/** Name plus kind and masked number: some banks give every account the same name. */
export function accountLabel(a: Account): string {
  const kind = a.subtype ? (SUBTYPES[a.subtype] ?? null) : null;
  return [a.name, kind, a.number].filter(Boolean).join(" · ");
}
export type Role = "personal" | "business";
export type Purpose = "regular" | "investment";
export interface Connection {
  alias: string;
  role: Role;
  bank: string | null;
  status: string | null;
  execution_status: string | null;
  error_code: string | null;
  last_updated_at: string | null;
  last_synced_at: string | null;
  consent_expires_at: string | null;
  accounts: Account[];
  history: { transactions: number; date_from: string | null; date_to: string | null };
  hint: string | null;
  sync_failed: boolean;
  syncing: boolean;
  badge: "ok" | "outdated" | "login error" | "consent revoked" | "syncing";
}
export interface ConnectTokenResponse {
  accessToken: string;
  connectorIds: number[];
}
export interface ItemPreview {
  item_id: string;
  bank: string;
  status: string | null;
  accounts: { name: string; type: string | null; number: string | null }[];
  suggested_role?: Role;
}
/** A preview from discover: `alias` when already connected, `error` when it cannot be added. */
export interface FoundItem extends Omit<ItemPreview, "bank"> {
  bank: string | null;
  alias: string | null;
  error: string | null;
}
export interface DisconnectImpact {
  alias: string;
  total_transactions: number;
  before_window: number;
  oldest_date: string | null;
  window_start: string;
}
export interface ServerInfo {
  last_full_refresh: string | null;
  next_full_refresh: string | null;
  db_size_bytes: number;
  schema_version: number;
  app_version: string;
}

export const LOGIN_PATH = "/admin/auth/login";

export function loginUrl(next: string): string {
  return `${LOGIN_PATH}?next=${encodeURIComponent(next)}`;
}

let cached: SessionInfo | null = null;
const unauthorized = new Set<() => void>();

/** Call `cb` whenever the API answers 401 (the session expired). Returns an unsubscribe function. */
export function onUnauthorized(cb: () => void): () => void {
  unauthorized.add(cb);
  return () => void unauthorized.delete(cb);
}

export function resetSession(): void {
  cached = null;
}

async function call<T>(method: string, path: string, body?: unknown, mutating = method !== "GET"): Promise<T> {
  const headers: Record<string, string> = {};
  if (mutating) headers["X-CSRF-Token"] = (cached ?? (await api.session())).csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const init: RequestInit = { method, credentials: "same-origin" };
  if (Object.keys(headers).length > 0) init.headers = headers;
  if (body !== undefined) init.body = JSON.stringify(body);
  const res = await fetch(path, init);
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // an empty or non-JSON body leaves data null
  }
  if (!res.ok) {
    const code = (data as { error?: string } | null)?.error ?? "unknown";
    if (res.status === 401) {
      cached = null;
      unauthorized.forEach((cb) => cb());
    }
    if (res.status === 403 && code === "reauth_required") {
      window.location.assign(loginUrl(window.location.pathname + window.location.search));
    }
    throw new ApiError(res.status, code);
  }
  return data as T;
}

const enc = encodeURIComponent;

export const api = {
  async session(): Promise<SessionInfo> {
    cached = await call<SessionInfo>("GET", "/admin/api/session");
    return cached;
  },
  connections: () => call<{ connections: Connection[] }>("GET", "/admin/api/connections"),
  connectToken: () => call<ConnectTokenResponse>("POST", "/admin/api/connect-token"),
  preview: (itemId: string) => call<ItemPreview>("POST", "/admin/api/connections/preview", { item_id: itemId }),
  discover: (itemIds: string[]) =>
    call<{ items: FoundItem[] }>("POST", "/admin/api/connections/discover", { item_ids: itemIds }),
  addConnection: (alias: string, itemId: string, role?: Role) =>
    call<{ alias: string; started: boolean }>("POST", "/admin/api/connections", {
      alias,
      item_id: itemId,
      ...(role ? { role } : {}),
    }),
  setRole: (alias: string, role: Role) =>
    call<{ alias: string; role: Role }>("PATCH", `/admin/api/connections/${enc(alias)}`, { role }),
  setPurpose: (accountId: string, purpose: Purpose) =>
    call<{ account_id: string; purpose: Purpose }>("PATCH", `/admin/api/accounts/${enc(accountId)}`, { purpose }),
  reload: (alias?: string) =>
    call<{ started: boolean }>("POST", alias ? `/admin/api/connections/${enc(alias)}/reload` : "/admin/api/reload"),
  disconnectImpact: (alias: string) =>
    call<DisconnectImpact>("GET", `/admin/api/connections/${enc(alias)}/disconnect-impact`),
  disconnect: (alias: string) => call<{ disconnected: string }>("DELETE", `/admin/api/connections/${enc(alias)}`),
  server: () => call<ServerInfo>("GET", "/admin/api/server"),
  logout: () => call<{ signed_out: boolean }>("POST", "/admin/auth/logout"),
};

const PLUGGY_API = "https://api.pluggy.ai";
const ITEM_ID_RE = /^[A-Za-z0-9-]{1,64}$/;

/**
 * The MeuPluggy items that already exist for this application. Pluggy does not list items on
 * personal accounts, but a connect token issued with avoidDuplicates refuses a second MeuPluggy
 * item and names the existing ones; this asks for one and reads the refusal. Call it only once an
 * item is known to exist (after the widget succeeded or was refused): with none, Pluggy would
 * create a pending item. Any other answer gives an empty list.
 */
export async function existingItemIds(connectToken: string, connectorId: number): Promise<string[]> {
  try {
    const res = await fetch(`${PLUGGY_API}/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-KEY": connectToken },
      body: JSON.stringify({ connectorId, parameters: {} }),
    });
    if (res.status !== 400) return [];
    const data = (await res.json()) as { codeDescription?: unknown; data?: { items?: unknown } } | null;
    const items = data?.codeDescription === "ITEM_USER_ALREADY_EXISTS" ? data.data?.items : null;
    return Array.isArray(items) ? items.filter((i): i is string => typeof i === "string" && ITEM_ID_RE.test(i)) : [];
  } catch {
    return [];
  }
}

export const ALIAS_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

/** An alias derived from a bank name ("BANCO INTER" -> "inter"), not in `taken`. */
export function suggestAlias(bank: string | null, taken: Set<string>): string {
  let base = (bank ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/^banco-(?=.)/, "")
    .slice(0, 28)
    .replace(/-+$/, "");
  if (!ALIAS_RE.test(base)) base = "bank";
  let alias = base;
  for (let n = 2; taken.has(alias); n++) alias = `${base}-${n}`;
  return alias;
}

/** Trim and lowercase an alias; null when it does not match the server's rule. */
export function normalizeAlias(raw: string): string | null {
  const alias = raw.trim().toLowerCase();
  return ALIAS_RE.test(alias) ? alias : null;
}

export const ALIAS_HINT = "Use lowercase letters, digits and hyphens, starting with a letter or digit (up to 32 characters).";

const MESSAGES: Record<string, string> = {
  alias_invalid: ALIAS_HINT,
  alias_taken: "That alias is already in use.",
  item_unknown: "Pluggy does not know that item id for this application.",
  item_not_meupluggy: "That item is not a MeuPluggy connection.",
  item_taken: "That item is already connected.",
  pluggy_unavailable: "Pluggy is unavailable right now. Try again in a moment.",
  reload_in_progress: "A reload is already running.",
  role_invalid: "That role is not valid.",
  purpose_invalid: "That account type is not valid.",
  not_found: "That connection no longer exists.",
  csrf: "Your session looks out of date. Reload the page and try again.",
  reauth_required: "Redirecting to Google to confirm it is you...",
};

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return MESSAGES[err.code] ?? "Something went wrong. Try again.";
  return "Could not reach the server. Try again.";
}
