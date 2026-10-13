import type { Severity } from "./api/client";

export const SEVERITY_ORDER: Severity[] = ["CRITICAL", "HIGH", "MODERATE", "LOW", "UNKNOWN"];

/** Tailwind classes per severity. Text labels always accompany colour (not colour-only). */
export const SEVERITY_STYLE: Record<Severity, { badge: string; dot: string; label: string }> = {
  CRITICAL: {
    badge: "bg-red-100 text-red-800 ring-red-200 dark:bg-red-950/60 dark:text-red-300 dark:ring-red-900",
    dot: "bg-red-600",
    label: "Critical",
  },
  HIGH: {
    badge: "bg-orange-100 text-orange-800 ring-orange-200 dark:bg-orange-950/60 dark:text-orange-300 dark:ring-orange-900",
    dot: "bg-orange-500",
    label: "High",
  },
  MODERATE: {
    badge: "bg-amber-100 text-amber-800 ring-amber-200 dark:bg-amber-950/60 dark:text-amber-300 dark:ring-amber-900",
    dot: "bg-amber-400",
    label: "Moderate",
  },
  LOW: {
    badge: "bg-sky-100 text-sky-800 ring-sky-200 dark:bg-sky-950/60 dark:text-sky-300 dark:ring-sky-900",
    dot: "bg-sky-500",
    label: "Low",
  },
  UNKNOWN: {
    badge: "bg-zinc-100 text-zinc-700 ring-zinc-200 dark:bg-zinc-900 dark:text-zinc-300 dark:ring-zinc-800",
    dot: "bg-zinc-400",
    label: "Unknown",
  },
};
