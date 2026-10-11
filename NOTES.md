# Design notes

A running log of design decisions: what was chosen, what it was weighed against,
and why. Newest stage at the bottom.

---

## Stage 1: Scaffold

### Tooling: uv rather than pip-tools or Poetry
One tool manages the Python version, the virtualenv, and a cross-platform lockfile
(`uv.lock`). Installs are fast enough that CI and Docker builds don't need clever
caching to stay quick. `uv sync --frozen` fails if the lockfile is stale, so CI,
Docker and local runs install exactly the same versions.

### Python 3.13 (not 3.14)
3.14 was installed locally, but 3.13 has a longer track record for C-extension wheels
(asyncpg, uvloop) and the `python:3.13-slim` base image. Nothing here needs 3.14
features. Revisit later; it is a one-line change in `.python-version`, `pyproject.toml`
and the Dockerfile.

### SQLAlchemy 2.0 async + asyncpg (not raw asyncpg)
- Alembic autogenerate works from SQLAlchemy metadata, so the ORM pays for itself
  in migrations alone.
- asyncpg is still the driver, so we keep its performance.
- The graph queries (recursive CTEs) will be written as explicit SQL through the
  same engine, so the ORM never hides the queries that matter.
- Trade-off: two abstractions to understand, plus async-ORM pitfalls (no implicit lazy
  loading). Mitigated with `expire_on_commit=False` and explicit eager loads.

### Configuration: pydantic-settings, env vars only
All settings live in one `Settings` class, are validated at startup, and are read
from env vars, with `.env` only as a local convenience. DSNs are `SecretStr`, so they
never appear in reprs or logs (there is a test for this). The Alembic URL comes from
the same settings, never from `alembic.ini`, so the app and migrations can't target
different databases.

### Logging: structlog, JSON in containers
- Stdlib loggers (uvicorn, SQLAlchemy, Alembic) go through the same processor chain,
  so there is one format across the whole process.
- JSON on stdout is what Cloud Logging parses natively; local host runs can use the
  console renderer (`LOG_JSON=false`).
- A pure-ASGI middleware binds `request_id` (honouring incoming `X-Request-ID`) to
  every log line in the request and echoes it on the response. It is pure ASGI
  rather than `BaseHTTPMiddleware` because the latter has known contextvar and
  streaming quirks.
- Gotcha hit during setup: Docker Compose substitutes variables from `.env`, so a
  host-oriented `LOG_JSON=false` leaked into the containers. Containers now hardcode
  JSON so they always log like production.

### App factory + lifespan
`create_app(settings)` builds the app, and the DB engine and Redis client are created
in the lifespan and closed on shutdown. Tests pass their own settings, nothing
connects at import time, and Cloud Run instance shutdown closes pools cleanly.
Uvicorn runs it with `--factory`.

### Health: liveness vs readiness
`/health` never touches dependencies, so a database blip can't get healthy
containers restarted (that would turn an outage into a restart storm). `/health/ready`
checks Postgres and Redis with timeouts and returns 503 with per-dependency detail.

### Compose: one-shot `migrate` service
The API starts only after `alembic upgrade head` exits 0. This mirrors the planned
production shape: migrations run as a separate Cloud Run Job before a deploy, not
inside every app instance's startup, where concurrent instances would race.

### Versions: Postgres 17, Redis 7.4
Matched to what Cloud SQL and Memorystore offer, so local, CI and production run
the same majors.

### Container image
Multi-stage build: dependencies are installed in a layer separate from source (fast
rebuilds), the package is installed non-editable so the runtime stage needs only the
venv, and the image runs as a non-root user. `exec uvicorn` makes it PID 1, so it
receives Cloud Run's SIGTERM directly.

### Testing approach
Tests use `httpx.ASGITransport` against the real app (lifespan included) and real
Postgres/Redis, which are compose locally and service containers in CI. Mocks are
reserved for things we don't own (the npm registry, from stage 4).

### CI
There are three jobs: lint (ruff check + format), test (migrations apply, then pytest,
against Postgres 17 / Redis 7.4 service containers) and docker (the image builds).
Building the image in CI catches Dockerfile breakage before deploy day.

---

## Stage 1, step 2: Graph schema

### Storing the graph: adjacency table + recursive CTE
"Adjacency table vs recursive CTE" is a false choice: a recursive CTE is how you
*query* an adjacency table. The real alternatives were:

