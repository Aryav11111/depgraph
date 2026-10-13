import Link from "next/link";

import type { FindingSummary } from "@/lib/api/client";
import { SEVERITY_STYLE } from "@/lib/severity";

/** Shortest dependency path, middle-truncated so long chains stay one line. */
export function PathTrail({ path }: { path: FindingSummary["shortest_path"] }) {
  if (!path) return <span className="text-zinc-600 dark:text-zinc-400">path beyond depth limit</span>;
  const steps = path.length > 5 ? [...path.slice(0, 2), null, ...path.slice(-2)] : path;
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1 font-mono text-xs text-zinc-600 dark:text-zinc-400">
      {steps.map((step, i) =>
        step === null ? (
          <span key="gap" title={`${path.length - 4} more`} className="text-zinc-500 dark:text-zinc-400">
            › …
          </span>
        ) : (
          <span key={`${step.id}-${i}`} className="inline-flex items-center gap-1">
            {i > 0 && <span className="text-zinc-500 dark:text-zinc-400">›</span>}
            <span>{step.name}</span>
            {step.dep_type && step.dep_type !== "prod" && (
              <span className="rounded bg-zinc-100 px-1 text-[10px] uppercase text-zinc-600 dark:text-zinc-400 dark:bg-zinc-800">
                {step.dep_type.replace("_", " ")}
              </span>
            )}
          </span>
        ),
      )}
    </span>
  );
}

export function Badge({ children, tone = "zinc" }: { children: React.ReactNode; tone?: "zinc" | "red" | "indigo" }) {
  const tones = {
    zinc: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
    red: "bg-red-600 text-white",
    indigo: "bg-indigo-50 text-indigo-700 dark:bg-indigo-950/60 dark:text-indigo-300",
  };
  return <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${tones[tone]}`}>{children}</span>;
}

export function FindingRow({ finding, scanId }: { finding: FindingSummary; scanId: string }) {
  const { advisory } = finding;
  const style = SEVERITY_STYLE[advisory.severity];
  const cve = advisory.aliases.find((a) => a.startsWith("CVE-"));
  const malicious = advisory.kind === "malicious";
  return (
    <li>
      <Link
        href={`/scans/${scanId}/findings/${finding.id}`}
        className="flex flex-col gap-2 rounded-xl border border-zinc-200 p-4 transition hover:border-indigo-400 hover:shadow-sm focus-visible:outline-2 focus-visible:outline-indigo-500 dark:border-zinc-800 dark:hover:border-indigo-500"
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className={`size-2 shrink-0 rounded-full ${style.dot}`} aria-hidden />
          <span className="font-mono font-medium">{finding.package}</span>
          <span className="font-mono text-sm">
            <span className="text-red-700 dark:text-red-400">{finding.version}</span>
            {malicious ? (
              <span className="text-zinc-600 dark:text-zinc-400"> → remove</span>
            ) : finding.fixed_version_hint ? (
              <span className="text-zinc-600 dark:text-zinc-400">
                {" → "}
                <span className="text-emerald-700 dark:text-emerald-400">{finding.fixed_version_hint}</span>
              </span>
            ) : (
              <span className="text-zinc-600 dark:text-zinc-400"> → no fix yet</span>
            )}
          </span>
          <span className="flex flex-wrap gap-1.5">
            {malicious && <Badge tone="red">Malicious</Badge>}
            <Badge tone={finding.direct ? "indigo" : "zinc"}>{finding.direct ? "Direct" : "Transitive"}</Badge>
            {finding.dev_only && <Badge>Dev-only</Badge>}
          </span>
        </div>
        <p className="text-sm">
          <span className="sr-only">{style.label} severity: </span>
          {advisory.summary ?? advisory.id}
        </p>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <PathTrail path={finding.shortest_path} />
          <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">
            {cve ?? advisory.id}
            {advisory.cvss_v3_score != null && ` · CVSS ${advisory.cvss_v3_score.toFixed(1)}`}
          </span>
        </div>
      </Link>
    </li>
  );
}
