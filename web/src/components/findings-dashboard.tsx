"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import type { FindingSummary, Severity } from "@/lib/api/client";
import { rememberFilters } from "@/lib/dashboard-memory";
import {
  applyFilters,
  type Filters,
  filtersFromParams,
  filtersToParams,
  groupBySeverity,
} from "@/lib/findings-filter";
import { SEVERITY_STYLE } from "@/lib/severity";

import { FilterBar } from "./filter-bar";
import { FindingRow } from "./finding-row";

type Props = { scanId: string; findings: FindingSummary[]; counts: Record<Severity, number> };

export function FindingsDashboard({ scanId, findings, counts }: Props) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Filters live in the URL (shareable, survive reloads) but are applied from local
  // state immediately: router.replace commits as a transition, so URL-only state made
  // controls lag (a clicked checkbox snapped back until navigation finished).
  const urlKey = searchParams.toString();
  const [filters, setLocalFilters] = useState<Filters>(() =>
    filtersFromParams(new URLSearchParams(urlKey)),
  );
  // Adopt URL changes made elsewhere (back/forward, a pasted link), adjusting state
  // during render rather than in an effect.
  const [syncedKey, setSyncedKey] = useState(urlKey);
  if (urlKey !== syncedKey) {
    setSyncedKey(urlKey);
    setLocalFilters(filtersFromParams(new URLSearchParams(urlKey)));
  }
  const setFilters = useCallback(
    (next: Filters) => {
      setLocalFilters(next);
      const qs = filtersToParams(next).toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname],
  );

  // Remember this view so a finding's "← All findings" link returns to it.
  useEffect(() => rememberFilters(scanId, urlKey), [scanId, urlKey]);

  const visible = useMemo(() => applyFilters(findings, filters), [findings, filters]);
  const groups = useMemo(() => groupBySeverity(visible), [visible]);

  if (findings.length === 0) {
    return (
      <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-6 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
        <p className="font-medium">No known vulnerable or malicious packages.</p>
        <p className="mt-1 text-sm opacity-80">
          Checked against the OSV.dev database (GitHub advisories and OpenSSF malicious-package reports).
          New advisories are matched against this scan automatically as they&apos;re published.
        </p>
      </div>
    );
  }

  return (
    <section aria-labelledby="findings-heading" className="flex flex-col gap-4">
      <h2 id="findings-heading" className="sr-only">
        Findings
      </h2>
      <FilterBar filters={filters} counts={counts} onChange={setFilters} />
      <p className="text-sm text-zinc-500" aria-live="polite">
        Showing {visible.length} of {findings.length} findings
      </p>

      {groups.length === 0 ? (
        <div className="rounded-xl border border-dashed border-zinc-300 p-8 text-center text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
          No findings match these filters.{" "}
          <button
            type="button"
            className="font-medium text-indigo-600 hover:underline dark:text-indigo-400"
            onClick={() => setFilters({ severities: [], kind: "all", reach: "all", hideDevOnly: false, query: "" })}
          >
            Reset filters
          </button>
        </div>
      ) : (
        groups.map((group) => (
          <div key={group.severity} className="flex flex-col gap-2">
            <h3 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-zinc-600 dark:text-zinc-400">
              <span className={`size-2.5 rounded-full ${SEVERITY_STYLE[group.severity].dot}`} aria-hidden />
              {SEVERITY_STYLE[group.severity].label}
              <span className="font-normal tabular-nums text-zinc-400">{group.findings.length}</span>
            </h3>
            <ul className="flex flex-col gap-2">
              {group.findings.map((f) => (
                <FindingRow key={f.id} finding={f} scanId={scanId} />
              ))}
            </ul>
          </div>
        ))
      )}
    </section>
  );
}
