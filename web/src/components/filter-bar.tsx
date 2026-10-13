"use client";

import { useEffect, useState } from "react";

import type { Severity } from "@/lib/api/client";
import { type Filters, isFiltered } from "@/lib/findings-filter";
import { SEVERITY_ORDER, SEVERITY_STYLE } from "@/lib/severity";

type Props = {
  filters: Filters;
  counts: Record<Severity, number>;
  onChange: (next: Filters) => void;
};

export function FilterBar({ filters, counts, onChange }: Props) {
  // The search box is local state, pushed to the URL after a short pause, so
  // typing doesn't rewrite browser history on every keystroke.
  const [query, setQuery] = useState(filters.query);
  // When the URL's query changes from outside (reset, back button), adopt it.
  // Adjusting state during render (React's recommended pattern), not in an effect.
  const [syncedQuery, setSyncedQuery] = useState(filters.query);
  if (filters.query !== syncedQuery) {
    setSyncedQuery(filters.query);
    setQuery(filters.query);
  }
  useEffect(() => {
    if (query === filters.query) return;
    const t = setTimeout(() => onChange({ ...filters, query }), 250);
    return () => clearTimeout(t);
  }, [query, filters, onChange]);

  const toggleSeverity = (s: Severity) =>
    onChange({
      ...filters,
      severities: filters.severities.includes(s)
        ? filters.severities.filter((x) => x !== s)
        : [...filters.severities, s],
    });

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-zinc-200 p-3 dark:border-zinc-800" role="search">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          maxLength={100}
          placeholder="Search package, CVE, advisory…"
          aria-label="Search findings"
          className="min-w-0 flex-1 basis-56 rounded-lg border border-zinc-300 bg-transparent px-3 py-1.5 text-sm outline-none focus:border-indigo-500 dark:border-zinc-700"
        />
        {isFiltered(filters) && (
          <button
            type="button"
            onClick={() => onChange({ severities: [], kind: "all", reach: "all", hideDevOnly: false, query: "" })}
            className="rounded-lg px-3 py-1.5 text-sm text-indigo-600 hover:bg-indigo-50 dark:text-indigo-400 dark:hover:bg-indigo-950/40"
          >
            Reset filters
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Severity">
        {SEVERITY_ORDER.filter((s) => counts[s] > 0).map((s) => {
          const on = filters.severities.includes(s);
          return (
            <button
              key={s}
              type="button"
              aria-pressed={on}
              onClick={() => toggleSeverity(s)}
              className={[
                "inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-sm ring-1 transition",
                on ? SEVERITY_STYLE[s].badge : "ring-zinc-200 text-zinc-600 hover:bg-zinc-50 dark:ring-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-900",
              ].join(" ")}
            >
              <span className={`size-2 rounded-full ${SEVERITY_STYLE[s].dot}`} aria-hidden />
              {SEVERITY_STYLE[s].label} <span className="tabular-nums opacity-70">{counts[s]}</span>
            </button>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
        <Segmented
          label="Kind"
          value={filters.kind}
          options={[["all", "All"], ["vulnerability", "Vulnerabilities"], ["malicious", "Malicious"]]}
          onChange={(kind) => onChange({ ...filters, kind })}
        />
        <Segmented
          label="Reach"
          value={filters.reach}
          options={[["all", "All"], ["direct", "Direct"], ["transitive", "Transitive"]]}
          onChange={(reach) => onChange({ ...filters, reach })}
        />
        <label className="inline-flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={filters.hideDevOnly}
            onChange={(e) => onChange({ ...filters, hideDevOnly: e.target.checked })}
            className="size-4 accent-indigo-600"
          />
          Hide dev-only
        </label>
      </div>
    </div>
  );
}

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: [T, string][];
  onChange: (v: T) => void;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-lg bg-zinc-100 p-0.5 dark:bg-zinc-900">
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          onClick={() => onChange(v)}
          className={[
            "rounded-md px-2.5 py-1 transition",
            value === v
              ? "bg-white shadow-sm dark:bg-zinc-700"
              : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100",
          ].join(" ")}
        >
          {text}
        </button>
      ))}
    </div>
  );
}
