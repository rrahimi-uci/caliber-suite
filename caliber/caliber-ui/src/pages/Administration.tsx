/**
 * Administration — accounts and secrets.
 *
 * The identity and secret stores shipped as **API-only** surfaces, which is why the
 * report kept Platform UX and the end-to-end lifecycle scored down: a product whose
 * documented answer to "add a second user" is `curl` has a real hole in its low-code
 * claim, not a cosmetic one. This page closes that for the two stores an operator has
 * to touch to stand a deployment up.
 *
 * Two rules the UI enforces because the API does:
 *
 * - **A secret value is never displayed.** `GET /secrets` returns metadata only, and
 *   this page has no field to render a value into. Rotation is "write a new value",
 *   not "read then edit", so there is nothing to prefill.
 * - **A password is never echoed back.** Creation and reset are write-only; the form
 *   clears on success rather than leaving the credential sitting in a DOM node.
 *
 * Admin-scoped. Non-admins get a 403 from the API, which is surfaced inline rather
 * than as an empty table — "no accounts" and "you may not list accounts" are different
 * facts and must not look the same.
 */

import { useCallback, useState } from "react";
import { KeyRound, ShieldCheck, UserPlus, Users } from "lucide-react";

import { caliberApi } from "@/api/caliberApi";
import type { AuthAccountList, SecretList } from "@/api/types";
import type { Project, ProjectMember, ProjectRole } from "@/api/workflowTypes";
import { clearLocalAuthSession, getStoredAuthSession } from "@/auth/localAuth";
import { PageHeader } from "@/components/PageHeader";
import { useApiQuery } from "@/hooks/useApiQuery";
import { getActiveProjectId } from "@/workspace/activeWorkspace";

/** Minimum enforced server-side; mirrored here so the error arrives before the round trip. */
const MIN_PASSWORD_LENGTH = 12;

