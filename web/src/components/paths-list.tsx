"use client";

import { useState } from "react";

import type { FindingDetail } from "@/lib/api/client";

type Path = FindingDetail["paths"][number];
const INITIAL = 10;

/** Full root → package chains, shortest first (the API's order). */
export function PathsList({ paths, truncated }: { paths: Path[]; truncated: boolean }) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? paths : paths.slice(0, INITIAL);

  if (paths.length === 0) {
    return <p className="text-sm text-zinc-500">No path from the project root within the depth limit.</p>;
  }
  return (
    <div className="flex flex-col gap-2">
      <ol className="flex flex-col gap-1.5">
        {visible.map((path, i) => (
          <li key={i} className="flex gap-3 rounded-lg bg-zinc-50 px-3 py-2 dark:bg-zinc-900">
            <span className="w-6 shrink-0 text-right font-mono text-xs leading-6 text-zinc-400">{i + 1}</span>
            <span className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5 font-mono text-xs leading-6">
              {path.map((step, j) => (
                <span key={`${step.id}-${j}`} className="inline-flex items-center gap-1">
                  {j > 0 && (
                    <span className="text-zinc-400" aria-hidden>
                      →
                    </span>
                  )}
                  {step.dep_type && step.dep_type !== "prod" && (
                    <span className="rounded bg-zinc-200 px-1 text-[10px] uppercase text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                      {step.dep_type.replace("_", " ")}
                    </span>
                  )}
                  <span className={j === path.length - 1 ? "font-semibold text-red-700 dark:text-red-400" : ""}>
                    {step.name}
                    {step.version && <span className="text-zinc-500">@{step.version}</span>}
                  </span>
                </span>
              ))}
            </span>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {paths.length > INITIAL && (
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className="font-medium text-indigo-600 hover:underline dark:text-indigo-400"
          >
            {showAll ? "Show fewer" : `Show all ${paths.length} paths`}
          </button>
        )}
        {truncated && (
          <span className="text-zinc-500">
            More paths exist; showing the {paths.length} shortest (server cap).
          </span>
        )}
      </div>
    </div>
  );
}