| Option | Why not (for now) |
|---|---|
| Closure table (every ancestor→descendant pair) | Roughly quadratic rows per scan, heavy writes, and it still can't return the paths themselves, which is the main query. |
| Materialized path / `ltree` | Assumes a tree. npm graphs are DAGs with shared nodes, plus cycles. |
| Separate graph DB (Neo4j, Apache AGE) | A second datastore to run, back up and keep consistent, with no transaction spanning both. Per-scan graphs are ~1–5k nodes, which an indexed recursive CTE traverses in milliseconds. |

**Revisit when** we need graph algorithms across many scans. Even then, "which scans
contain X?" is answered by an index on `scan_nodes.version_id`, with no traversal.

### Global catalogue vs per-scan graph
`packages` and `package_versions` are global and deduplicated, because a published npm
`name@version` is immutable. `scan_nodes` and `scan_edges` are per scan, because
*which* version `^4.17.0` resolved to is a fact about one lockfile, not about the
package.

### Node identity: name@version (not install path)
A lockfile can install `x@1.0.0` at several `node_modules` paths. We collapse those
into one node per scan and keep every path in `install_paths`.
- **Gain:** duplicate subtrees are stored once. "Is X in this project?" is one row.
- **Known inaccuracy:** if two copies of `x@1.0.0` resolve the same dependency
  differently (nested `node_modules` overrides), the merged node gets both edges, and
  path queries can report a path that doesn't physically exist. This is rare, the
  over-report is conservative for security use, and `install_paths` preserves the
  ground truth if we ever need exact per-path resolution.

### Non-registry nodes (a change from the original proposal)
The first sketch used `version_id IS NULL` to mean "root". Lockfiles also contain
workspace packages, `git:` dependencies and `file:` tarballs, none of which has an
immutable registry `name@version`. So `scan_nodes.source` is one of `root | registry
| workspace | git | file | remote`:
- registry nodes point at `package_versions`;
- all other nodes carry their own `name` and `ref` (a path or URL).

A CHECK constraint enforces exactly one of those two shapes, and a partial unique
index allows exactly one root per scan.

### Integrity enforced in the database, not just in Python
- **Composite FKs** `(scan_id, parent_node_id) → scan_nodes(scan_id, id)` make it
  impossible for an edge to connect nodes from two different scans.
- **Cascades:** deleting a scan deletes its nodes and edges. `package_versions` are
  `RESTRICT`, so the shared catalogue can't lose rows that a scan still uses.
- Status, source and dependency-type columns are **text + CHECK** rather than Postgres
  ENUMs. Changing an ENUM (`ALTER TYPE ... ADD VALUE`) has transaction caveats, while
  a CHECK is a plain constraint swap.

### Edges
- PK is `(scan_id, parent_node_id, child_node_id, dep_type)`. Its prefix serves
  forward traversal. A second index, `(scan_id, child_node_id)`, serves the reverse
  walk (target → root) that the "paths to package X" query will use.
- `dep_type` is in the PK because a parent can declare the same child twice, for
  example as both a dependency and a peer dependency.
- Cycles are just rows. Traversal will use Postgres's `CYCLE` clause to terminate.

### Keys
- UUID for `scans`, because scan IDs appear in URLs and shouldn't be enumerable.
- `bigint` everywhere else keeps the edge table (the largest by far) narrow.

### Deliberately not here yet
- Registry metadata (license, deprecation, publish time): added in step 5, when we
  know what the builder uses.
- Semver-aware ordering: versions are raw text. Ordering happens in Python.
- `integrity` is stored first-seen. Detecting a mismatch across scans would be a
  useful tampering signal later.

### Tests and tooling
- Schema tests run against real Postgres, because the rules under test (partial
  unique index, composite FKs, cascades) only exist in the database.
- Each test runs inside a transaction that is rolled back (savepoint mode), so tests
  never see each other's rows.
- `alembic check` runs as a test: editing a model without generating a migration
  fails CI.
- The constraint naming convention uses all column names (`column_0_N_name`), so
  `uq_packages_ecosystem_name` says what it covers.
- New migrations are auto-formatted with ruff through an Alembic post-write hook.

