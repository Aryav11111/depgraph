// CI guard: bundled sample lockfiles that mirror backend test fixtures must stay
// byte-identical to them (the depgraph-web sample is a deliberate pinned snapshot).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
const pairs = [
  ["web/public/samples/legacy-api.json", "tests/fixtures/npm/vulnerable/package-lock.json"],
  ["web/public/samples/acme-dashboard.json", "tests/fixtures/npm/realworld/package-lock.v3.json"],
];

let failed = false;
for (const [sample, fixture] of pairs) {
  if (!readFileSync(root + sample).equals(readFileSync(root + fixture))) {
    console.error(`${sample} differs from ${fixture}: copy the fixture over the sample`);
    failed = true;
  }
}
if (failed) process.exit(1);
console.log(`samples match fixtures (${pairs.length} checked)`);
