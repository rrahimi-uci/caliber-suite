export interface LocalAuthSession {
  username: string;
  identity: string;
  createdAt: string;
  /** Opaque browser-login generation used to reject stale async auth responses. */
  generation: string;
}

export interface AuthChangedDetail {
  notice?: string;
}

const AUTH_KEY = "caliber.auth.session";
const AUTH_EPOCH_KEY = "caliber.auth.epoch";
const AUTH_OPERATION_LOCK = "caliber.auth.cookie-operation";
export const AUTH_CHANGED_EVENT = "caliber-auth-changed";

// The HttpOnly session cookie is shared by every same-origin tab. Login and logout
// responses both mutate that cookie, so their network requests and matching display-
// state writes must be one serialized operation. Otherwise response-cookie order and
// localStorage order can disagree (for example, cookie=user-a while the shell says
// user-b). Web Locks supplies an origin-wide mutex in supported browsers. The promise
// tail is still useful for non-browser callers and older engines. It only serializes
// this JavaScript realm, so the operation also detects a competing auth epoch and
// reconciles against the authoritative cookie before updating display state.
let inProcessAuthOperationTail: Promise<void> = Promise.resolve();

type AuthOperationOutcome<T> =
  | { ok: true; value: T }
  | { ok: false; error: unknown };

async function withInProcessAuthOperation<T>(
  operation: () => Promise<T>,
): Promise<T> {
  const previous = inProcessAuthOperationTail;
  let release!: () => void;
  inProcessAuthOperationTail = new Promise<void>((resolve) => {
    release = resolve;
  });
  await previous;
  try {
    return await operation();
  } finally {
    release();
  }
}

async function withAuthOperationLock<T>(
  operation: () => Promise<T>,
): Promise<T> {
  if (typeof navigator !== "undefined") {
    let manager: LockManager | undefined;
    try {
      manager = (navigator as Navigator & { locks?: LockManager }).locks;
    } catch {
      // Hardened contexts can expose navigator while denying access to Web Locks.
      return withInProcessAuthOperation(operation);
    }
    if (manager && typeof manager.request === "function") {
      let outcome: AuthOperationOutcome<T>;
      let callbackStarted = false;
      try {
        // The callback always resolves with an outcome. That distinction matters:
        // only a LockManager failure should fall back. Retrying when the operation
        // itself rejects would submit a failed login/logout request twice.
        outcome = await manager.request(
          AUTH_OPERATION_LOCK,
          { mode: "exclusive" },
          async (): Promise<AuthOperationOutcome<T>> => {
            callbackStarted = true;
            try {
              return {
                ok: true,
                value: await withInProcessAuthOperation(operation),
              };
            } catch (error) {
              return { ok: false, error };
            }
          },
        );
      } catch (error) {
        // Access to Web Locks can be unavailable in hardened/legacy browser
        // contexts. The same-document mutex below still prevents local overlap;
        // the auth-epoch reconciliation handles a cooperating fallback race.
        // If the callback already started, retrying is unsafe because its network
        // mutation may have completed even though the lock manager rejected.
        if (callbackStarted) throw error;
        return withInProcessAuthOperation(operation);
      }
      if (!outcome.ok) throw outcome.error;
      return outcome.value;
    }
  }
  return withInProcessAuthOperation(operation);
}

/**
 * There is no browser-side default credential or client-side authentication.
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
        // Preserve display-only sessions written by older UI bundles. The next
        // successful login replaces this deterministic compatibility value with a
        // random generation.
        generation:
          typeof parsed.generation === "string" && parsed.generation
            ? parsed.generation
            : `${parsed.username}:${parsed.createdAt}`,
      };
    }
  } catch {
    // Invalid persisted auth is treated as logged out.
  }
  return null;
}

function emitAuthChanged(detail: AuthChangedDetail = {}): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<AuthChangedDetail>(AUTH_CHANGED_EVENT, { detail }),
  );
}

export function identityForUsername(username: string): string {
  return username.startsWith("@") ? username : `@${username}`;
}

export function getStoredAuthSession(): LocalAuthSession | null {
  if (typeof window === "undefined") return null;
  return parseSession(window.localStorage.getItem(AUTH_KEY));
}

/** Monotonic-ish browser auth boundary, including transitions whose session is null. */
export function getAuthEpoch(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(AUTH_EPOCH_KEY) ?? "";
}

