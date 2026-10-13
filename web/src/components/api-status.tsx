"use client";

import { useEffect, useState } from "react";

import { API_URL, api } from "@/lib/api/client";

type Status =
  | { state: "checking" }
  | { state: "ok"; checks: Record<string, string> }
  | { state: "degraded"; checks: Record<string, string> }
  | { state: "unreachable" };

/** Live API readiness, fetched from the browser (proves CORS + the typed client). */
export function ApiStatus() {
  const [status, setStatus] = useState<Status>({ state: "checking" });

  useEffect(() => {
    const controller = new AbortController();
    api
      .GET("/health/ready", { signal: controller.signal })
      .then(({ data, response }) => {
        const body = (data ?? {}) as { checks?: Record<string, string> };
        const checks = body.checks ?? {};
        setStatus(response.ok ? { state: "ok", checks } : { state: "degraded", checks });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setStatus({ state: "unreachable" });
        if (process.env.NODE_ENV !== "production") console.warn("API unreachable", err);
      });
    return () => controller.abort();
  }, []);

  const dot = {
    checking: "bg-zinc-400 animate-pulse",
    ok: "bg-emerald-500",
    degraded: "bg-amber-500",
    unreachable: "bg-red-500",
  }[status.state];

  const label = {
    checking: "Checking API…",
    ok: "API ready",
    degraded: "API degraded",
    unreachable: "API unreachable",
  }[status.state];

  return (
    <div
      role="status"
      aria-live="polite"
      className="inline-flex items-center gap-2 rounded-full border border-zinc-200 px-3 py-1 text-sm dark:border-zinc-800"
    >
      <span className={`size-2 rounded-full ${dot}`} aria-hidden />
      <span>{label}</span>
      {"checks" in status && (
        <span className="text-zinc-500">
          ({Object.entries(status.checks).map(([k, v]) => `${k}: ${v}`).join(", ")})
        </span>
      )}
      {status.state === "unreachable" && (
        <span className="font-mono text-xs text-zinc-500">{API_URL}</span>
      )}
    </div>
  );
}