### Dev tooling runs in a container
Smart App Control (enforcing on the dev machine) blocks the unsigned
`.venv\Scripts\python.exe` launcher that every Windows virtualenv uses. Turning it
off is irreversible on Windows 11, so instead `make test|lint|fmt|migrate|revision`
run in a `dev` image (Dockerfile `dev` stage, compose `dev` profile) with the repo
bind-mounted. Side benefits: Docker is the only host prerequisite, and tests see the
same Linux/Python as CI and production. The venv lives at `/opt/venv`, outside the
mount, so a host `.venv` can't shadow it. `LOCAL=1` opts back into the host venv.

---

## Stage 1, step 3: package-lock.json parser

### Pure function, install-path graph
`parse_package_lock(raw) -> LockfileGraph` performs no I/O and no logging. It
returns frozen dataclasses: nodes keyed by install path, typed edges, and a list of
unresolved dependencies. Keying by install path means the parser reports exactly
what the lockfile says. Collapsing to one node per `name@version` is a *storage*
policy (step 2's schema), applied in one place by the graph builder.

### v2 and v3: read `packages`, ignore the legacy tree
v2 carries both the modern `packages` map and npm 6's nested `dependencies` tree.
v3 has only `packages`. Reading `packages` for both gives one code path, and the
fixture tests assert that npm's v2 and v3 output for the same project parse to
identical graphs. v1 (npm <= 6) is rejected with a message that says how to
regenerate the lockfile.

### Edges are inferred with Node's module resolution
The lockfile doesn't record which entry satisfies a dependency. A dependency `x`
of the package at `P` resolves to the first `D/node_modules/x` for `D` in `P` and
each ancestor of `P`. This one rule handles hoisting, nested version conflicts and
workspace-local `node_modules`. Link entries (`"link": true`) are followed to
their target directory and are not nodes themselves.

### Rules that are easy to get wrong
- An entry's `name` field overrides the folder name. That is how npm aliases
  (`"x": "npm:lodash@^4"`) record the real package.
- `optionalDependencies` override same-named `dependencies` (npm semantics).
- A peer becomes `peer_optional` via `peerDependenciesMeta`.
- `devDependencies` only count for the root and local directories. An installed
  package's dev dependencies are never installed.
- Workspaces are implicit dependencies of the root, so we add root → workspace
  edges unless the root already declares the workspace (no duplicate edge, which
  would violate the edge primary key).

### Source classification by URL shape
A `resolved` URL counts as a registry package if it looks like
`.../<name>/-/<name>-<version>.tgz` *and* the name and version match the entry.
This covers private registries (Artifactory, Verdaccio) without a host allowlist,
and a registry-looking URL for a *different* version is not trusted (→ `remote`).
A missing `resolved` means registry: npm can omit it, and bundled dependencies
have none. `git+`/`github:` → git, `file:` → file. A linked directory is a
workspace if it matches the root's `workspaces` globs, otherwise file.

### Inconsistency is data, not an exception
A dependency nothing satisfies becomes an `Unresolved` record with
`required = False` for optional dependencies and optional peers, which are
legitimately absent. Only malformed input or an unsupported version raises. A
scan of a slightly inconsistent lockfile should still produce a graph plus
warnings, not a 500 error.

### Fixture and test strategy
- `tests/fixtures/npm/realworld`: a realistic Express + React + Vite
  `package.json`, with lockfiles generated by npm 10 in `node:22-alpine`
  (`npm install --package-lock-only --lockfile-version=2|3`). That is genuine npm
  output, about 390 packages including nested duplicates (`debug@2.6.9` at several
  paths), optional platform binaries and optional peers. Regenerate it the same
  way for later fixtures.
- Unit tests use one minimal lockfile per rule.
- The tests were mutation-checked: deliberately breaking each rule (walk-up,
  optional override, alias names, dev scoping, link following, implicit workspace
  edges) fails at least one test. The one mutation that survived was a defensive
  branch for lockfile keys that can't exist, which was deleted rather than kept
  untested.

---

## Stage 1, step 4: npm registry client

### Why it exists (the graph doesn't need it)
The lockfile already pins every resolved version, so building the graph needs no
network. The client is for what the lockfile *doesn't* contain:
- every published version of a package, for stage 2's "minimum fixed version";
- deprecation notices;
- registry integrity hashes, to cross-check against the lockfile.

It is built lean around those uses.

### Abbreviated packuments (and the size claim, measured)
We request `application/vnd.npm.install-v1+json`, the document npm itself
installs from. My planning estimate of "10–100× smaller" was wrong. Measured on
2026-10-11:

| package | full | abbreviated | what we cache (trimmed + zlib) |
|---|---|---|---|
| typescript (3,850 versions) | 15.8 MB | 8.7 MB | 273 KB |
| express (290 versions) | n/a | 0.3 MB | 21 KB |

The abbreviated format roughly halves the download. The big win is caching only
the fields we use (version → integrity, deprecated), compressed, which is about
30× smaller than the download. The cache key carries a format version (`v1`), so
adding fields later (e.g. per-version dependencies for stage 2's parent-upgrade
suggestions) just ignores old entries.

### Layering
`get_packument(name)`: single-flight → Redis cache → retry → rate limit → HTTP.
- **Single-flight:** concurrent calls for the same package share one `asyncio.Task`
  (`shield`ed, so one caller's cancellation doesn't cancel it for the others). A
  lockfile build can ask for the same package from many places at once.
- **Cache freshness:** an entry is served without a request for 1 hour. After that
  it is revalidated with `If-None-Match` (a 304 has no body). Entries are kept
  7 days so revalidation stays possible. 404s are cached for 5 minutes. Everything
  is set by env var.
- **Redis is optional:** any `RedisError` is logged as `npm_cache_unavailable` and
  we fetch directly. A cache outage must not become a scan outage.

### Retry policy
- Retried: 429, 5xx, and transport errors (connect, read, timeout).
- Not retried: 404 (`PackageNotFoundError`, negatively cached) and other 4xx.
  Repeating a bad request won't fix it.
- Delay is `Retry-After` when present (seconds or HTTP date), otherwise
  exponential backoff with **full jitter**, `uniform(0, min(cap, base·2ⁿ))`. Jitter
  matters because many clients that failed together would otherwise retry
  together. There is no sleep after the final attempt.
- The semaphore is released *before* sleeping, so a backing-off request doesn't
  hold a concurrency slot.

### Rate limiting: semaphore + token bucket, per process
The concurrency cap bounds open connections. The token bucket bounds request
*rate* and smooths bursts, serving callers in FIFO order. Both limits apply per
process, so N Cloud Run instances can send N× the configured rate. A shared Redis
limiter is the upgrade path, deferred until we actually run more than one worker.

### Bug found by the tests: float rounding in the token bucket
After sleeping exactly `(1 − tokens)/rate`, the refill can come to 0.9999999
tokens. The original `while tokens < 1` loop then slept ~1e-17 s repeatedly: an
infinite loop under a fake clock, and a busy-spin in production. The fix takes
the token after the computed wait without re-checking, with a regression test.

### Testing approach
- HTTP: `httpx.MockTransport` with a scripted queue of responses and exceptions.
  No extra dependency, and CI never touches the network.
- Time: one fake clock drives cache freshness, `Retry-After` dates, the token
  bucket and `sleep`, so backoff tests take no wall time and assert exact delays.
- Redis is real, with a unique key prefix per test, cleaned up afterwards.
- One `@pytest.mark.live` smoke test hits registry.npmjs.org and is skipped unless
  `NPM_LIVE_TESTS=1`.

### Package-name validation
Names are checked before they reach a URL: length, leading `.`/`_`, scope shape,
whitespace, and `\ ? # %`. This is looser than npm's rules for *new* names, because
legacy packages have capitals, but strict about anything that could change the
request path. Scoped names are sent as `@scope%2Fname`.

### Shared Redis client is now bytes-mode
The cache stores zlib-compressed bytes, so the app's Redis client switched to
`decode_responses=False`. Only the health check used it before, and `PING` works
either way.

---

## Stage 1, step 5: Graph builder

### Two parts: pure collapse, then persistence
- `graph/collapse.py` (pure) turns the parser's install-path graph into one node
  per identity. This is the only place the identity policy from step 2 lives:
  - registry → `(name, version)`;
  - root / workspace / local directory → its path;
  - git / remote / file tarball → its resolved URL (one commit at two paths is
    one node).
- `graph/builder.py` writes the collapsed graph and runs the scan status machine.

### Merge rules
- A merged node is dev, optional or peer only if **every** copy is: one
  production copy means the package ships.
- Duplicate edges (the same dependency seen from two copies of its parent)
  collapse into one.
- Copies of the same `name@version` with *different* integrity hashes produce an
  `integrity_conflict` warning. A copy with no hash is not a conflict.

### Warnings are stored on the scan (`scans.warnings`, JSONB)
Required-but-unresolved dependencies, integrity conflicts, and **cross-scan
integrity mismatches** are recorded as warnings. A mismatch means this lockfile's
hash for `name@version` differs from the first-seen catalogue value. Since npm
versions are immutable, that is a tampering signal, and it costs nothing to
detect during the upsert. Optional dependencies that are legitimately absent are
not reported (noise). The column was added with a constant default, which on
Postgres 11+ is a metadata-only change (no table rewrite).

### Status lifecycle and atomicity
`pending → processing → completed | failed`.
- The graph and the `completed` update share one transaction.
- On any exception, that transaction rolls back completely, and a *separate*
  transaction records `failed` and the error (a test proves no partial graph
  remains).
- `run_build` re-raises after recording the failure, so the API or worker
  decides how to surface it.
- The stored error text never includes SQL: SQLAlchemy's `DBAPIError` puts the
  statement and its parameters into its message, so those get a generic message
  and the detail goes to the server log only.

### Bulk writes: one `INSERT … SELECT FROM unnest(arrays)` per table
- The first version used `executemany`. Profiling showed per-row round trips
  dominating, so each table is now written in one statement with one array
  parameter per column.
- `install_paths` (itself an array) travels as a JSON string, because Postgres
  multi-dimensional arrays must be rectangular.
- `RETURNING` order isn't guaranteed, so node ids are mapped back by each node's
  per-scan natural key (`version_id`, or `(source, ref)`), which are exactly the
  unique constraints.

### Concurrency
- Catalogue rows are upserted (`ON CONFLICT DO NOTHING` / conditional
  `DO UPDATE`), so concurrent scans sharing new packages never duplicate rows.
- Inserts go in sorted key order, so two transactions lock rows in the same order
  and can't deadlock.
- Tested with 4 concurrent builds racing to insert the same 30 new packages.

### Performance, measured (385 nodes / 570 edges)
| setup | build time |
|---|---|
| executemany, host → Docker Desktop | ~450 ms |
| unnest, host → Docker Desktop | ~250–370 ms |
| unnest, inside the Docker network | ~140–230 ms |

`EXPLAIN ANALYZE` of the edge insert: 31 ms total, of which 3 ms is the insert and
28 ms is FK-check triggers. That led to dropping the **redundant** direct FK
`scan_edges.scan_id → scans`. The two composite FKs through `scan_nodes` already
guarantee the scan exists, and deletes cascade scans → nodes → edges. The rest
of the gap is Docker Desktop port forwarding and Postgres running in the WSL2 VM.
This is fast enough to build synchronously at this size. The decision is
revisited in step 6.

### The builder does not call the npm registry
Enriching a scan from the registry (deprecations, an integrity cross-check) would
be one request per package, ~385 for the fixture, on every scan. The registry
client's consumer is stage 2's fix suggestions. The cross-scan integrity check
above gives a cheap tampering signal without the network.

---

## Stage 1, step 6: Scan API

### Endpoints
`POST /scans`, `GET /scans/{id}`, `GET /scans/{id}/graph`,
`GET /scans/{id}/paths?package=&version=&max_paths=&max_depth=`.

### Upload: raw lockfile as the body
`curl --data-binary @package-lock.json` is the whole client. Multipart adds
nothing for a single JSON file. The body is streamed with a hard byte limit
(`MAX_LOCKFILE_BYTES`, 25 MB), checked against `Content-Length` *and* while
reading, because chunked uploads carry no length. The OpenAPI schema documents
the raw body explicitly.

### Validate before creating anything
The lockfile is parsed *before* a scan row exists, so an unparseable upload is
a `422` and leaves nothing behind. Parsing is CPU-bound, so it runs in
`asyncio.to_thread` to keep the event loop responsive.

### Synchronous build (for now)
The build takes ~150–400 ms for ~400 packages (step 5 measurements), so
`POST /scans` builds inline and returns `201` with the completed scan.
Trade-off: very large monorepo lockfiles would hold a request for seconds. The
status machine (`pending → processing → completed | failed`) already supports
moving the build to a worker and returning `202`, without changing the schema
or the read endpoints. Do that when (a) p95 build time or (b) Cloud Run request
timeouts make it necessary.

### Status codes
- `404` for an unknown scan, `422` for a malformed UUID or lockfile, `413` for
  too large.
- `409` when reading the graph or paths of a scan that isn't `completed`. The
  resource exists, but its graph isn't available.
- `500` if the build fails. The scan is still recorded as `failed` with a safe
  error message.

### Path queries: CTE to prune, A* to enumerate
1. **SQL (recursive CTE):** the set of ancestors of the target nodes. `UNION`
   (not `UNION ALL`) discards revisits, so cycles terminate. It walks the
   `(scan_id, child_node_id)` index. Only edges into that set are loaded.
2. **Python (`graph/paths.py`, pure):** enumerate simple root→target paths.

Why not do it all in one recursive CTE? Shortest-first output needs
`ORDER BY length`, which forces Postgres to materialize *every* path before
returning any, and path counts are exponential. The cap (`LIMIT`) and the order
can't both be honoured cheaply in SQL.

**First attempt, plain BFS, failed its own test.** On 30 stacked diamonds (2³⁰
equal-length paths), BFS has to hold every partial path of length L before
completing one of length L+1, so it hit the work budget with **zero** results.
The fix is a **best-first (A\*) search**:
- Partial paths, grown backwards from the targets, are ranked by
  `f = edges so far + exact distance from root to the head node` (one forward
  BFS precomputes these).
- That bound never overestimates, so completed paths come out in
  non-decreasing length: still shortest first, provably.
- Ties go to the deepest partial path, so the search runs depth-first along
  optimal routes. Each extra path costs about 4.6 expansions (measured), so
  work is proportional to the paths *returned*.
- Heads with no route to the root are pruned before costing anything.

There are three caps: `max_paths` (default 100, hard max 1000), `max_depth` (64)
and an expansion budget (200k). Requested caps are clamped to the server limits.
`truncated: true` means more paths may exist.

### Path representation
Each step is `{id, name, version, dep_type}`, where `dep_type` labels the edge
*into* that step (`null` for the root). A path through a `dev` or `peer` edge is
visible as such, which matters for "does this actually ship?".

### Known limitations
- Non-registry nodes (root, workspaces, git) have no stored version, so the root
  shows `version: null`. That needs a `scan_nodes.version` column for local
  nodes.
- Paths respect the collapse inaccuracy from step 2: a merged
  `name@version` node can join edges from different install paths.

---

# Stage 2: Vulnerability detection

## Step 1: OSV advisory ingestion

### The dataset, measured before designing (npm export, 2026-10-11)
`all.zip` is 218 MB with **230,283 records**:
- **7,837 vulnerabilities** (GHSA/CVE).
- **222,446 malicious-package reports** (`MAL-*`, from OpenSSF's malicious-packages
  project), about 97% of the export.

What matters for the design:

| Fact | Consequence |
|---|---|
| Raw JSON is 386 MB, 362 MB of it MAL `details` prose | `raw` is the only copy of `details`; no duplicate column |
| Range types: `SEMVER` 219k, `ECOSYSTEM` 33 | both use npm semver semantics |
| Events: `introduced`, `fixed`, `last_affected`; **no `limit`** in practice | the matcher must handle all three |
| 14.5k affected entries list explicit `versions` only, no ranges | the matcher must check the list, not just ranges |
| One advisory can list the **same package in several affected entries** (GHSA-27f5: four) | match = OR across entries |
| Multi-ecosystem records (EEF-CVE-2026-56812: Hex + npm) | keep only `npm` entries |
| 1,140 bounds are prereleases (`1.2.0-rc.0`) | SemVer 2.0 precedence, tested in step 2 |
| 730 withdrawn | stored with `withdrawn`, excluded from matching |
| 1,289 have only a CVSS **v4** vector | GHSA's curated severity label is the primary source |
| 6 records contain `\u0000` | Postgres JSONB rejects U+0000; replaced with U+FFFD |
| 0 records failed to parse | the normalizer is right for real data |

### Decision: ingest malicious-package reports too
You asked for vulnerability detection, but for a supply-chain platform a
known-malicious package version (credential-stealing typosquats with install
hooks) is the highest-value signal there is, and it uses the same range-matching
machinery. `kind` (`vulnerability | malicious`) separates them, and
`OSV_INCLUDE_MALICIOUS=false` turns them off. Malicious packages get severity
`CRITICAL` by policy: installing one runs attacker code, whatever a CVSS score
would say. **Flagged for you to revisit.**

### Schema
- `advisories`: one row per OSV id. Normalized `kind`, `severity`,
  `cvss_v3_vector`/`cvss_v3_score`, `cvss_v4_vector`, timestamps, `raw` (the full
  record), and `last_changed_run_id`.
- `advisory_aliases (advisory_id, alias)`: CVE/GHSA cross-references, indexed by
  alias, so "is CVE-2021-23337 in my project?" is a lookup.
- `advisory_affected`: one row **per affected entry**, with `(ecosystem, package)`
  indexed, `ranges` as JSONB (OSV events verbatim) and `versions text[]`.
- `advisory_sync_runs`: an audit log of every load or sync, with counts, the
  `cursor` (max `modified` applied) and the source ETag.

### Trade-off: range events as JSONB, not rows
Postgres has no npm-semver type (the `semver` extension isn't available on Cloud
SQL), so "is 4.17.20 in [0, 4.17.21)?" can't be a SQL predicate however the
events are stored. Rows would buy nothing and would lose the meaningful event
order. What SQL *should* do is the cheap pre-filter, `package = ANY(scan
packages)`, which the `(ecosystem, package)` index serves. Semver evaluation then
happens in Python on that small candidate set (step 2). The alternative,
precomputing every affected `name@version` by expanding ranges against registry
version lists, is in effect what step 4's findings table does, but only for
versions that actually appear in scans.

### Severity
1. Malicious → `CRITICAL`.
2. Otherwise GHSA's curated `database_specific.severity` (`MEDIUM` normalized to
   `MODERATE`). It exists even for v4-only records.
