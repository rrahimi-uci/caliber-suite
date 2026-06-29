import { useState, type FormEvent } from "react";

import {
  DEFAULT_LOGIN_PASSWORD,
  DEFAULT_LOGIN_USERNAME,
  createLocalAuthSession,
  isDefaultCredential,
  saveLocalAuthSession,
} from "@/auth/localAuth";
import { BrandAcronym } from "@/components/BrandAcronym";

interface LoginProps {
  onLogin: () => void;
}

/* ── Feature cards shown on the left panel ────────────────────────── */

interface Feature {
  icon: JSX.Element;
  title: string;
  desc: string;
  gradient: string;
}

const FEATURES: Feature[] = [
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-5 h-5">
        <circle cx="18" cy="5" r="3" />
        <circle cx="6" cy="12" r="3" />
        <circle cx="18" cy="19" r="3" />
        <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
        <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
      </svg>
    ),
    title: "Knowledge Bases",
    desc: "Ground agents in a governed, versioned knowledge layer — chunks, embeddings, and graph-aware GraphRAG retrieval over your continuously evolving corpus.",
    gradient: "from-cyan-500/10 to-cyan-600/5",
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-5 h-5">
        <rect x="2" y="3" width="20" height="6" rx="2" />
        <rect x="2" y="15" width="20" height="6" rx="2" />
        <path d="M6 6h.01M6 18h.01M12 9v6" />
      </svg>
    ),
    title: "MCP Servers",
    desc: "Connect external systems through MCP and expose governed tools and data to your agents.",
    gradient: "from-sky-500/10 to-sky-600/5",
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-5 h-5">
        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
      </svg>
    ),
    title: "Skills and Prompt Engineering",
    desc: "Version prompts and reusable skills, run evaluation pipelines, and promote better variants with confidence.",
    gradient: "from-violet-500/10 to-violet-600/5",
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-5 h-5">
        <rect x="3" y="3" width="6" height="6" rx="1.5" />
        <rect x="15" y="15" width="6" height="6" rx="1.5" />
        <path d="M9 6h6a3 3 0 013 3v6" />
      </svg>
    ),
    title: "Workflow Orchestration",
    desc: "Design multi-step agent workflows in a visual editor with runtime controls and calibration loops.",
    gradient: "from-blue-500/10 to-blue-600/5",
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-5 h-5">
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    ),
    title: "Verification & Calibration",
    desc: "Version prompts, skills, tools, and workflows, run verification and calibration loops, and apply better candidates with rollback-ready controls.",
    gradient: "from-emerald-500/10 to-emerald-600/5",
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-5 h-5">
        <path d="M3 3v18h18" />
        <path d="M7 12l4-4 4 4 6-6" />
      </svg>
    ),
    title: "Observability",
    desc: "Monitor every agent run with MLflow-native traces, metrics, and diagnostics for faster root-cause analysis.",
    gradient: "from-rose-500/10 to-rose-600/5",
  },
];

/* ── Login component ──────────────────────────────────────────────── */

