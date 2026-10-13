/**
 * Findings filters: pure, so they're unit-tested, and round-tripped through the
 * URL query string so a filtered view survives reloads and can be shared.
 */
import type { FindingSummary, Severity } from "./api/client";
import { SEVERITY_ORDER } from "./severity";

export type Filters = {
  severities: Severity[]; // empty = all
  kind: "all" | "vulnerability" | "malicious";
  reach: "all" | "direct" | "transitive";
  hideDevOnly: boolean;
  query: string;
};

export const DEFAULT_FILTERS: Filters = {
  severities: [],
  kind: "all",
  reach: "all",
  hideDevOnly: false,
  query: "",
};

const KINDS = ["all", "vulnerability", "malicious"] as const;
const REACHES = ["all", "direct", "transitive"] as const;
const MAX_QUERY = 100;

/** Untrusted input (anyone can craft a URL): unknown values fall back to defaults. */
export function filtersFromParams(params: URLSearchParams): Filters {
  const severities = (params.get("severity") ?? "")
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter((s): s is Severity => (SEVERITY_ORDER as string[]).includes(s));
  const kind = params.get("kind");
  const reach = params.get("reach");
  return {
    severities: [...new Set(severities)],
    kind: KINDS.includes(kind as Filters["kind"]) ? (kind as Filters["kind"]) : "all",
    reach: REACHES.includes(reach as Filters["reach"]) ? (reach as Filters["reach"]) : "all",
    hideDevOnly: params.get("hide_dev") === "1",
    query: (params.get("q") ?? "").slice(0, MAX_QUERY),
  };
}

/** Only non-default values are written, so the default view has a clean URL. */
export function filtersToParams(filters: Filters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.severities.length) {
    params.set("severity", SEVERITY_ORDER.filter((s) => filters.severities.includes(s)).join(","));
  }
  if (filters.kind !== "all") params.set("kind", filters.kind);
  if (filters.reach !== "all") params.set("reach", filters.reach);
  if (filters.hideDevOnly) params.set("hide_dev", "1");
  if (filters.query.trim()) params.set("q", filters.query.trim());
  return params;
}

export function isFiltered(filters: Filters): boolean {
  return filtersToParams(filters).size > 0;
}

export function matchesQuery(f: FindingSummary, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [f.package, f.advisory.id, f.advisory.summary ?? "", ...f.advisory.aliases].some((s) =>
    s.toLowerCase().includes(q),
  );
}

export function applyFilters(findings: FindingSummary[], filters: Filters): FindingSummary[] {
  return findings.filter(
    (f) =>
      (filters.severities.length === 0 || filters.severities.includes(f.advisory.severity)) &&
      (filters.kind === "all" || f.advisory.kind === filters.kind) &&
      (filters.reach === "all" || (filters.reach === "direct") === f.direct) &&
      !(filters.hideDevOnly && f.dev_only) &&
      matchesQuery(f, filters.query),
  );
}

export type SeverityGroup = { severity: Severity; findings: FindingSummary[] };

/** Most severe first; empty groups dropped; API order (CVSS, then name) kept within a group. */
export function groupBySeverity(findings: FindingSummary[]): SeverityGroup[] {
  return SEVERITY_ORDER.map((severity) => ({
    severity,
    findings: findings.filter((f) => f.advisory.severity === severity),
  })).filter((g) => g.findings.length > 0);
}
