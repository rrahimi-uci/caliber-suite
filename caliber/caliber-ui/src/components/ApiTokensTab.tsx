/**
 * API tokens: issue, list, rotate and revoke the signed-in user's personal access tokens.
 *
 * Tokens are the credential for the SDK and the `caliberctl` CLI, and the CLI cannot
 * issue the first one (it needs a token to run `token create`). This page is the on-ramp
 * (alpha gap P1-07).
 *
 * The secret is shown exactly once, in the response to create or rotate, and lives only
 * in this component's state until the user dismisses it. It is never written to the query
 * cache, to storage, or to the URL, and the list endpoint does not carry it.
 */

import { Check, Copy, KeyRound, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type {
  IssuedPersonalAccessToken,
  PersonalAccessToken,
} from "@/api/types";
import type { Project } from "@/api/workflowTypes";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";

const TOKENS_KEY = ["auth", "tokens"] as const;
const ME_KEY = ["auth", "me"] as const;
const PROJECTS_KEY = ["projects", "access"] as const;

/** Expiry choices. Null means the token never expires. */
const EXPIRY_OPTIONS: Array<{ label: string; days: number | null }> = [
  { label: "30 days", days: 30 },
  { label: "90 days", days: 90 },
  { label: "1 year", days: 365 },
  { label: "No expiry", days: null },
];

function formatWhen(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

function expiryIso(days: number | null): string | undefined {
  if (days === null) return undefined;
  return new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString();
}

/** A bound token's workspace as its name when this user can see it, else the raw id. */
function workspaceLabel(
  projectId: string,
  projects: Project[] | undefined,
): string {
  const match = projects?.find((project) => project.project_id === projectId);
  return match ? `${match.name} (${projectId})` : projectId;
}

function tokenStatus(token: PersonalAccessToken): string {
  if (!token.active)
    return token.revoked_reason === "rotated" ? "Rotated" : "Revoked";
  if (token.expires_at && new Date(token.expires_at).getTime() < Date.now())
    return "Expired";
  return "Active";
}

export function ApiTokensTab(): JSX.Element {
  const invalidate = useInvalidate();
  const me = useApiQuery(ME_KEY, (signal) => caliberApi.getMe(signal));
  const tokens = useApiQuery<PersonalAccessToken[]>(TOKENS_KEY, (signal) =>
    caliberApi.listAccessTokens(signal),
  );
  // The workspaces this user can act in. A failed load only removes the choices: the
  // token can still be created unbound, so the form never waits on this list.
  const projects = useApiQuery<Project[]>(PROJECTS_KEY, (signal) =>
    caliberApi.listProjects("active", signal),
  );

  const [name, setName] = useState("");
  // Empty means an unbound token, which acts in whichever workspace a request names.
  const [projectId, setProjectId] = useState("");
  const [days, setDays] = useState<number | null>(30);
  const [selected, setSelected] = useState<Set<string>>(
    new Set(["caliber.viewer"]),
  );
  const [formError, setFormError] = useState<string | null>(null);
  // The plaintext of the most recently issued token. Cleared on dismiss.
  const [issued, setIssued] = useState<IssuedPersonalAccessToken | null>(null);
  const [copied, setCopied] = useState(false);
  const [pendingRevoke, setPendingRevoke] =
    useState<PersonalAccessToken | null>(null);

  const heldScopes = [...(me.data?.scopes ?? [])].sort();

  const create = useApiMutation(
    (payload: {
      name: string;
      scopes: string[];
      expires_at?: string;
      project_id?: string;
    }) => caliberApi.createAccessToken(payload),
    {
      onSuccess(token) {
        setIssued(token);
        setCopied(false);
        setName("");
        setFormError(null);
        void invalidate(TOKENS_KEY);
      },
      // The server's refusal is the message: it names the scopes the caller does not hold.
      onError(error) {
        setFormError(error.message || "Could not create the token.");
      },
    },
  );

  const rotate = useApiMutation(
    (tokenId: string) => caliberApi.rotateAccessToken(tokenId),
    {
      onSuccess(token) {
        setIssued(token);
        setCopied(false);
        void invalidate(TOKENS_KEY);
      },
      onError(error) {
        showToast.error(error.message || "Could not rotate the token.");
      },
    },
  );

  const revoke = useApiMutation(
    (tokenId: string) => caliberApi.revokeAccessToken(tokenId),
    {
      onSuccess() {
        setPendingRevoke(null);
        void invalidate(TOKENS_KEY);
        showToast.success("Token revoked");
      },
      onError(error) {
        setPendingRevoke(null);
        showToast.error(error.message || "Could not revoke the token.");
      },
    },
  );

  function submit(event: React.FormEvent): void {
    event.preventDefault();
    setFormError(null);
    const trimmed = name.trim();
    if (!trimmed) {
      setFormError("Give the token a name so you can recognise it later.");
      return;
    }
    if (selected.size === 0) {
      setFormError("Choose at least one scope.");
      return;
    }
    create.mutate({
      name: trimmed,
      scopes: [...selected].sort(),
      ...(days === null ? {} : { expires_at: expiryIso(days) }),
      ...(projectId ? { project_id: projectId } : {}),
    });
  }

  function toggleScope(scope: string): void {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(scope)) next.delete(scope);
      else next.add(scope);
      return next;
    });
  }

  async function copySecret(): Promise<void> {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.token);
      setCopied(true);
    } catch {
      showToast.error("Could not copy. Select the token and copy it by hand.");
    }
  }

  return (
    <div className="space-y-6" data-testid="api-tokens-tab">
      <section className="card p-6 space-y-4">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold text-slate-900">
            <KeyRound className="h-4 w-4" aria-hidden="true" /> API tokens
          </h2>
          <p className="mt-1 text-sm text-slate-600">
            A token lets the Python SDK and{" "}
            <code className="font-mono">caliberctl</code> act as you. It can
            never do more than you can, and the scopes you choose are a ceiling,
            not a grant. The secret is shown once, when you create it.
          </p>
        </div>

        {issued && (
          <div
            role="status"
            data-testid="api-token-secret"
            className="rounded-lg border border-emerald-300 bg-emerald-50 p-4 space-y-2"
          >
            <p className="text-sm font-semibold text-emerald-900">
              Copy this token now. It will not be shown again.
            </p>
            <div className="flex items-center gap-2">
              <code
                data-testid="api-token-secret-value"
                className="flex-1 break-all rounded bg-white px-3 py-2 font-mono text-xs text-slate-900 border border-emerald-200"
              >
                {issued.token}
              </code>
              <button
                type="button"
                onClick={() => void copySecret()}
                className="inline-flex items-center gap-1 rounded-md border border-emerald-300 bg-white px-3 py-2 text-xs font-medium text-emerald-800 hover:bg-emerald-100"
              >
                {copied ? (
                  <Check className="h-3.5 w-3.5" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <p className="text-xs text-emerald-900">
              Use it as <code className="font-mono">CALIBER_TOKEN</code> or pass{" "}
              <code className="font-mono">--token</code> to{" "}
              <code className="font-mono">caliberctl</code>.
            </p>
            <button
              type="button"
              onClick={() => setIssued(null)}
              className="rounded-md px-3 py-1.5 text-xs font-medium text-emerald-900 underline"
            >
              I have copied it
            </button>
          </div>
        )}

        <form
          onSubmit={submit}
          className="grid gap-4 md:grid-cols-2"
          aria-label="Create an API token"
        >
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-slate-700">Name</span>
            <input
              aria-label="Token name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="ci-automation"
              maxLength={120}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-slate-700">
              Expires
            </span>
            <select
              aria-label="Token expiry"
              value={days === null ? "never" : String(days)}
              onChange={(event) =>
                setDays(
                  event.target.value === "never"
                    ? null
                    : Number(event.target.value),
                )
              }
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              {EXPIRY_OPTIONS.map((option) => (
                <option
                  key={option.label}
                  value={option.days === null ? "never" : option.days}
                >
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm md:col-span-2">
            <span className="mb-1 block font-medium text-slate-700">
              Workspace
            </span>
            <select
              aria-label="Token workspace"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              <option value="">Any workspace (not bound)</option>
              {(projects.data ?? []).map((project) => (
                <option key={project.project_id} value={project.project_id}>
                  {project.name} ({project.access_role ?? "member"})
                </option>
              ))}
            </select>
            <span className="mt-1 block text-xs text-slate-500">
              A bound token is refused (403) for any other workspace, and a
              request that names none acts in the bound one. Bind CI tokens to
              the one workspace they need.
            </span>
          </label>
          <fieldset className="md:col-span-2">
            <legend className="mb-1 text-sm font-medium text-slate-700">
              Scopes
            </legend>
            <div className="flex flex-wrap gap-4">
              {heldScopes.map((scope) => (
                <label
                  key={scope}
                  className="inline-flex items-center gap-2 text-sm"
                >
                  <input
                    type="checkbox"
                    aria-label={scope}
                    checked={selected.has(scope)}
                    onChange={() => toggleScope(scope)}
                  />
                  <code className="font-mono text-xs">{scope}</code>
                </label>
              ))}
            </div>
            <p className="mt-1 text-xs text-slate-500">
              Only the scopes you hold are offered. Choose the least a job
              needs: a read-only report needs{" "}
              <code className="font-mono">caliber.viewer</code>.
            </p>
          </fieldset>
          {formError && (
            <p
              role="alert"
              data-testid="api-token-error"
              className="md:col-span-2 text-sm text-red-700"
            >
              {formError}
            </p>
          )}
          <div className="md:col-span-2">
            <button
              type="submit"
              disabled={create.isPending}
              className="rounded-md bg-caliber-600 px-4 py-2 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-60"
            >
              {create.isPending ? "Creating…" : "Create token"}
            </button>
          </div>
        </form>
      </section>

      <section className="card p-6 space-y-3">
        <h3 className="text-sm font-semibold text-slate-800">Your tokens</h3>
        {tokens.isLoading ? (
          <p className="text-sm text-slate-500">Loading tokens…</p>
        ) : tokens.error ? (
          <p role="alert" className="text-sm text-red-700">
            Could not load your tokens: {tokens.error.message}
          </p>
        ) : (tokens.data ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">
            You have not created any tokens yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="api-token-table">
              <thead>
                <tr className="text-left text-xs uppercase text-slate-500">
                  <th className="py-2 pr-4">Name</th>
                  <th className="py-2 pr-4">Scopes</th>
                  <th className="py-2 pr-4">Workspace</th>
                  <th className="py-2 pr-4">Created</th>
                  <th className="py-2 pr-4">Last used</th>
                  <th className="py-2 pr-4">Expires</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody>
                {(tokens.data ?? []).map((token) => {
                  const status = tokenStatus(token);
                  return (
                    <tr
                      key={token.token_id}
                      data-testid={`api-token-row-${token.token_id}`}
                      className="border-t border-slate-100"
                    >
                      <td className="py-2 pr-4 font-medium text-slate-900">
                        {token.name}
                      </td>
                      <td className="py-2 pr-4">
                        <code className="font-mono text-xs">
                          {token.scopes.join(", ") || "—"}
                        </code>
                      </td>
                      <td className="py-2 pr-4">
                        {token.project_id ? (
                          <code className="font-mono text-xs">
                            {workspaceLabel(token.project_id, projects.data)}
                          </code>
                        ) : (
                          "Any"
                        )}
                      </td>
                      <td className="py-2 pr-4">
                        {formatWhen(token.created_at)}
                      </td>
                      <td className="py-2 pr-4">
                        {formatWhen(token.last_used_at)}
                      </td>
                      <td className="py-2 pr-4">
                        {token.expires_at
                          ? formatWhen(token.expires_at)
                          : "Never"}
                      </td>
                      <td className="py-2 pr-4">{status}</td>
                      <td className="py-2 text-right whitespace-nowrap">
                        {token.active && (
                          <>
                            <button
                              type="button"
                              aria-label={`Rotate ${token.name}`}
                              onClick={() => rotate.mutate(token.token_id)}
                              disabled={rotate.isPending}
                              className="mr-2 inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
                            >
                              <RefreshCw
                                className="h-3 w-3"
                                aria-hidden="true"
                              />{" "}
                              Rotate
                            </button>
                            <button
                              type="button"
                              aria-label={`Revoke ${token.name}`}
                              onClick={() => setPendingRevoke(token)}
                              className="inline-flex items-center gap-1 rounded-md border border-red-300 px-2 py-1 text-xs text-red-700 hover:bg-red-50"
                            >
                              <Trash2 className="h-3 w-3" aria-hidden="true" />{" "}
                              Revoke
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ConfirmDialog
        open={pendingRevoke !== null}
        title={pendingRevoke ? `Revoke "${pendingRevoke.name}"` : ""}
        description="Anything using this token stops working immediately. This cannot be undone."
        confirmLabel="Revoke token"
        destructive
        busy={revoke.isPending}
        onConfirm={() => {
          if (pendingRevoke) revoke.mutate(pendingRevoke.token_id);
        }}
        onCancel={() => setPendingRevoke(null)}
      />
    </div>
  );
}
