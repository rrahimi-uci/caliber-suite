/**
 * SPA entry point.
 *
 * Two prefix-aware bits to be aware of:
 *
 * 1. **`basename`** for the router: when MLflow runs behind
 *    `MLFLOW_STATIC_PREFIX=/mlflow`, CALIBER lives at `/mlflow/caliber/`.
 *    The router needs `basename="/mlflow/caliber"` so app links like
 *    `<Link to="/prompts">` resolve correctly. The hosting backend stamps the prefix into
 *    `window.__CALIBER_STATIC_PREFIX__` at serve time; locally it
 *    falls back to `""`.
 *
 * 2. **`window.__CALIBER_STATIC_PREFIX__`** is consumed by the API
 *    client to compute the `/ajax-api/...` URL. The two stay in sync
 *    via the same global so a single env-var bump (the static prefix)
 *    is enough to redeploy CALIBER behind any subpath.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { Toaster } from "sonner";

import { bootstrapCsrf } from "./api/caliberApi";
import { queryClient } from "./lib/queryClient";
import { TooltipProvider } from "./components/ui/tooltip";
import { App } from "./App";
import "./styles/index.css";

const PREFIX = (typeof window !== "undefined" && window.__CALIBER_STATIC_PREFIX__) || "";
const ROUTER_BASENAME = `${PREFIX}/caliber`;

const container = document.getElementById("root");
if (!container) {
  throw new Error("root element not found");
}

// Bootstrap CSRF before mounting. The `/csrf` endpoint reports
// `enabled=false` in the common deployment shape, so this is a fast
// no-op for most environments. When CSRF *is* enabled the cached
// token gets attached to every write request the SPA fires next.
// Failures are non-fatal — the SPA still mounts; writes that need
// the header will get a 403 and surface it through `ApiError`.
void bootstrapCsrf().catch((err: unknown) => {
  console.warn("caliber: csrf bootstrap failed; writes may 403 if csrf is enabled", err);
});

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <BrowserRouter
          basename={ROUTER_BASENAME}
        >
          <App />
          <Toaster position="bottom-right" richColors closeButton />
        </BrowserRouter>
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
