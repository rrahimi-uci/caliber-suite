export interface LocalAuthSession {
  username: string;
  identity: string;
  createdAt: string;
}

const AUTH_KEY = "caliber.auth.session";
export const AUTH_CHANGED_EVENT = "caliber-auth-changed";
export const DEFAULT_LOGIN_USERNAME = "admin";
export const DEFAULT_LOGIN_PASSWORD = "admin";

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
  return username === DEFAULT_LOGIN_USERNAME ? "@local-admin" : `@${username}`;
}

export function getStoredAuthSession(): LocalAuthSession | null {
  if (typeof window === "undefined") return null;
  return parseSession(window.localStorage.getItem(AUTH_KEY));
}

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

export function isDefaultCredential(username: string, password: string): boolean {
  return username.trim() === DEFAULT_LOGIN_USERNAME && password === DEFAULT_LOGIN_PASSWORD;
}
