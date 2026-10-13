"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * Shared loading / error building blocks, so every view handles them the same way.
 */

/** True once `active` has lasted `ms`: used to explain slow loads (Cloud Run cold starts). */
export function useSlow(active: boolean, ms = 3000): boolean {
  const [slow, setSlow] = useState(false);
  const [wasActive, setWasActive] = useState(active);
  if (active !== wasActive) {
    setWasActive(active);
    if (!active) setSlow(false);
  }
  useEffect(() => {
    if (!active) return;
    const t = setTimeout(() => setSlow(true), ms);
    return () => clearTimeout(t);
  }, [active, ms]);
  return active && slow;
}

export function SlowHint({ active }: { active: boolean }) {
  const slow = useSlow(active);
  if (!slow) return null;
  return (
    <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
      Still working… the API scales to zero when idle, so the first request can take a few seconds to wake it up.
    </p>
  );
}

export function ErrorState({
  message,
  onRetry,
  backHref = "/",
  backLabel = "← Scan another lockfile",
}: {
  message: string;
  onRetry?: () => void;
  backHref?: string;
  backLabel?: string;
}) {
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950/30">
      <p className="text-red-800 dark:text-red-300">{message}</p>
      <div className="flex flex-wrap items-center gap-3">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="min-h-11 rounded-lg bg-red-700 px-4 text-sm font-medium text-white hover:bg-red-800 dark:bg-red-600 dark:hover:bg-red-500"
          >
            Try again
          </button>
        )}
        <Link href={backHref} className="inline-flex min-h-11 items-center text-sm font-medium text-indigo-700 hover:underline dark:text-indigo-300">
          {backLabel}
        </Link>
      </div>
    </div>
  );
}
