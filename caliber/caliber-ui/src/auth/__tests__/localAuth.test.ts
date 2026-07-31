import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  login: vi.fn(),
  logout: vi.fn(),
  getAuthSessionForReconciliation: vi.fn(),
}));

vi.mock("@/api/caliberApi", () => ({ caliberApi: apiMocks }));

import {
  createLocalAuthSession,
  getStoredAuthSession,
  saveLocalAuthSession,
  signIn,
  signOut,
} from "@/auth/localAuth";

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function authenticatedSession(userId: string): {
  user_id: string;
  scopes: string[];
  is_admin: boolean;
  auth_mode: "session";
  authenticated_by: "session";
  login_required: false;
} {
  return {
    user_id: userId,
    scopes: [],
    is_admin: false,
    auth_mode: "session",
    authenticated_by: "session",
    login_required: false,
  };
}

function installSerialWebLock(): {
  request: ReturnType<typeof vi.fn>;
  maxActive: () => number;
} {
  let tail = Promise.resolve();
  let active = 0;
  let maxActive = 0;
  const request = vi.fn(
    <T>(
      _name: string,
      _options: LockOptions,
      callback: () => Promise<T>,
    ): Promise<T> => {
      const run = tail.then(async () => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        try {
          return await callback();
        } finally {
          active -= 1;
        }
      });
      tail = run.then(
        () => undefined,
        () => undefined,
      );
      return run;
    },
  );
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: { request },
  });
  return { request, maxActive: () => maxActive };
}

beforeEach(() => {
  window.localStorage.clear();
  apiMocks.login.mockReset();
  apiMocks.logout.mockReset();
  apiMocks.getAuthSessionForReconciliation.mockReset();
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "locks");
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("local auth cookie-operation coordination", () => {
  it("serializes successful cookie mutations through one origin-wide Web Lock", async () => {
    const lock = installSerialWebLock();
    const loginA = deferred<{ user_id: string; expires_at: string }>();
    const loginB = deferred<{ user_id: string; expires_at: string }>();
    apiMocks.login.mockImplementation((userId: string) =>
      userId === "user-a" ? loginA.promise : loginB.promise,
    );

    const first = signIn("user-a", "password-a");
    const second = signIn("user-b", "password-b");
    await vi.waitFor(() => expect(apiMocks.login).toHaveBeenCalledTimes(1));

    loginA.resolve({
      user_id: "user-a",
      expires_at: "2026-08-01T00:00:00Z",
    });
    await vi.waitFor(() => expect(apiMocks.login).toHaveBeenCalledTimes(2));
    loginB.resolve({
      user_id: "user-b",
      expires_at: "2026-08-01T00:00:00Z",
    });
    await Promise.all([first, second]);

    expect(lock.request).toHaveBeenCalledTimes(2);
    expect(
      lock.request.mock.calls.every(
        ([name, options]) =>
          name === "caliber.auth.cookie-operation" &&
          (options as LockOptions).mode === "exclusive",
      ),
    ).toBe(true);
    expect(lock.maxActive()).toBe(1);
    expect(getStoredAuthSession()?.username).toBe("user-b");
  });

  it("does not retry a rejected auth operation when the Web Lock callback fails", async () => {
    const lock = installSerialWebLock();
    apiMocks.login.mockRejectedValue(new Error("invalid credentials"));

    await expect(signIn("ghost", "wrong-password")).rejects.toThrow(
      "invalid credentials",
    );

    expect(lock.request).toHaveBeenCalledTimes(1);
    expect(apiMocks.login).toHaveBeenCalledTimes(1);
  });

  it("falls back safely when a hardened context denies Web Locks access", async () => {
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      get: () => {
        throw new DOMException("denied", "SecurityError");
      },
    });
    apiMocks.login.mockResolvedValue({
      user_id: "user-a",
      expires_at: "2026-08-01T00:00:00Z",
    });

    await expect(signIn("user-a", "password-a")).resolves.toBeUndefined();

    expect(apiMocks.login).toHaveBeenCalledTimes(1);
    expect(getStoredAuthSession()?.username).toBe("user-a");
  });

  it("reconciles a fallback login race to the authoritative cookie identity", async () => {
    const login = deferred<{ user_id: string; expires_at: string }>();
    apiMocks.login.mockReturnValue(login.promise);
    apiMocks.getAuthSessionForReconciliation.mockResolvedValue(
      authenticatedSession("user-b"),
    );

    const signingIn = signIn("user-b", "password-b");
    await vi.waitFor(() => expect(apiMocks.login).toHaveBeenCalledTimes(1));
    // Simulate another realm completing user-a while user-b's Set-Cookie response is
    // pending. The fallback cannot lock that realm, so it must probe the cookie.
    saveLocalAuthSession(createLocalAuthSession("user-a"));
    login.resolve({
      user_id: "user-b",
      expires_at: "2026-08-01T00:00:00Z",
    });

    await expect(signingIn).resolves.toBeUndefined();
    expect(apiMocks.getAuthSessionForReconciliation).toHaveBeenCalledTimes(1);
    expect(getStoredAuthSession()?.username).toBe("user-b");
  });

  it("reconciles a fallback logout race instead of erasing a newer winning login", async () => {
    const logout = deferred<{ revoked: boolean }>();
    saveLocalAuthSession(createLocalAuthSession("user-a"));
    apiMocks.logout.mockReturnValue(logout.promise);
    apiMocks.getAuthSessionForReconciliation.mockResolvedValue(
      authenticatedSession("user-b"),
    );

    const signingOut = signOut();
    await vi.waitFor(() => expect(apiMocks.logout).toHaveBeenCalledTimes(1));
    saveLocalAuthSession(createLocalAuthSession("user-b"));
    logout.resolve({ revoked: true });

    await signingOut;
    expect(apiMocks.getAuthSessionForReconciliation).toHaveBeenCalledTimes(1);
    expect(getStoredAuthSession()?.username).toBe("user-b");
  });

  it("does not claim logout when revocation fails and the cookie is still authenticated", async () => {
    const prior = createLocalAuthSession("user-a");
    saveLocalAuthSession(prior);
    apiMocks.logout.mockRejectedValue(new Error("network unavailable"));
    apiMocks.getAuthSessionForReconciliation.mockResolvedValue(
      authenticatedSession("user-a"),
    );

    await expect(signOut()).resolves.toBeUndefined();

    expect(apiMocks.getAuthSessionForReconciliation).toHaveBeenCalledTimes(1);
    expect(getStoredAuthSession()).toEqual(prior);
  });

  it("preserves the prior identity when both revoke and reconciliation are unavailable", async () => {
    const prior = createLocalAuthSession("user-a");
    saveLocalAuthSession(prior);
    apiMocks.logout.mockRejectedValue(new Error("network unavailable"));
    apiMocks.getAuthSessionForReconciliation.mockRejectedValue(
      new Error("session probe unavailable"),
    );

    await expect(signOut()).resolves.toBeUndefined();

    expect(getStoredAuthSession()).toEqual(prior);
  });
});
