"use client";

import Link from "next/link";
import { useEffect, useState, useSyncExternalStore } from "react";

import type { FindingDetail } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { getFinding } from "@/lib/api/scans";
import { recallFilters } from "@/lib/dashboard-memory";
import { EXTERNAL_LINK, safeHttpUrl } from "@/lib/safe-url";
import { SEVERITY_STYLE } from "@/lib/severity";

import { AdvisoryMarkdown } from "./advisory-markdown";
import { Badge } from "./finding-row";
import { FixCard } from "./fix-card";
import { PathsList } from "./paths-list";
import { ErrorState, SlowHint } from "./states";

type State = { kind: "loading" } | { kind: "error"; message: string } | { kind: "ok"; finding: FindingDetail };

const noSubscribe = () => () => {};

export function FindingDetailView({ scanId, findingId }: { scanId: string; findingId: string }) {
  const id = Number(findingId);
  const validId = Number.isSafeInteger(id) && id > 0;
  const [state, setState] = useState<State>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const retry = () => {
    setState({ kind: "loading" });
    setAttempt((n) => n + 1);
  };

  // sessionStorage is an external store: read it with useSyncExternalStore, which
  // uses the server snapshot ("") during hydration, so there is no mismatch.
  const remembered = useSyncExternalStore(noSubscribe, () => recallFilters(scanId), () => "");
  const backHref = remembered ? `/scans/${scanId}?${remembered}` : `/scans/${scanId}`;

  useEffect(() => {
    if (!validId) return;
    const controller = new AbortController();
    getFinding(scanId, id, controller.signal)
      .then((finding) => setState({ kind: "ok", finding }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          message: err instanceof ApiError ? err.message : "Couldn't load this finding.",
        });
      });
    return () => controller.abort();
  }, [scanId, id, validId, attempt]);

  const back = (
    <Link href={backHref} className="text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400">
      ← All findings
    </Link>
  );

  if (!validId) {
    return (
      <ErrorState message="Not a valid finding." backHref={backHref} backLabel="← All findings" />
    );
  }
  if (state.kind === "loading") {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <SlowHint active />
        <DetailSkeleton />
      </div>
    );
  }
  if (state.kind === "error") {
    // 404s won't fix themselves; everything else (network, 5xx, cold start) might.
    const retryable = !/not found/i.test(state.message);
    return (
      <ErrorState
        message={state.message}
        onRetry={retryable ? retry : undefined}
        backHref={backHref}
        backLabel="← All findings"
      />
    );
  }

  const f = state.finding;
  const a = f.advisory;
  const style = SEVERITY_STYLE[a.severity];
  const osv = safeHttpUrl(a.url);
  const references = a.references.filter((r) => safeHttpUrl(r.url));

  return (
    <article className="flex flex-col gap-8">
      <div className="flex flex-col gap-3">
        {back}
        <div className="flex flex-wrap items-center gap-2">
          <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-sm ring-1 ${style.badge}`}>
            <span className={`size-2 rounded-full ${style.dot}`} aria-hidden />
            {style.label}
            {a.cvss_v3_score != null && <span className="tabular-nums">· CVSS {a.cvss_v3_score.toFixed(1)}</span>}
          </span>
          {a.kind === "malicious" && <Badge tone="red">Malicious package</Badge>}
          {f.dev_only && <Badge>Dev-only</Badge>}
        </div>
        <h1 className="text-2xl font-semibold tracking-tight break-words sm:text-3xl">{a.summary ?? a.id}</h1>
        <p className="font-mono text-sm">
          {f.package}@<span className="text-red-700 dark:text-red-400">{f.version}</span>
        </p>
        <p className="flex flex-wrap gap-x-3 gap-y-1 text-sm text-zinc-600 dark:text-zinc-400">
          {osv ? (
            <a href={osv} {...EXTERNAL_LINK} className="font-mono text-indigo-600 hover:underline dark:text-indigo-400">
              {a.id}
            </a>
          ) : (
            <span className="font-mono">{a.id}</span>
          )}
          {a.aliases.map((alias) => (
            <span key={alias} className="font-mono">
              {alias}
            </span>
          ))}
          {a.published && <span>published {new Date(a.published).toLocaleDateString()}</span>}
        </p>
      </div>

      <FixCard fix={f.fix} pkg={f.package} installed={f.version} />

      <section aria-labelledby="paths-heading" className="flex flex-col gap-3">
        <h2 id="paths-heading" className="font-semibold">
          How it gets into your project{" "}
          <span className="font-normal text-zinc-600 dark:text-zinc-400">
            · {f.paths.length}
            {f.paths_truncated ? "+" : ""} path{f.paths.length === 1 ? "" : "s"}, shortest first
          </span>
        </h2>
        <PathsList paths={f.paths} truncated={f.paths_truncated} />
      </section>

      {a.details && (
        <section aria-labelledby="details-heading" className="flex flex-col gap-3">
          <h2 id="details-heading" className="font-semibold">
            Advisory details
          </h2>
          <AdvisoryMarkdown>{a.details}</AdvisoryMarkdown>
          {(a.cvss_v3_vector || a.cvss_v4_vector) && (
            <p className="break-all font-mono text-xs text-zinc-600 dark:text-zinc-400">{a.cvss_v3_vector ?? a.cvss_v4_vector}</p>
          )}
        </section>
      )}

      {references.length > 0 && (
        <section aria-labelledby="refs-heading" className="flex flex-col gap-2">
          <h2 id="refs-heading" className="font-semibold">
            References
          </h2>
          <ul className="flex flex-col gap-1 text-sm">
            {references.map((r, i) => (
              <li key={i} className="flex min-w-0 gap-2">
                <span className="w-20 shrink-0 text-xs uppercase leading-5 text-zinc-600 dark:text-zinc-400">{r.type.toLowerCase()}</span>
                <a href={safeHttpUrl(r.url)!} {...EXTERNAL_LINK} className="min-w-0 break-all text-indigo-600 hover:underline dark:text-indigo-400">
                  {r.url}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </article>
  );
}

function DetailSkeleton() {
  return (
    <div className="flex animate-pulse flex-col gap-4" aria-busy="true" aria-label="Loading finding">
      <div className="h-6 w-32 rounded-full bg-zinc-200 dark:bg-zinc-800" />
      <div className="h-8 w-3/4 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="h-4 w-40 rounded bg-zinc-200 dark:bg-zinc-800" />
      <div className="mt-2 h-36 rounded-xl bg-zinc-200 dark:bg-zinc-800" />
      <div className="h-24 rounded-xl bg-zinc-100 dark:bg-zinc-900" />
    </div>
  );
}
