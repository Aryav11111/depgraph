"use client";

import { useEffect, useState } from "react";

import type { FindingSummary, Scan, Severity } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { getFindings, getScan } from "@/lib/api/scans";

import { FindingsDashboard } from "./findings-dashboard";
import { ScanTabs } from "./scan-tabs";
import { ErrorState, SlowHint } from "./states";

type Loaded = {
  scan: Scan;
  findings: FindingSummary[] | null;
  counts: Record<Severity, number> | null;
};
type State = { kind: "loading" } | { kind: "error"; message: string } | ({ kind: "ok" } & Loaded);

/** A scan page: header + findings dashboard. Fetches once; filters work client-side. */
export function ScanView({ scanId }: { scanId: string }) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const retry = () => {
    setState({ kind: "loading" });
    setAttempt((n) => n + 1);
  };

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const scan = await getScan(scanId, controller.signal);
        const body = scan.status === "completed" ? await getFindings(scanId, controller.signal) : null;
        setState({ kind: "ok", scan, findings: body?.findings ?? null, counts: body?.counts ?? null });
      } catch (err) {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          message: err instanceof ApiError ? err.message : "Couldn't load this scan.",
        });
      }
    })();
    return () => controller.abort();
  }, [scanId, attempt]);

  if (state.kind === "loading") {
    return (
      <div className="flex flex-col gap-4">
        <SlowHint active />
        <ScanSkeleton />
      </div>
    );
  }
  if (state.kind === "error") {
    return <ErrorState message={state.message} onRetry={retry} />;
  }

  const { scan, findings, counts } = state;
  return (
    <div className="flex flex-col gap-8">
      <header>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">Scan</p>
        <h1 className="text-2xl font-semibold tracking-tight break-words sm:text-3xl">
          {scan.root_name ?? "Untitled project"}
        </h1>
        <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
          {packageCount(scan)} packages · lockfile v{scan.lockfile_version ?? "?"}
          {findings && ` · ${findings.length} findings`}
          {scan.warnings.length > 0 &&
            ` · ${scan.warnings.length} lockfile warning${scan.warnings.length === 1 ? "" : "s"}`}
        </p>
      </header>

      {scan.status === "completed" && <ScanTabs scanId={scanId} />}

      {scan.status !== "completed" ? (
        <p role="alert" className="text-amber-700 dark:text-amber-400">
          This scan is {scan.status}
          {scan.error ? `: ${scan.error}` : "."}
        </p>
      ) : findings && counts ? (
        <FindingsDashboard scanId={scanId} findings={findings} counts={counts} />
      ) : null}
    </div>
  );
}

/** node_count includes the project itself (the graph root); people count dependencies. */
function packageCount(scan: Scan): string {
  return scan.node_count == null ? "?" : Math.max(0, scan.node_count - 1).toLocaleString();
}

function ScanSkeleton() {
  return (
    <div className="flex animate-pulse flex-col gap-4" aria-busy="true" aria-label="Loading scan">
      <div className="h-4 w-16 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="h-8 w-64 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="h-4 w-48 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="mt-4 h-24 rounded-xl bg-zinc-200 dark:bg-zinc-800" />
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-20 rounded-xl bg-zinc-100 dark:bg-zinc-900" />
      ))}
    </div>
  );
}