function formatWhen(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

export function Administration(): JSX.Element {
  const accounts = useApiQuery<AuthAccountList>(
    ["auth", "accounts"],
    (signal) => caliberApi.listAccounts(signal),
  );
  const secrets = useApiQuery<SecretList>(["secrets"], (signal) =>
    caliberApi.listSecrets(signal),
  );
  const projects = useApiQuery<Project[]>(["projects", "access"], (signal) =>
    caliberApi.listProjects("active", signal),
  );

  const [newUser, setNewUser] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [resetPasswords, setResetPasswords] = useState<Record<string, string>>(
    {},
  );
  const [accountError, setAccountError] = useState<string | null>(null);
  const [accountNotice, setAccountNotice] = useState<string | null>(null);

  const [secretName, setSecretName] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [secretError, setSecretError] = useState<string | null>(null);
  const [secretNotice, setSecretNotice] = useState<string | null>(null);

  const createAccount = useCallback(async () => {
    setAccountError(null);
    setAccountNotice(null);
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setAccountError(
        `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`,
      );
      return;
    }
    try {
      await caliberApi.createAccount(newUser.trim(), newPassword);
      setAccountNotice(`Created ${newUser.trim()}.`);
      // Cleared immediately: a credential must not linger in a form field.
      setNewUser("");
      setNewPassword("");
      await accounts.refetch();
    } catch (error) {
      setAccountError(
        error instanceof Error
          ? error.message
          : "Could not create the account.",
      );
    }
  }, [accounts, newPassword, newUser]);

  const toggleAccount = useCallback(
    async (userId: string, disabled: boolean) => {
      setAccountError(null);
      try {
        await caliberApi.updateAccount(userId, { disabled });
        await accounts.refetch();
      } catch (error) {
        setAccountError(
          error instanceof Error
            ? error.message
            : "Could not update the account.",
        );
      }
    },
    [accounts],
  );

  const resetPassword = useCallback(
    async (userId: string) => {
      const password = resetPasswords[userId] ?? "";
      setAccountError(null);
      setAccountNotice(null);
      if (password.length < MIN_PASSWORD_LENGTH) {
        setAccountError(
          `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`,
        );
        return;
      }
      try {
        await caliberApi.updateAccount(userId, { password });
        setResetPasswords((current) => ({ ...current, [userId]: "" }));
        const currentSession = getStoredAuthSession();
        const resetCurrentAccount = currentSession?.username === userId;
        if (resetCurrentAccount) {
          const notice = `Changed the password for ${userId}. Sign in again with the new password.`;
          setAccountNotice(notice);
          // The server revokes every session for the account, including this one.
          // Clear the shell's display state immediately instead of waiting for the
          // next API request to discover the revoked cookie. The event carries the
          // explanation to the login screen because this page unmounts immediately.
          clearLocalAuthSession(notice);
          return;
        }
        await accounts.refetch();
        setAccountNotice(
          `Changed the password for ${userId} and revoked all of that account's sessions.`,
        );
      } catch (error) {
        setAccountError(
          error instanceof Error
            ? error.message
            : "Could not reset the password.",
        );
      }
    },
    [accounts, resetPasswords],
  );

  const revokeSessions = useCallback(async (userId: string) => {
    setAccountError(null);
    setAccountNotice(null);
    try {
      const result = await caliberApi.revokeAccountSessions(userId);
      // Disabling an account does not by itself end sessions already issued to it,
      // so this is the control that actually logs someone out now.
      setAccountNotice(`Revoked ${result.revoked} session(s) for ${userId}.`);
    } catch (error) {
      setAccountError(
        error instanceof Error ? error.message : "Could not revoke sessions.",
      );
    }
  }, []);

  const putSecret = useCallback(async () => {
    setSecretError(null);
    setSecretNotice(null);
    try {
      const result = await caliberApi.putSecret(secretName.trim(), secretValue);
      setSecretNotice(
        `Stored ${result.name} as version ${result.version}. ` +
          `Reference it as ${secrets.data?.reference_scheme ?? "secret://"}${result.name}.`,
      );
      setSecretName("");
      setSecretValue("");
      await secrets.refetch();
    } catch (error) {
      setSecretError(
        error instanceof Error ? error.message : "Could not store the secret.",
      );
    }
  }, [secretName, secretValue, secrets]);

  const revokeSecret = useCallback(
    async (name: string) => {
      setSecretError(null);
      try {
        await caliberApi.revokeSecret(name);
        await secrets.refetch();
      } catch (error) {
        setSecretError(
          error instanceof Error
            ? error.message
            : "Could not revoke the secret.",
        );
      }
    },
    [secrets],
  );

  return (
    <div className="space-y-8">
      <PageHeader
        title="Administration"
        subtitle="Accounts and secrets for this deployment."
      />

      {/* ---------------------------------------------------------------- Accounts */}
      <section aria-labelledby="accounts-heading" className="space-y-3">
        <h2
          id="accounts-heading"
          className="flex items-center gap-2 text-lg font-semibold"
        >
          <ShieldCheck className="h-5 w-5" aria-hidden="true" />
          Accounts
        </h2>

        {accounts.isError ? (
          <p role="alert" className="text-sm text-red-400">
            {accounts.error?.message ?? "Could not load accounts."}
          </p>
        ) : null}
        {accountError ? (
          <p role="alert" className="text-sm text-red-400">
            {accountError}
          </p>
        ) : null}
        {accountNotice ? (
          <p role="status" className="text-sm text-emerald-400">
            {accountNotice}
          </p>
        ) : null}

        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            void createAccount();
          }}
        >
          <label className="flex flex-col text-sm">
            <span className="mb-1">User ID</span>
            <input
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
              value={newUser}
              onChange={(event) => setNewUser(event.target.value)}
              placeholder="@alice"
              required
            />
          </label>
          <label className="flex flex-col text-sm">
            <span className="mb-1">Password</span>
            <input
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              minLength={MIN_PASSWORD_LENGTH}
              required
            />
          </label>
          <button
            type="submit"
            className="flex items-center gap-1 rounded bg-cyan-700 px-3 py-1.5 text-sm"
          >
            <UserPlus className="h-4 w-4" aria-hidden="true" />
            Create account
          </button>
        </form>

        <table className="w-full text-left text-sm">
          <caption className="sr-only">Accounts in this deployment</caption>
          <thead>
            <tr className="text-slate-400">
              <th scope="col">User</th>
              <th scope="col">Status</th>
              <th scope="col">Last login</th>
              <th scope="col">Password set</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(accounts.data?.accounts ?? []).map((account) => (
              <tr key={account.user_id} className="border-t border-slate-800">
                <td className="py-1 font-mono">{account.user_id}</td>
                <td>{account.disabled ? "Disabled" : "Active"}</td>
                <td>{formatWhen(account.last_login_at)}</td>
                <td>{formatWhen(account.password_updated_at)}</td>
                <td className="py-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <label
                      className="sr-only"
                      htmlFor={`reset-password-${account.user_id}`}
                    >
                      New password for {account.user_id}
                    </label>
                    <input
                      id={`reset-password-${account.user_id}`}
                      className="w-48 rounded border border-slate-700 bg-slate-900 px-2 py-0.5"
                      type="password"
                      autoComplete="new-password"
                      placeholder="New password"
                      minLength={MIN_PASSWORD_LENGTH}
                      value={resetPasswords[account.user_id] ?? ""}
                      onChange={(event) =>
                        setResetPasswords((current) => ({
                          ...current,
                          [account.user_id]: event.target.value,
                        }))
                      }
                    />
                    <button
                      type="button"
                      className="rounded border border-slate-600 px-2 py-0.5"
                      onClick={() => void resetPassword(account.user_id)}
                    >
                      Reset password for {account.user_id}
                    </button>
                    <button
                      type="button"
                      className="rounded border border-slate-600 px-2 py-0.5"
                      onClick={() =>
                        void toggleAccount(account.user_id, !account.disabled)
                      }
                    >
                      {account.disabled ? "Enable" : "Disable"}
                    </button>
                    <button
                      type="button"
                      className="rounded border border-slate-600 px-2 py-0.5"
                      onClick={() => void revokeSessions(account.user_id)}
                    >
                      Revoke sessions
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!accounts.isLoading &&
        !accounts.isError &&
        (accounts.data?.accounts.length ?? 0) === 0 ? (
          <p className="text-sm text-slate-400">No accounts yet.</p>
        ) : null}
      </section>

      <ProjectAccessSection projects={projects.data ?? []} />

      {/* ----------------------------------------------------------------- Secrets */}
      <section aria-labelledby="secrets-heading" className="space-y-3">
        <h2
          id="secrets-heading"
          className="flex items-center gap-2 text-lg font-semibold"
        >
          <KeyRound className="h-5 w-5" aria-hidden="true" />
          Secrets
        </h2>

        {secrets.data && !secrets.data.enabled ? (
          <p role="status" className="text-sm text-amber-400">
            The encrypted store is disabled — set
            CALIBER_SECRET_ENCRYPTION_KEY_SOURCE. Until then a{" "}
            <code>secret://</code> reference resolves to nothing and its
            consumer fails closed rather than reading a plaintext fallback.
          </p>
        ) : null}
        {secrets.isError ? (
          <p role="alert" className="text-sm text-red-400">
            {secrets.error?.message ?? "Could not load secrets."}
          </p>
        ) : null}
        {secretError ? (
          <p role="alert" className="text-sm text-red-400">
            {secretError}
          </p>
        ) : null}
        {secretNotice ? (
          <p role="status" className="text-sm text-emerald-400">
            {secretNotice}
          </p>
        ) : null}

        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            void putSecret();
          }}
        >
          <label className="flex flex-col text-sm">
            <span className="mb-1">Name</span>
            <input
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
              value={secretName}
              onChange={(event) => setSecretName(event.target.value)}
              placeholder="stripe-key"
              required
            />
          </label>
          <label className="flex flex-col text-sm">
            <span className="mb-1">Value</span>
            <input
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
              type="password"
              value={secretValue}
              onChange={(event) => setSecretValue(event.target.value)}
              required
            />
          </label>
          <button
            type="submit"
            className="rounded bg-cyan-700 px-3 py-1.5 text-sm"
          >
            Store / rotate
          </button>
        </form>

        <table className="w-full text-left text-sm">
          <caption className="sr-only">
            Stored secrets — metadata only, never values
          </caption>
          <thead>
            <tr className="text-slate-400">
              <th scope="col">Name</th>
              <th scope="col">Version</th>
              <th scope="col">Versions</th>
              <th scope="col">State</th>
              <th scope="col">Updated</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(secrets.data?.secrets ?? []).map((secret) => (
              <tr key={secret.name} className="border-t border-slate-800">
                <td className="py-1 font-mono">{secret.name}</td>
                <td>{secret.current_version ?? "—"}</td>
                <td>{secret.versions}</td>
                <td>{secret.revoked ? "Revoked" : "Active"}</td>
                <td>{formatWhen(secret.updated_at)}</td>
                <td className="py-1">
                  {secret.revoked ? null : (
                    <button
                      type="button"
                      className="rounded border border-slate-600 px-2 py-0.5"
                      onClick={() => void revokeSecret(secret.name)}
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!secrets.isLoading &&
        !secrets.isError &&
        (secrets.data?.secrets.length ?? 0) === 0 ? (
          <p className="text-sm text-slate-400">No secrets stored yet.</p>
        ) : null}
      </section>
    </div>
  );
}

function ProjectAccessSection({ projects }: { projects: Project[] }): JSX.Element {
  const [selectedProjectId, setSelectedProjectId] = useState<string>(
    () => getActiveProjectId() ?? projects[0]?.project_id ?? "",
  );
  const [newUser, setNewUser] = useState("");
  const [newRole, setNewRole] = useState<ProjectRole>("viewer");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const projectId = selectedProjectId || projects[0]?.project_id || "";
  const project = projects.find((item) => item.project_id === projectId) ?? null;
  const members = useApiQuery(
    ["project-members", projectId],
    (signal) => caliberApi.listProjectMembers(projectId, signal),
    { enabled: Boolean(projectId) },
  );
  const canManage = project?.permissions?.includes("project.manage_members") ?? false;

  const addMember = async (): Promise<void> => {
    setError(null);
    setNotice(null);
    try {
      await caliberApi.addProjectMember(projectId, {
        user_id: newUser.trim(),
        role: newRole,
      });
      setNewUser("");
      setNotice(`Added ${newUser.trim()} to ${project?.name ?? "the project"}.`);
      await members.refetch();
    } catch (value) {
      setError(value instanceof Error ? value.message : "Could not add project member.");
    }
  };

  const changeRole = async (member: ProjectMember, role: ProjectRole): Promise<void> => {
    setError(null);
    try {
      await caliberApi.updateProjectMember(projectId, member.user_id, { role });
      await members.refetch();
    } catch (value) {
      setError(value instanceof Error ? value.message : "Could not update project member.");
    }
  };

  const removeMember = async (member: ProjectMember): Promise<void> => {
    setError(null);
    try {
      await caliberApi.removeProjectMember(projectId, member.user_id);
      setNotice(`Removed ${member.user_id} from the project.`);
      await members.refetch();
    } catch (value) {
      setError(value instanceof Error ? value.message : "Could not remove project member.");
    }
  };

  return (
    <section aria-labelledby="project-access-heading" className="space-y-3">
      <h2 id="project-access-heading" className="flex items-center gap-2 text-lg font-semibold">
        <Users className="h-5 w-5" aria-hidden="true" />
        Project access
      </h2>
      <p className="text-sm text-slate-400">
        Manage who can read, edit, review, and publish resources in each project.
      </p>
      {projects.length === 0 ? (
        <p className="text-sm text-slate-400">No active projects are available.</p>
      ) : (
        <>
          <label className="flex max-w-md flex-col text-sm">
            <span className="mb-1">Project</span>
            <select
              aria-label="Project for access management"
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5"
              value={projectId}
              onChange={(event) => setSelectedProjectId(event.target.value)}
            >
              {projects.map((item) => (
                <option key={item.project_id} value={item.project_id}>
                  {item.name} ({item.access_role ?? "member"})
                </option>
              ))}
            </select>
          </label>
          {error ? <p role="alert" className="text-sm text-red-400">{error}</p> : null}
          {notice ? <p role="status" className="text-sm text-emerald-400">{notice}</p> : null}
          {members.isError ? (
            <p role="alert" className="text-sm text-red-400">
              {members.error?.message ?? "Could not load project members."}
            </p>
          ) : null}
          {canManage ? (
            <form
              className="flex flex-wrap items-end gap-3"
              onSubmit={(event) => {
                event.preventDefault();
                void addMember();
              }}
            >
              <label className="flex flex-col text-sm">
                <span className="mb-1">User ID</span>
                <input
                  className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
                  value={newUser}
                  onChange={(event) => setNewUser(event.target.value)}
                  placeholder="@developer"
                  required
                />
              </label>
              <label className="flex flex-col text-sm">
                <span className="mb-1">Role</span>
                <select
                  className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
                  value={newRole}
                  onChange={(event) => setNewRole(event.target.value as ProjectRole)}
                >
                  <option value="viewer">Viewer</option>
                  <option value="editor">Editor</option>
                  <option value="reviewer">Reviewer</option>
                </select>
              </label>
              <button type="submit" className="rounded bg-cyan-700 px-3 py-1.5 text-sm">
                Add member
              </button>
            </form>
          ) : (
            <p className="text-sm text-slate-400">Only the project owner can manage members.</p>
          )}
          <div className="overflow-x-auto rounded border border-slate-800">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Project members</caption>
              <thead>
                <tr className="text-slate-400">
                  <th scope="col" className="px-3 py-2">User</th>
                  <th scope="col" className="px-3 py-2">Role</th>
                  {canManage ? <th scope="col" className="px-3 py-2">Actions</th> : null}
                </tr>
              </thead>
              <tbody>
                {(members.data?.members ?? []).map((member) => (
                  <tr key={member.member_id} className="border-t border-slate-800">
                    <td className="px-3 py-2 font-mono">{member.user_id}</td>
                    <td className="px-3 py-2">
                      {canManage && member.role !== "owner" ? (
                        <select
                          aria-label={`Role for ${member.user_id}`}
                          className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
                          value={member.role}
                          onChange={(event) => void changeRole(member, event.target.value as ProjectRole)}
                        >
                          <option value="viewer">Viewer</option>
                          <option value="editor">Editor</option>
                          <option value="reviewer">Reviewer</option>
                        </select>
                      ) : (
                        member.role
                      )}
                    </td>
                    {canManage ? (
                      <td className="px-3 py-2">
                        {member.role !== "owner" ? (
                          <button
                            type="button"
                            className="rounded border border-slate-600 px-2 py-0.5"
                            onClick={() => void removeMember(member)}
                          >
                            Remove
                          </button>
                        ) : null}
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
