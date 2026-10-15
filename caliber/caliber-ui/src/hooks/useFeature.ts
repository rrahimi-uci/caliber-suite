/**
 * Alpha feature flags, as the server reports them.
 *
 * Four areas the alpha does not stand behind (the Aria assistant and plans, MCP servers and
 * OpenAPI integrations, cookbooks, and release-candidate signoff) are off by default and the
 * API refuses them with a 404 ``feature_disabled`` until a deployment turns them on.
 * ``GET /capabilities`` reports which are on under ``features``; this hook reads it.
 *
 * The rule that makes the clean alpha clean: **an area is on only when the server said so**.
 * While capabilities load, if the request fails, or if the response has no ``features`` (an
 * older server), every area reads as off. The cost of that rule is a beat of empty chrome on
 * a deployment that enabled something; the cost of the opposite rule is unsupported UI flashing
 * on every default install, and calls to routes that answer 404.
 *
 * Shares the ``["capabilities"]`` query with the workflow pages, so the whole app makes one
 * request.
 */

import { useMemo } from "react";

import { caliberApi } from "@/api/caliberApi";
import type {
  FeatureId,
  FeatureInfo,
  PlatformCapabilities,
} from "@/api/workflowTypes";
import { useApiQuery } from "@/hooks/useApiQuery";

/**
 * What the UI says about an area when the server did not (capabilities still loading, failed,
 * or from a server that predates the flags). Mirrors ``caliber/features.py`` so the
 * "disabled" panel can still name the variable to set.
 */
export const FEATURE_FALLBACKS: Readonly<Record<FeatureId, FeatureInfo>> = {
  aria: {
    enabled: false,
    env_var: "CALIBER_FEATURE_ARIA",
    label: "Aria assistant and plans",
    stability: "preview",
  },
  integrations: {
    enabled: false,
    env_var: "CALIBER_FEATURE_INTEGRATIONS",
    label: "MCP servers and OpenAPI integrations",
    stability: "preview",
  },
  cookbooks: {
    enabled: false,
    env_var: "CALIBER_FEATURE_COOKBOOKS",
    label: "Cookbooks",
    stability: "preview",
  },
  release_candidates: {
    enabled: false,
    env_var: "CALIBER_FEATURE_RELEASE_CANDIDATES",
    label: "Release-candidate signoff and workspace releases",
    stability: "record_only",
  },
};

export interface FeatureState {
  /** True only when the server reported this area as enabled. */
  enabled: boolean;
  /** True until capabilities have either loaded or failed. Always read as disabled meanwhile. */
  loading: boolean;
  /** The server's description of the area, or the built-in fallback (always ``enabled: false``). */
  info: FeatureInfo;
}

export interface FeatureFlags {
  loading: boolean;
  isEnabled: (id: FeatureId) => boolean;
  info: (id: FeatureId) => FeatureInfo;
}

/** Read the flags once and look several up (the sidebar needs all four). */
export function useFeatureFlags(): FeatureFlags {
  const query = useApiQuery<PlatformCapabilities>(["capabilities"], (signal) =>
    caliberApi.getCapabilities(signal),
  );
  const features = query.data?.features;
  const loading = query.isPending;
  // Memoised on what the server said, so a consumer can safely list ``isEnabled`` in an
  // effect's dependencies without re-running it on every render.
  return useMemo<FeatureFlags>(() => {
    const reported = (id: FeatureId): FeatureInfo | null => {
      const entry = features?.[id];
      return entry && typeof entry === "object" ? entry : null;
    };
    return {
      loading,
      isEnabled: (id) => reported(id)?.enabled === true,
      info: (id) => {
        const entry = reported(id);
        return entry
          ? { ...FEATURE_FALLBACKS[id], ...entry }
          : FEATURE_FALLBACKS[id];
      },
    };
  }, [features, loading]);
}

/** Whether one area is on: ``{ enabled, loading, info }``. */
export function useFeature(id: FeatureId): FeatureState {
  const flags = useFeatureFlags();
  return {
    enabled: flags.isEnabled(id),
    loading: flags.loading,
    info: flags.info(id),
  };
}
