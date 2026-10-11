# Test fixtures

Everything here is real tool output, not hand-written, so tests exercise the quirks
real data has.

## `npm/realworld/`
A typical Express + React + Vite app, about 390 packages. `package-lock.v2.json` and
`package-lock.v3.json` are npm 10's output for the same `package.json`. The parser must
produce identical graphs from both. `npm-audit.json` is `npm audit --json` for the v3
lockfile.

## `npm/vulnerable/`
An unmaintained API pinned to releases with well-known advisories (express 4.17.1,
lodash 4.17.20, axios 0.21.1, minimist 1.2.5, jsonwebtoken 8.5.1, …), 177 packages.
`npm-audit.json` is the reference our findings must match exactly
(`tests/test_against_npm_audit.py`).

## `osv/`
- `<ID>.json`: individual OSV records chosen for edge cases (`last_affected`,
  withdrawn, versions-only, multi-ecosystem with prerelease bounds, malicious).
- `audit-subset.zip`: every record in the full npm export that mentions any package in
  the two lockfiles above (352 records). It includes the many that *don't* affect the
  installed versions, so over-matching fails the test too.

## Regenerating (keep the snapshots in sync)

npm audit results and the OSV subset must describe the **same moment**, or upstream
advisory changes make the comparison drift. Regenerate them together:

```bash
# 1. Lockfiles (only if package.json changed) and npm audit snapshots
for p in realworld vulnerable; do
  docker run --rm -v "$PWD/tests/fixtures/npm/$p:/src:ro" node:22-alpine sh -c \
    'mkdir /w && cp /src/package.json /w/ && cd /w &&
     cp /src/package-lock*.json . 2>/dev/null; [ -f package-lock.v3.json ] && mv package-lock.v3.json package-lock.json;
     npm audit --json' > "tests/fixtures/npm/$p/npm-audit.json"
done

# 2. The matching OSV subset, from a fresh full export
curl -o /tmp/all.zip https://osv-vulnerabilities.storage.googleapis.com/npm/all.zip
python scripts/refresh_audit_fixtures.py /tmp/all.zip
```

Then update the expected pair counts in `tests/test_against_npm_audit.py` if they
changed.