3. Otherwise derived from the **CVSS v3.1 base score**, computed from the vector
   by `advisories/cvss.py`, an implementation of the spec formula including
   v3.1's float-safe Roundup.
4. Otherwise `UNKNOWN`.

CVSS v4 vectors are stored but not scored: v4 uses a lookup-table model, and every
v4-only record has a GHSA label anyway. One CVSS reference value in my own test was
wrong (I wrote 9.0 for `AV:N/AC:H/PR:L/UI:N/S:C/C:H/I:H/A:H`). Hand-computing the
formula gives 8.5, which the code returned.

### Ingestion mechanics
- The download is **streamed to a temp file**, never held in memory. Its
  **MD5 is verified against GCS's `x-goog-hash` header**: a supply-chain tool
  should verify its own supply chain. A mismatch fails the run.
- Records are upserted in **committed batches** (1,000): progress survives a
  crash, and no 230k-row transaction holds locks and WAL for minutes.
- **Idempotent and monotonic:** `ON CONFLICT … DO UPDATE … WHERE
  advisories.modified < EXCLUDED.modified`. Re-running is a no-op, and an older
  export can never roll data back. `RETURNING (xmax = 0)` separates inserts from
  updates (a known Postgres idiom).
- For changed advisories only, aliases and affected rows are deleted and
  re-inserted. Each statement uses the `unnest(arrays)` pattern from stage 1.
- Unparseable records are logged and skipped, never fatal.
- A **separate test database**: tests now always run against `<db>_test`, created
  on demand, because a dev database holding a real 230k-advisory load must never
  be touched by test cleanup.

### Measured
- **Full load: 230,283 records in 1 min 54 s** (~2,000/s) from the local archive
  into Docker Postgres. That gave 233,062 affected rows and 38,869 aliases.
- **Size: `advisories` 427 MB**, `advisory_affected` 64 MB, aliases 7 MB.
- **LZ4 had almost no effect, and why:** Postgres only compresses values once a
  row exceeds ~2 KB (TOAST threshold). The average record is 1.7 KB, so 76% of
  rows are never compressed, and those that are compress only ~1.4× (JSONB's
  binary form is bigger than the text, and MAL `details` is prose). Forcing
  compression (`toast_tuple_target` plus a full rewrite) might save ~30%. At
  Cloud SQL storage prices that is cents a month, so I stopped there.
