/**
 * Real npm-generated lockfiles bundled as one-click demos (served from
 * /public/samples). The first two are copies of the backend's test fixtures (CI
 * checks they stay identical); the third is a pinned snapshot of this frontend's
 * own lockfile, so the demo doesn't shift whenever a dependency is bumped.
 */
export type Sample = {
  id: string;
  file: string;
  title: string;
  blurb: string;
  highlights: string;
};

export const SAMPLES: Sample[] = [
  {
    id: "legacy-api",
    file: "/samples/legacy-api.json",
    title: "Legacy Express API",
    blurb: "An unmaintained API pinned to releases with famous CVEs: express 4.17.1, lodash, axios, jsonwebtoken.",
    highlights: "171 packages · 89 findings, 6 critical (Oct 2026)",
  },
  {
    id: "acme-dashboard",
    file: "/samples/acme-dashboard.json",
    title: "React + Vite dashboard",
    blurb: "A typical modern app. Its CRITICAL issues sit in dev-only tooling. Can you tell from the paths?",
    highlights: "384 packages · 11 findings, 9 dev-only · matches npm audit",
  },
  {
    id: "depgraph-web",
    file: "/samples/depgraph-web.json",
    title: "This website",
    blurb: "depgraph's own frontend, scanned by depgraph. Dogfooding, with an honest result.",
    highlights: "505 packages · 1 finding, dev-only, no fix exists",
  },
];
