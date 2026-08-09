/** Discoverable, installable built-in examples backed by the server catalog. */

import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type { CookbookReadinessCheck, CookbookRecipe } from "@/api/workflowTypes";
import { PageHeader } from "@/components/PageHeader";
import { SearchInput } from "@/components/SearchInput";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";

function defaultName(recipe: CookbookRecipe): string {
  return `Cookbook ${recipe.id} — ${recipe.title}`;
}

/**
 * The checks standing between this recipe and a clean install.
 *
 * The server computes these and ships them with the catalog; before this they
 * were fetched and dropped, leaving "Setup needed" as a badge that named no
 * cause and offered no way out.
 */
function unmetChecks(recipe: CookbookRecipe): CookbookReadinessCheck[] {
  return recipe.readiness.checks.filter((check) => check.status !== "ready");
}

/** One unmet check, with the route that fixes it when the server named one. */
function ReadinessCheckItem({ check }: { check: CookbookReadinessCheck }): JSX.Element {
  return (
    <li className="flex items-start gap-2 text-xs text-amber-800">
      <span aria-hidden="true" className="mt-0.5 text-amber-500">
        •
      </span>
      <span className="flex-1">
        {check.label}
        {check.settings_path && (
          <>
            {" "}
            <Link
              to={check.settings_path}
              className="font-semibold text-caliber-purple underline underline-offset-2"
            >
              Configure
            </Link>
          </>
        )}
      </span>
    </li>
  );
}