function advanceAuthEpoch(): void {
  const epoch =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}:${Math.random().toString(36).slice(2)}`;
  // Write this before the session value. A startup validation that races a cross-tab
  // login/logout then sees the changed epoch before it could apply its stale response.
  window.localStorage.setItem(AUTH_EPOCH_KEY, epoch);
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
  const createdAt = new Date().toISOString();
  const generation =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `${username}:${createdAt}:${Math.random().toString(36).slice(2)}`;
  return {
    username,
    identity: identityForUsername(username),
    createdAt,
    generation,
  };
}

export function saveLocalAuthSession(session: LocalAuthSession): void {
  advanceAuthEpoch();
  window.localStorage.setItem(AUTH_KEY, JSON.stringify(session));
  emitAuthChanged();
}

export function clearLocalAuthSession(notice?: string): void {
  if (typeof window === "undefined") return;
  advanceAuthEpoch();
  window.localStorage.removeItem(AUTH_KEY);
  emitAuthChanged(notice ? { notice } : {});
}

function isAuthenticatedSession(info: {
  user_id: string;
  authenticated_by: string;
  login_required: boolean;
}): boolean {
  return (
    !info.login_required &&
    info.authenticated_by !== "none" &&
    info.user_id !== "anonymous"
  );
}

/**
 * Re-read the HttpOnly cookie after a fallback-mode auth race.
 *
 * A storage epoch changing while login/logout is in flight means another realm wrote
 * display state and may also have changed the shared cookie. Two bounded probes let a
 * second observed epoch settle without overwriting it with a stale probe. If the
 * session endpoint is temporarily unavailable, the mutation response remains the
 * best evidence: login falls back to its returned user and logout falls back to null.
 */
async function reconcileLocalAuthSession(
  fallbackSession: LocalAuthSession | null,
): Promise<void> {
  const { caliberApi } = await import("@/api/caliberApi");
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const probeEpoch = getAuthEpoch();
    try {
      const info = await caliberApi.getAuthSessionForReconciliation();
      if (getAuthEpoch() !== probeEpoch) continue;
      if (isAuthenticatedSession(info)) {
        saveLocalAuthSession(
          fallbackSession?.username === info.user_id
            ? fallbackSession
            : createLocalAuthSession(info.user_id),
        );
      } else {
        clearLocalAuthSession();
      }
      return;
    } catch {
      if (getAuthEpoch() !== probeEpoch) continue;
      if (fallbackSession === null) clearLocalAuthSession();
      else saveLocalAuthSession(fallbackSession);
      return;
    }
  }
  // Continuous cross-realm churn means the newest storage event is better evidence
  // than either completed probe. Leave that state intact instead of clobbering it.
}

/**
 * Sign in against the server.
 *
 * Replaces the browser-side credential check. On success the server sets an HttpOnly
 * session cookie; the returned username is persisted only so the shell can display
 * it. A failure message is deliberately generic, matching the server, so the form
 * cannot be used to enumerate accounts.
 */
export async function signIn(
  username: string,
  password: string,
): Promise<void> {
  await withAuthOperationLock(async () => {
    const operationEpoch = getAuthEpoch();
    const { caliberApi } = await import("@/api/caliberApi");
    const result = await caliberApi.login(username.trim(), password);
    if (getAuthEpoch() !== operationEpoch) {
      await reconcileLocalAuthSession(createLocalAuthSession(result.user_id));
    } else {
      saveLocalAuthSession(createLocalAuthSession(result.user_id));
    }
  });
}

/** Revoke the session server-side, then clear local display state. */
export async function signOut(): Promise<void> {
  await withAuthOperationLock(async () => {
    const operationEpoch = getAuthEpoch();
    const signingOutSession = getStoredAuthSession();
    const signingOutGeneration = signingOutSession?.generation ?? null;
    let serverCompleted = false;
    try {
      const { caliberApi } = await import("@/api/caliberApi");
      await caliberApi.logout();
      serverCompleted = true;
    } catch {
      // A network/500 response is an ambiguous revoke, not proof of logout. The
      // HttpOnly cookie may still authenticate this browser, so reconcile it and keep
      // the prior identity if even that probe is unavailable. Falsely showing the
      // login screen on a shared device would leave a live server session hidden behind
      // local display state.
      await reconcileLocalAuthSession(signingOutSession);
      return;
    }
    // Inside the origin-wide lock, no newer login can apply a Set-Cookie response or
    // display generation between the server response and this clear. The generation
    // comparison still protects a newer state installed by a non-cooperating legacy
    // tab when the logout request failed before completing.
    if (serverCompleted && getAuthEpoch() !== operationEpoch) {
      await reconcileLocalAuthSession(null);
    } else if (
      serverCompleted ||
      getStoredAuthSession()?.generation === signingOutGeneration
    ) {
      clearLocalAuthSession();
    }
  });
}
