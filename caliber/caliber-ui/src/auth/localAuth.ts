export interface LocalAuthSession {
  username: string;
  identity: string;
  createdAt: string;
}

const AUTH_KEY = "caliber.auth.session";
export const AUTH_CHANGED_EVENT = "caliber-auth-changed";

/**
 * There is no default credential any more.
 *
 * This module used to define `admin`/`admin`, validate it in the browser, and
 * synthesise an identity the backend then trusted via `X-CALIBER-User` — so any
 * client could assert any identity (C1). Credentials are now verified server-side by
 * `POST /caliber/auth/login`, which sets an HttpOnly session cookie.
 *
 * What remains here is *display* state: which username the browser last signed in
 * as, so the shell can render it and decide whether to show a login form. It is
 * deliberately NOT an authorization input — the cookie is, and the browser cannot
 * read it.
 */
export const AUTH_SESSION_KEY = AUTH_KEY;

function parseSession(raw: string | null): LocalAuthSession | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<LocalAuthSession>;
    if (
      typeof parsed.username === "string" &&
      typeof parsed.identity === "string" &&
      typeof parsed.createdAt === "string"
    ) {
      return {
        username: parsed.username,
        identity: parsed.identity,
        createdAt: parsed.createdAt,
      };
    }
  } catch {
    // Invalid persisted auth is treated as logged out.
  }
  return null;
}

function emitAuthChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
}

export function identityForUsername(username: string): string {
  return username.startsWith("@") ? username : `@${username}`;
}

export function getStoredAuthSession(): LocalAuthSession | null {
  if (typeof window === "undefined") return null;
  return parseSession(window.localStorage.getItem(AUTH_KEY));
}

/**
 * The `X-CALIBER-User` header value, or `null`.
 *
 * Only meaningful when the deployment runs in `trusted_header` mode. In the shipped
 * `session` mode the backend ignores this header entirely; it is still sent so a
 * proxy-mode deployment keeps working without a separate client build.
 */
export function getCaliberUserHeader(): string | null {
  return getStoredAuthSession()?.identity ?? null;
}

export function createLocalAuthSession(username: string): LocalAuthSession {
  return {
    username,
    identity: identityForUsername(username),
    createdAt: new Date().toISOString(),
  };
}

export function saveLocalAuthSession(session: LocalAuthSession): void {
  window.localStorage.setItem(AUTH_KEY, JSON.stringify(session));
  emitAuthChanged();
}

export function clearLocalAuthSession(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(AUTH_KEY);
  emitAuthChanged();
}

/**
 * Sign in against the server.
 *
 * Replaces the browser-side credential check. On success the server sets an HttpOnly
 * session cookie; the returned username is persisted only so the shell can display
 * it. A failure message is deliberately generic, matching the server, so the form
 * cannot be used to enumerate accounts.
 */
export async function signIn(username: string, password: string): Promise<void> {
  const { caliberApi } = await import("@/api/caliberApi");
  const result = await caliberApi.login(username.trim(), password);
  saveLocalAuthSession({
    username: username.trim(),
    identity: identityForUsername(result.user_id),
    createdAt: new Date().toISOString(),
  });
}

/** Revoke the session server-side, then clear local display state. */
export async function signOut(): Promise<void> {
  try {
    const { caliberApi } = await import("@/api/caliberApi");
    await caliberApi.logout();
  } catch {
    // A failed revoke must not trap the user in a signed-in UI; the local state is
    // cleared either way and the cookie expires on its own.
  }
  clearLocalAuthSession();
}
