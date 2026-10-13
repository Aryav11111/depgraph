import { describe, expect, it } from "vitest";

import type { FindingSummary } from "./api/client";
import {
  DEFAULT_FILTERS,
  applyFilters,
  filtersFromParams,
  filtersToParams,
  groupBySeverity,
  isFiltered,
  type Filters,
} from "./findings-filter";

function finding(over: Partial<FindingSummary> & { severity?: string; kind?: string; id?: number }) {
  const { severity = "HIGH", kind = "vulnerability", ...rest } = over;
  return {
    id: 1,
    package: "lodash",
    version: "4.17.20",
    direct: true,
    dev_only: false,
    fixed_version_hint: "4.17.21",
    shortest_path: null,
    first_detected: "2026-10-01T00:00:00Z",
    last_confirmed: "2026-10-01T00:00:00Z",
    ...rest,
    advisory: {
      id: "GHSA-35jh-r3h4-6jhm",
      kind,
      severity,
      cvss_v3_score: 7.2,
      summary: "Command Injection in lodash",
      aliases: ["CVE-2021-23337"],
      published: null,
      url: "https://osv.dev/vulnerability/GHSA-35jh-r3h4-6jhm",
      ...(rest.advisory ?? {}),
    },
  } as FindingSummary;
}

const ALL = [
  finding({ id: 1, package: "lodash", severity: "HIGH" }),
  finding({ id: 2, package: "minimist", severity: "CRITICAL", direct: false,
            advisory: { id: "GHSA-xvch-5gv4-984h", aliases: ["CVE-2021-44906"], summary: "Prototype Pollution" } as never }),
  finding({ id: 3, package: "esbuild", severity: "MODERATE", direct: false, dev_only: true,
            advisory: { id: "GHSA-67mh-4wv8-2f99", aliases: [], summary: "dev server CORS" } as never }),
  finding({ id: 4, package: "tostpro", severity: "CRITICAL", kind: "malicious",
            advisory: { id: "MAL-2026-17564", aliases: [], summary: "Malicious code in tostpro" } as never }),
];

const ids = (list: FindingSummary[]) => list.map((f) => f.id).sort();
const f = (over: Partial<Filters>): Filters => ({ ...DEFAULT_FILTERS, ...over });

describe("applyFilters", () => {
  it("default filters keep everything", () => {
    expect(ids(applyFilters(ALL, DEFAULT_FILTERS))).toEqual([1, 2, 3, 4]);
  });
  it.each<[string, Partial<Filters>, number[]]>([
    ["severity", { severities: ["CRITICAL"] }, [2, 4]],
    ["several severities", { severities: ["CRITICAL", "MODERATE"] }, [2, 3, 4]],
    ["malicious only", { kind: "malicious" }, [4]],
    ["vulnerabilities only", { kind: "vulnerability" }, [1, 2, 3]],
    ["direct", { reach: "direct" }, [1, 4]],
    ["transitive", { reach: "transitive" }, [2, 3]],
    ["hide dev-only", { hideDevOnly: true }, [1, 2, 4]],
    ["query by package", { query: "MINI" }, [2]],
    ["query by CVE alias", { query: "cve-2021-23337" }, [1]],
    ["query by advisory id", { query: "MAL-2026" }, [4]],
    ["query by summary", { query: "prototype" }, [2]],
    ["combined", { severities: ["CRITICAL"], reach: "transitive" }, [2]],
    ["nothing matches", { query: "left-pad" }, []],
  ])("%s", (_, over, expected) => {
    expect(ids(applyFilters(ALL, f(over)))).toEqual(expected);
  });
});

describe("URL round trip", () => {
  it("default filters produce an empty query string", () => {
    expect(filtersToParams(DEFAULT_FILTERS).toString()).toBe("");
    expect(isFiltered(DEFAULT_FILTERS)).toBe(false);
  });

  it("round-trips every field, severities in canonical order", () => {
    const filters = f({ severities: ["LOW", "CRITICAL"], kind: "malicious", reach: "direct",
                        hideDevOnly: true, query: "lodash" });  // prettier-ignore
    const params = filtersToParams(filters);
    expect(params.toString()).toBe("severity=CRITICAL%2CLOW&kind=malicious&reach=direct&hide_dev=1&q=lodash");
    expect(filtersFromParams(params)).toEqual({ ...filters, severities: ["CRITICAL", "LOW"] });
  });

  it("ignores junk from hand-edited or hostile URLs", () => {
    const parsed = filtersFromParams(
      new URLSearchParams("severity=critical,BOGUS,critical&kind=<script>&reach=sideways&hide_dev=yes&q=" + "x".repeat(500)),
    );
    expect(parsed.severities).toEqual(["CRITICAL"]);
    expect(parsed.kind).toBe("all");
    expect(parsed.reach).toBe("all");
    expect(parsed.hideDevOnly).toBe(false);
    expect(parsed.query).toHaveLength(100);
  });
});

describe("groupBySeverity", () => {
  it("orders groups most severe first and drops empty ones", () => {
    const groups = groupBySeverity(ALL);
    expect(groups.map((g) => [g.severity, ids(g.findings)])).toEqual([
      ["CRITICAL", [2, 4]],
      ["HIGH", [1]],
      ["MODERATE", [3]],
    ]);
  });
});