export function Login({ onLogin }: LoginProps): JSX.Element {
  const [username, setUsername] = useState(DEFAULT_LOGIN_USERNAME);
  const [password, setPassword] = useState(DEFAULT_LOGIN_PASSWORD);
  const [error, setError] = useState<string | null>(null);
  const [showPassword, setShowPassword] = useState(false);
  const staticPrefix =
    (typeof window !== "undefined" && window.__CALIBER_STATIC_PREFIX__) || "";
  const logoSrc = `${staticPrefix}/caliber/caliber.png`;
  const iconSrc = `${staticPrefix}/caliber/caliber-icon.png`;

  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!isDefaultCredential(username, password)) {
      setError("Invalid username or password.");
      return;
    }
    saveLocalAuthSession(createLocalAuthSession(DEFAULT_LOGIN_USERNAME));
    onLogin();
  };

  return (
    <main className="min-h-screen bg-slate-950 text-white selection:bg-caliber-500/30">
      <div className="relative min-h-screen overflow-hidden">
        {/* ── Background effects ─────────────────────────────────── */}
        <div className="absolute inset-0">
          <div className="absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,rgba(124,58,237,0.25),transparent)]" />
          <div className="absolute bottom-0 left-0 right-0 h-1/2 bg-[radial-gradient(ellipse_60%_50%_at_50%_100%,rgba(59,130,246,0.12),transparent)]" />
          <div className="absolute inset-0 opacity-[0.03] [background-image:linear-gradient(rgba(255,255,255,0.1)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.1)_1px,transparent_1px)] [background-size:64px_64px]" />
        </div>

        {/* ── Centered two-column layout ─────────────────────────── */}
        <div className="relative z-10 mx-auto flex min-h-screen w-full max-w-6xl items-center gap-10 px-6 py-10 lg:px-10 xl:gap-16">
        {/* ── Left: brand + hero + features ──────────────────────── */}
        <section className="hidden flex-1 flex-col gap-8 lg:flex">
          {/* Brand */}
          <div className="flex items-center gap-3">
            <div className="relative">
              <div className="absolute -inset-1 rounded-xl bg-gradient-to-br from-caliber-500 to-blue-500 opacity-50 blur-lg" />
              <img
                src={iconSrc}
                alt="CALIBER"
                className="relative h-14 w-14 rounded-xl object-contain shadow-2xl shadow-caliber-500/30"
              />
            </div>
            <div>
              <p className="text-base font-bold tracking-wide text-white">CALIBER</p>
              <BrandAcronym className="mt-0.5 block max-w-md text-[12px] font-normal leading-snug text-slate-400" />
            </div>
          </div>

          {/* Hero */}
          <div className="max-w-xl">
            <h1 className="text-3xl font-bold leading-[1.15] tracking-tight text-white xl:text-4xl">
              Build Trusted Agentic Workflows with Verification and Calibration
            </h1>
            <p className="mt-4 max-w-md text-sm leading-6 text-slate-400">
              Design, evaluate, and refine prompts, tools, skills, and
              multi-agent workflows in a unified platform. Deliver versioned,
              governed, and production-ready AI systems with traceability and
              controlled promotion.
            </p>
          </div>

          {/* Feature grid */}
          <div className="grid max-w-xl grid-cols-3 auto-rows-fr gap-2.5">
            {FEATURES.map((f) => (
              <div
                key={f.title}
                className={`group flex h-full flex-col rounded-xl border border-white/[0.06] bg-gradient-to-br ${f.gradient} p-3.5 backdrop-blur-sm transition hover:border-white/10 hover:bg-white/[0.04]`}
              >
                <div className="mb-2.5 flex h-8 w-8 items-center justify-center rounded-lg bg-white/[0.07] text-slate-300 transition group-hover:text-white group-hover:bg-white/10">
                  {f.icon}
                </div>
                <p className="text-[13px] font-semibold text-slate-200">{f.title}</p>
                <p className="mt-1 text-[11px] leading-snug text-slate-500 transition-colors group-hover:text-slate-400">
                  {f.desc}
                </p>
              </div>
            ))}
          </div>
        </section>

        {/* ── Right: login form ──────────────────────────────────── */}
        <section className="relative flex w-full flex-col items-center lg:w-[400px] lg:flex-none">
          {/* Subtle glow behind form */}
          <div className="absolute right-[20%] top-[30%] h-72 w-72 rounded-full bg-caliber-600/10 blur-[128px]" />

          <div className="w-full max-w-md">
            {/* Mobile logo */}
            <div className="mb-8 flex justify-center lg:hidden">
              <img src={logoSrc} alt="CALIBER" className="h-16 object-contain brightness-0 invert" />
            </div>

            {/* Form card */}
            <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-8 shadow-2xl shadow-black/40 backdrop-blur-xl sm:p-10">
              <div className="mb-8">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.2em] text-caliber-400">
                    Welcome
                  </p>
                  <h2 className="mt-2 text-2xl font-bold text-white">Sign in</h2>
                  <p className="mt-1 text-sm text-slate-500">
                    Access your calibration workspace
                  </p>
                </div>
              </div>

              <form className="space-y-5" onSubmit={submit}>
                <label className="block">
                  <span className="mb-2 block text-xs font-semibold text-slate-400">Username</span>
                  <input
                    autoComplete="username"
                    className="w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-white placeholder:text-slate-600 outline-none transition focus:border-caliber-500/50 focus:bg-white/[0.07] focus:ring-2 focus:ring-caliber-500/20"
                    placeholder="Enter your username"
                    value={username}
                    onChange={(event) => {
                      setUsername(event.target.value);
                      setError(null);
                    }}
                  />
                </label>
                <label className="block">
                  <span className="mb-2 block text-xs font-semibold text-slate-400">Password</span>
                  <div className="relative">
                    <input
                      autoComplete="current-password"
                      type={showPassword ? "text" : "password"}
                      className="w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 pr-11 text-sm text-white placeholder:text-slate-600 outline-none transition focus:border-caliber-500/50 focus:bg-white/[0.07] focus:ring-2 focus:ring-caliber-500/20"
                      placeholder="Enter your password"
                      value={password}
                      onChange={(event) => {
                        setPassword(event.target.value);
                        setError(null);
                      }}
                    />
                    <button
                      type="button"
                      tabIndex={-1}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300 transition-colors"
                      onClick={() => setShowPassword((v) => !v)}
                      aria-label={showPassword ? "Hide password" : "Show password"}
                    >
                      {showPassword ? (
                        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                          <path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94" />
                          <path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19" />
                          <line x1="1" y1="1" x2="23" y2="23" />
                        </svg>
                      ) : (
                        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                          <circle cx="12" cy="12" r="3" />
                        </svg>
                      )}
                    </button>
                  </div>
                </label>

                {error && (
                  <div className="flex items-center gap-2 rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300" role="alert">
                    <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <circle cx="12" cy="12" r="10" />
                      <line x1="15" y1="9" x2="9" y2="15" />
                      <line x1="9" y1="9" x2="15" y2="15" />
                    </svg>
                    {error}
                  </div>
                )}

                <button
                  type="submit"
                  className="group flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-caliber-600 to-blue-600 px-4 py-3.5 text-sm font-semibold text-white shadow-lg shadow-caliber-600/25 transition-all hover:shadow-xl hover:shadow-caliber-600/30 hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-caliber-500/50 focus:ring-offset-2 focus:ring-offset-slate-950 active:scale-[0.98]"
                >
                  Sign in
                  <svg className="w-4 h-4 transition-transform group-hover:translate-x-0.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M5 12h14M12 5l7 7-7 7" />
                  </svg>
                </button>
              </form>

              <div className="mt-6 flex items-center gap-3 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3">
                <div className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-lg bg-caliber-600/10 text-caliber-400">
                  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <circle cx="12" cy="12" r="10" />
                    <line x1="12" y1="16" x2="12" y2="12" />
                    <line x1="12" y1="8" x2="12.01" y2="8" />
                  </svg>
                </div>
                <div className="text-xs text-slate-500">
                  Demo credentials:{" "}
                  <span className="font-semibold text-slate-300">admin</span>
                  {" / "}
                  <span className="font-semibold text-slate-300">admin</span>
                </div>
              </div>
            </div>

          </div>
        </section>
        </div>
      </div>
    </main>
  );
}
