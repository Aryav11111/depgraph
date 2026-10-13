"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import type { Scan, Severity } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { getFindings, getScan } from "@/lib/api/scans";
import { SEVERITY_ORDER, SEVERITY_STYLE } from "@/lib/severity";

type Loaded = { scan: Scan; counts: Record<Severity, number> | null };
type State = { kind: "loading" } | { kind: "error"; message: string } | ({ kind: "ok" } & Loaded);

/**
 * Minimal scan summary so uploads have somewhere to land (step 2). The findings
 * dashboard (step 3) builds on this page.
 */
export function ScanSummary({ scanId }: { scanId: string }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const scan = await getScan(scanId, controller.signal);
        const counts =
          scan.status === "completed"
            ? (await getFindings(scanId, controller.signal)).counts
            : null;
        setState({ kind: "ok", scan, counts });
      } catch (err) {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          message: err instanceof ApiError ? err.message : "Couldn't load this scan.",
        });
      }
    })();
    return () => controller.abort();
  }, [scanId]);

  if (state.kind === "loading") return <SummarySkeleton />;
  if (state.kind === "error") {
    return (
      <div role="alert" className="flex flex-col items-start gap-3">
        <p className="text-red-700 dark:text-red-400">{state.message}</p>
        <Link href="/" className="text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400">
          ← Scan another lockfile
        </Link>
      </div>
    );
  }

  const { scan, counts } = state;
  const total = counts ? Object.values(counts).reduce((a, b) => a + b, 0) : 0;
  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="text-sm text-zinc-500">Scan</p>
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">{scan.root_name ?? "Untitled project"}</h1>
        <p className="mt-1 text-sm text-zinc-500">
          {packageCount(scan)} packages · lockfile v{scan.lockfile_version ?? "?"} · status {scan.status}
        </p>
      </div>

      {counts && (
        <div>
          <p className="mb-3 text-lg">
            {total === 0 ? "No known vulnerabilities or malicious packages found." : `${total} findings`}
          </p>
          <ul className="flex flex-wrap gap-2">
            {SEVERITY_ORDER.filter((s) => counts[s] > 0).map((s) => (
              <li key={s} className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-sm ring-1 ${SEVERITY_STYLE[s].badge}`}>
                <span className={`size-2 rounded-full ${SEVERITY_STYLE[s].dot}`} aria-hidden />
                {counts[s]} {SEVERITY_STYLE[s].label}
              </li>
            ))}
          </ul>
        </div>
      )}

      {scan.warnings.length > 0 && (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          {scan.warnings.length} lockfile warning{scan.warnings.length === 1 ? "" : "s"} (e.g. unresolved dependencies).
        </p>
      )}
    </div>
  );
}

function SummarySkeleton() {
  return (
    <div className="flex animate-pulse flex-col gap-3" aria-busy="true" aria-label="Loading scan">
      <div className="h-4 w-16 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="h-8 w-64 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="h-4 w-48 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="mt-4 h-6 w-80 rounded bg-zinc-200 dark:bg-zinc-800" />
    </div>
  );
}

/** node_count includes the project itself (the graph root); people count dependencies. */
function packageCount(scan: Scan): string {
  return scan.node_count == null ? "?" : Math.max(0, scan.node_count - 1).toLocaleString();
}