export function Cookbooks(): JSX.Element {
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<CookbookRecipe | null>(null);
  const [name, setName] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const query = useApiQuery(["cookbooks"], (signal) =>
    caliberApi.listCookbooks(signal),
  );
  const install = useApiMutation(
    async (recipe: CookbookRecipe) =>
      caliberApi.installCookbook(recipe.id, {
        name: name.trim() || defaultName(recipe),
        acknowledge_prerequisites:
          recipe.prerequisites.length === 0 || acknowledged,
      }),
    {
      onSuccess: async (result) => {
        await invalidate(["workflows"]);
        showToast.success(`Installed ${result.recipe.title} as a draft`);
        setSelected(null);
        navigate(
          `/workflows/${result.workflow.workflow_id}/editor/${result.version.version_id}`,
        );
      },
      onError: (error: Error) => showToast.error(error.message),
    },
  );

  const recipes = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return query.data?.recipes ?? [];
    return (query.data?.recipes ?? []).filter((recipe) =>
      [
        recipe.id,
        recipe.title,
        recipe.summary,
        ...recipe.capabilities,
      ].some((value) => value.toLowerCase().includes(needle)),
    );
  }, [query.data, search]);

  const choose = (recipe: CookbookRecipe): void => {
    setSelected(recipe);
    setName(defaultName(recipe));
    setAcknowledged(false);
  };

  return (
    <div className="space-y-6 animate-fade-in" data-testid="cookbook-catalog">
      <PageHeader
        title="Cookbooks"
        subtitle="Install a governed, editable example—then review its bindings before activation"
      />

      <div className="rounded-2xl border border-slate-200/60 bg-white p-4 shadow-card">
        <SearchInput
          value={search}
          onChange={setSearch}
          ariaLabel="Search Cookbooks"
          placeholder="Search examples and capabilities…"
          className="w-full sm:max-w-md"
        />
      </div>

      {query.isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[1, 2, 3, 4, 5, 6].map((item) => (
            <div key={item} className="h-60 rounded-2xl bg-white shadow-card shimmer" />
          ))}
        </div>
      )}
      {query.error && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          Failed to load Cookbooks: {query.error.message}
        </div>
      )}
      {query.data && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {recipes.map((recipe) => (
            <article
              key={recipe.id}
              data-testid={`cookbook-card-${recipe.id}`}
              className="flex min-h-64 flex-col rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-3">
                  <span className="grid h-11 w-11 place-items-center rounded-xl bg-violet-50 text-2xl">
                    {recipe.icon}
                  </span>
                  <div>
                    <div className="text-[10px] font-bold uppercase tracking-widest text-caliber-purple">
                      Cookbook {recipe.id}
                    </div>
                    <h2 className="text-sm font-bold text-slate-900">{recipe.title}</h2>
                  </div>
                </div>
                <span
                  className={`rounded-full px-2 py-1 text-[10px] font-semibold ${
                    recipe.readiness.status === "ready"
                      ? "bg-emerald-50 text-emerald-700"
                      : "bg-amber-50 text-amber-700"
                  }`}
                >
                  {recipe.readiness.status === "ready" ? "Ready" : "Setup needed"}
                </span>
              </div>
              <p className="mt-4 text-xs leading-5 text-slate-500">{recipe.summary}</p>
              <div className="mt-4 flex flex-wrap gap-1.5">
                {recipe.capabilities.map((capability) => (
                  <span
                    key={capability}
                    className="rounded-md bg-slate-50 px-2 py-1 text-[10px] font-medium text-slate-600"
                  >
                    {capability}
                  </span>
                ))}
              </div>
              {unmetChecks(recipe).length > 0 && (
                <div
                  className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3"
                  data-testid={`cookbook-readiness-${recipe.id}`}
                >
                  <ul className="space-y-1">
                    {unmetChecks(recipe).slice(0, 2).map((check) => (
                      <ReadinessCheckItem key={check.label} check={check} />
                    ))}
                  </ul>
                  {unmetChecks(recipe).length > 2 && (
                    <div className="mt-1 pl-4 text-[10px] font-medium text-amber-700">
                      +{unmetChecks(recipe).length - 2} more before this is ready
                    </div>
                  )}
                </div>
              )}
              <button
                type="button"
                data-testid={`install-cookbook-${recipe.id}`}
                className="btn-primary mt-auto w-full"
                onClick={() => choose(recipe)}
              >
                Review & install draft
              </button>
            </article>
          ))}
        </div>
      )}

      {selected && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-labelledby="cookbook-install-title"
        >
          <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <span className="text-3xl">{selected.icon}</span>
              <div>
                <div className="text-[10px] font-bold uppercase tracking-widest text-caliber-purple">
                  Cookbook {selected.id}
                </div>
                <h2 id="cookbook-install-title" className="text-lg font-bold text-slate-900">
                  Install {selected.title}
                </h2>
              </div>
            </div>
            <p className="mt-4 text-sm text-slate-600">
              CALIBER will create one paused workflow and one editable draft. Nothing is
              published, deployed, or invoked by this action.
            </p>
            <label className="mt-5 block text-xs font-semibold text-slate-700">
              Workflow name
              <input
                data-testid="cookbook-install-name"
                className="form-input mt-2 w-full"
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            {/*
              Rendered when EITHER condition holds, not just when there are unmet
              checks. The acknowledgement gates the install button, so hiding it
              behind an empty checks list would strand the operator in a modal
              whose primary action can never enable.
            */}
            {(unmetChecks(selected).length > 0 || selected.prerequisites.length > 0) && (
              <div
                className="mt-5 rounded-xl border border-amber-200 bg-amber-50 p-4"
                data-testid="cookbook-install-readiness"
              >
                {unmetChecks(selected).length > 0 && (
                  <>
                    <div className="text-xs font-bold text-amber-900">
                      Before this recipe will run end to end
                    </div>
                    <ul className="mt-2 space-y-1.5">
                      {unmetChecks(selected).map((check) => (
                        <ReadinessCheckItem key={check.label} check={check} />
                      ))}
                    </ul>
                  </>
                )}
                {selected.prerequisites.length > 0 && (
                  <label className="mt-3 flex items-start gap-2 text-xs text-amber-900">
                    <input
                      type="checkbox"
                      data-testid="cookbook-prerequisites-ack"
                      checked={acknowledged}
                      onChange={(event) => setAcknowledged(event.target.checked)}
                    />
                    I reviewed these prerequisites and understand they may still need
                    configuration.
                  </label>
                )}
              </div>
            )}
            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                className="btn-secondary"
                disabled={install.isPending}
                onClick={() => setSelected(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                data-testid="confirm-cookbook-install"
                className="btn-primary"
                disabled={
                  install.isPending ||
                  !name.trim() ||
                  (selected.prerequisites.length > 0 && !acknowledged)
                }
                onClick={() => install.mutate(selected)}
              >
                {install.isPending ? "Installing…" : "Install paused draft"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
