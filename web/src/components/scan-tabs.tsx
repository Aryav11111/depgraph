"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/** Findings | Graph tabs shared by both scan pages. */
export function ScanTabs({ scanId }: { scanId: string }) {
  const pathname = usePathname();
  const tabs = [
    { href: `/scans/${scanId}`, label: "Findings" },
    { href: `/scans/${scanId}/graph`, label: "Dependency graph" },
  ];
  return (
    <nav aria-label="Scan views" className="flex gap-1 border-b border-zinc-200 dark:border-zinc-800">
      {tabs.map((t) => {
        const active = pathname === t.href;
        return (
          <Link
            key={t.href}
            href={t.href}
            aria-current={active ? "page" : undefined}
            className={[
              "-mb-px border-b-2 px-3 py-2 text-sm font-medium",
              active
                ? "border-indigo-600 text-indigo-700 dark:border-indigo-400 dark:text-indigo-300"
                : "border-transparent text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100",
            ].join(" ")}
          >
            {t.label}
          </Link>
        );
      })}
    </nav>
  );
}
