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

## Step 2: Semver range matcher

### Two pure modules
- `advisories/semver.py`: SemVer 2.0.0 parsing and **precedence** (spec s11).
  It uses the official regex plus npm's tolerated `v`/`=` prefixes. Comparison
  is a precomputed key: `(major, minor, patch, release-flag, prerelease ids)`.
  - A release sorts above all of its prereleases.
  - Numeric identifiers sort below alphanumeric ones, numbers compare
    numerically, and a shorter prefix sorts first.
  - Build metadata is ignored for ordering *and* equality.
- `advisories/matcher.py`: OSV's evaluation algorithm. Sort the events, then walk
  them:
  - `introduced` sets affected (if v ≥ bound, or the bound is `"0"`);
  - `fixed` and `limit` clear it (if v ≥ bound);
  - `last_affected` clears it (if v > bound).

  An entry matches if the explicit `versions` list contains v **or** any
  SEMVER/ECOSYSTEM range matches. GIT ranges are ignored (commit hashes can't
  apply to registry versions).

### Precedence, not npm's `satisfies()`
node-semver's `satisfies("1.5.15-beta", "<1.5.15")` is false. npm excludes
prereleases from ranges unless the range names that exact `major.minor.patch`.
That is an *install policy* ("don't surprise me with a beta"). A vulnerability
range asks a different question: is this build *ordered before the fix*? By
precedence 1.5.15-beta < 1.5.15, so it **is** affected. OSV specifies SEMVER
ranges by precedence. Using `satisfies` semantics would silently miss
vulnerable prereleases.

### Decisions on edge cases
- **Events at the same version:** `introduced` sorts before `last_affected`
  before `fixed`. So `introduced=X, fixed=X` is empty, and
  `introduced=X, last_affected=X` is exactly `{X}`. Those are the only sensible
  readings, and both are tested.
- **Unparseable bounds raise `InvalidRangeError` rather than being skipped.**
  Dropping a bad `fixed` would widen a range (false positives); dropping a bad
  `introduced` would hide it (false negatives). The caller decides and counts.
- **Unknown event types are ignored**, per OSV's forward-compatibility rule.
- **Installed versions are parsed strictly.** **Advisory bounds** additionally
  accept all-numeric shorthand (`13.0` → 13.0.0, `2026.04.1` → 2026.4.1), like
  node-semver's `coerce`.

### Found by running the matcher over the real corpus
All 1.16M evaluations (every affected entry × 5 probe versions) were run against
the full 230k-record export. **Every SEMVER range parsed strictly.** 165
evaluations raised: all from the 33 ECOSYSTEM ranges and 2 versions lists, with
bounds like `'13.0'`, `'1.3'`, `'2026.04.1'`. npm can't publish such versions,
so they are shorthand; adding coercion for bounds took errors to **0**.

### Testing: three independent angles
1. **Hand-picked edge cases:** the spec's own precedence chain, numeric vs
   lexical (`beta.2 < beta.11`), case order, prefixes, build metadata, >64-bit
   numbers, inclusive/exclusive bounds, multi-interval ranges, unsorted events,
   tie-breaks, prerelease bounds, versions-only entries, and ECOSYSTEM vs GIT.
2. **Real advisories:** lodash, minimist (two ranges), a `last_affected` record,
   the four-entry versions-only record, and a malicious package, each checked on
   both sides of its boundary.
3. **Property tests (Hypothesis):**
   - the ordering is a strict total order and transitive;
   - parse/str round-trips;
   - a prerelease sorts below its release;
   - **the event-walk agrees with an independent oracle** that builds explicit
     `[start, end)` intervals and tests membership;
   - a single introduced/fixed pair is exactly the half-open interval.

**Mutation check:** each of 10 deliberate bugs is caught by at least one test.
They were: fixed inclusive, last_affected exclusive, wrong tie order, release
below prerelease, text before numeric ids, numeric ids compared as text, no event
sort, build metadata compared, versions list ignored, ECOSYSTEM not evaluated.

## Step 3: Incremental sync worker

### Change detection from OSV's own index
OSV publishes `npm/modified_id.csv` (~8.7 MB): every record id with its
`modified` timestamp, **newest first**. One sync works like this:
1. `GET modified_id.csv` with `If-None-Match: <last CSV ETag>`. An unchanged
   export is a **304**: no body, no record fetches.
2. Stream it line by line and **stop reading** at the first record older than the
   cursor (minus the overlap). Cost scales with what changed, not with the
   230k-record database.
3. Fetch just those records (`npm/<id>.json`) with bounded concurrency (16) and
   retries, then upsert them through the **same idempotent, modified-guarded path**
   as the full load. A 404 for a listed record is logged and skipped.
4. New cursor = `max(old cursor, newest applied)`. **It never moves backwards**,
   and it is only advanced by a *completed* run, so a failed run resumes from the
   last good cursor.

### The overlap window (1 hour, configurable)
A record can appear in the export with a `modified` timestamp *older* than ones
already synced (pipeline delays). A strict `> cursor` scan would never see it.
The sync re-checks everything modified after `cursor - overlap`, and because
upserts are idempotent and modified-guarded, re-processing is free. Tests pin
both halves: `test_overlap_window_is_needed` (a strict scan misses the record)
and `test_overlap_window_rechecks_late_arrivals` (the sync applies it).

### Fallback to a full load
There is no cursor yet (fresh database), or the backlog exceeds
`OSV_INCREMENTAL_MAX` (5,000). One 220 MB streamed download beats thousands of
small requests, and the full load is equally idempotent.

### One sync at a time: a Postgres advisory lock
`pg_try_advisory_lock(<fixed key>)` is taken on a dedicated connection for the
sync's duration. It is non-blocking: a second worker or a manual `sync` logs
"skipped" and returns. The lock lives in the database everything already shares,
so there is no extra coordination service. It is session-scoped, so if a worker
dies, Postgres drops its connection **and its lock** (tested by invalidating the
holding connection). A crashed worker can't wedge syncing.

### Worker vs job
- `cli sync`: one-shot. This is the **Cloud Run** shape: a Cloud Run Job
  triggered by Cloud Scheduler, which costs nothing between runs.
- `cli worker`: a loop (`sync`, then sleep `OSV_SYNC_INTERVAL_S`, default 1 h).
  This is the **docker-compose `worker` service** you asked for. A failed sync is
  logged and retried next interval; it never kills the worker.
- **SIGTERM** sets a stop event. The worker finishes the current sync (never
  stopping mid-batch) and exits immediately if it is sleeping. Measured
  `docker compose stop worker`: **1.2 s, exit 0**.

### What changed is tracked
Every run, including 304 no-ops, writes an `advisory_sync_runs` row (mode,
status, seen/inserted/updated/unchanged, cursor, ETag, error).
`advisories.last_changed_run_id` marks exactly which advisories each run
changed. Step 4 re-matches existing scans against just those.

### Verified against the live OSV bucket
- The worker started on the dev database (cursor from the afternoon full load),
  ran an **incremental sync, listed 9 records in the overlap window, all
  unchanged, in 1.3 s** (vs 1 min 54 s for a full load).
- On restart, the CSV ETag matched: **304, 0.7 s, no record fetches**.

### Fix found while watching real logs
httpx logs every request at INFO, which would be thousands of lines per large
sync. It is now set to WARNING; our own structured events
(`advisory_sync_completed` with counts, `npm_registry_retry`, ...) carry what
matters.

## Step 4: Matching scans against advisories

### `findings` table
One row per (scan node, advisory), where a scan node is one `name@version` in one
scan.
- A composite FK to `scan_nodes(scan_id, id)` keeps a finding inside its own scan,
  and deleting a scan cascades through nodes to findings.
- Unique on `(scan_node_id, advisory_id)`.
- `created_at` means **first detected**; `updated_at` means last confirmed.
- Severity, aliases and the rest come from the advisory by join. Paths and fix
  suggestions are derived on read (steps 5 and 6), so they can never go stale.

### One matcher, two scopes
- `match()` is **pure**: installed `(scan, node, name, version)` × affected
  entries → a set of matches. An advisory matches if **any** of its entries for
  the package does (real data repeats a package across entries).
  - An unparseable range in one entry doesn't hide matches from the others.
  - Unevaluable ranges and versions are counted and logged, never guessed.
- SQL narrows the candidates: `advisory_affected.package = ANY(:names)` on the
  `(ecosystem, package)` index, withdrawn advisories excluded. Python evaluates
  semver on what's left.
- **`match_scan(scan)`**: every advisory against one scan. It runs at upload,
  right after the graph build, and sets `scans.matched_at`.
- **`rematch_advisories(ids)`**: these advisories against **every completed
  scan**. This is how a newly published advisory reaches scans uploaded before it
  existed, how a narrowed range drops findings, and how a **withdrawal** removes
  them (the candidate query excludes withdrawn advisories, so reconciliation
  deletes their findings).

### Reconciliation: upsert plus scoped delete, not delete-all-and-reinsert
Within a scope (one scan, or a set of advisories), findings not in the new match
set are deleted (`NOT EXISTS` against an `unnest` of the matches), then the
matches are upserted (`ON CONFLICT … DO UPDATE SET updated_at`). Existing
findings keep their `created_at`, so "first detected" survives every re-match,
which is what alerting on *new* issues needs later.

### Getting sync changes into scans, self-healing
Each sync marks changed advisories with `last_changed_run_id`. After the sync,
still under the advisory lock, **`rematch_pending()` processes every completed
run whose `rematched_at` is NULL** and then stamps it. If re-matching crashes,
the next sync picks those runs up again instead of their advisories silently
never reaching existing scans (tested by failing it once on purpose). A full
load changes ~230k advisories, so above 20k it switches to a full `match_scan`
per scan, which is cheaper than evaluating every advisory against every scan.

Upload-time matching is **best-effort**. If it fails, the scan and graph still
stand, `matched_at` stays NULL, and the next sync re-matches.

### Verified against reality: an exact match with `npm audit`
I uploaded the real-world fixture to the stack running the full 230k-advisory
database, then ran `npm audit` (GitHub's advisory database) on the same lockfile:

| | findings | package/advisory pairs |
|---|---|---|
| depgraph | 11 | vite ×3, vitest ×2, tinypool ×2, react-router ×2, esbuild, @vitest/mocker |
| npm audit | 11 | **identical set, identical severities** |

This is an end-to-end check of the OSV export, normalization, severity, the semver
matcher and the collapsed graph against an independent implementation. Step 8
turns it into a regression test.

## Step 5: Paths to each vulnerable package

No new algorithm: the bounded A* search from stage 1 (shortest first, simple
paths, capped, `truncated` flag) is reused with the finding's node as the target.

- **Refactor first:** the path-labelling logic moved out of the `/paths`
  endpoint into `graph.queries.labelled_paths()`, so the endpoint and findings
  share one implementation. The existing API tests passed unchanged, which shows
  the refactor preserved behaviour.
- **Single finding:** every root → vulnerable-package path, capped by
  `max_paths`/`max_depth`/expansions. Each hop carries the type of the edge *into*
  it, so `app -[dev]-> lodash` shows at a glance that the vulnerable code is
  dev-only.
- **Listing:** each finding carries its **shortest** path, computed in a batch.
  One ancestor CTE covers the union of every finding's node, then an in-memory
  `max_paths=1` search runs per finding. Sharing the parents map is safe because
  each search only walks up from its own target. Per-finding queries would be
  2 round trips × N findings. A test checks that the batched result equals the
  individual search for every finding.
- **Paths are derived on read, never stored,** so they can't drift from the graph
  or go stale after a re-match.

## Step 6: Fix suggestions

### Two different semver questions, two implementations
- **Matching** (step 2): is this version *ordered* inside the affected interval?
  That is precedence, so 1.5.0-beta < 1.5.0 is affected.
- **Fixing**: would *npm install* the fixed version, given these declared
  ranges? That is npm's `satisfies()`: caret/tilde/X-ranges/hyphen ranges/`||`,
  plus npm's prerelease policy (`^1.2.0` does not admit `1.3.0-beta`).

`advisories/npm_range.py` implements the latter and passes **158 cases taken from
node-semver's own range-include/range-exclude fixtures**. Non-semver specs (git
URLs, `file:`, dist-tags such as `latest`) return **None, not False**, so a
suggestion never claims a parent "blocks" a fix when we simply can't evaluate
its spec.

### Minimum fixed version: from real published versions
It is the lowest version above the installed one that is **published on npm**
and that **no entry of the advisory affects** (evaluated with the step 2 matcher).
It is not read off the advisory's `fixed` events, because:
- ranges can have several intervals and `last_affected` bounds;
- a `fixed` version may never have been published (tested: fixed 1.2.6 when the
  registry jumps from 1.2.5 to 1.2.8 gives 1.2.8).

Prereleases are skipped for stable installs, deprecated releases are skipped
unless nothing else fixes it, and unevaluable ranges are never recommended as
safe. This is the stage 1 npm registry client's real consumer (Redis-cached, so
repeat suggestions don't hit the registry). If the registry is down, it falls
back to the advisory's own `fixed` bounds, marked `source: "advisory"`.

`breaking` = the fix is outside `^installed` (caret semantics, so 0.2.x → 0.3.0
counts as breaking).

### Effort: who blocks the fix
The vulnerable node's parents come from the scan graph, with their declared
ranges. A parent **blocks** the fix if its range doesn't admit the fixed version
(or can't be evaluated).

| situation | action |
|---|---|
| nothing blocks: every declared range already admits the fix | `update_lockfile`: `npm update <pkg>`, no package.json change |
| only package.json blocks | `bump_direct` (flags semver-major bumps) |
| a parent package blocks | `upgrade_parent_or_override`, with the minimum parent upgrade and an `overrides` snippet |
| no published version fixes it | `no_fix` |
| known-malicious | `remove`, and rotate credentials (install scripts run at install) |

One subtle case is tested: a package can be **both direct and pinned by a
transitive parent**. Bumping package.json alone would leave the parent's copy
behind, so the action is driven by *who blocks*, not by "is it direct?".

### Minimum parent upgrade
For each blocking parent, find the lowest stable, non-deprecated release newer
than the installed one whose declared range for the vulnerable package admits
the fix, **or that dropped the dependency**. This needs per-version dependency
ranges, which the registry cache had been trimming away. The **cache-format
version key (`v1` → `v2`)** from stage 1 made that a non-event: old entries are
simply never read again, with no migration and no flush. It is deliberately
**one level deep**: it answers "which parent release stops pinning the
vulnerable version?". npm's full resolver is out of scope.

### Verified on the real fixture (live registry) vs `npm audit`
- Minimum fixed versions **match npm audit's vulnerable ranges exactly** (e.g.
  esbuild `<=0.24.2` → 0.25.0, @vitest/mocker `<=4.1.10` → 4.1.11), and the
  direct/transitive and breaking classifications agree.
- The parent upgrades are the same parents npm audit names, but **minimal**
  rather than latest: esbuild → **vite 6.2.0** (the release that moved to esbuild
  ^0.25), where npm audit says vite 8.3.4; tinypool → **vitest 4.0.0**, where npm
  audit says 5.0.3; react-router → **react-router-dom 7.18.0**.

### Test mistakes caught (my premises, not the code)
Two of my test expectations were wrong, and the code was right both times:
- with `fixed: 4.17.22-beta.1`, 4.17.21 is still affected (it sorts below the
  beta), so the answer is 4.17.23;
- `~1.2.5` *does* admit 1.2.6 (`>=1.2.5 <1.3.0`).

## Step 7: Findings API

- `GET /scans/{id}/findings`: most severe first (then CVSS, then name).
  - Filters: `severity` (repeatable, exact levels), `min_severity` (threshold) and
    `kind` (`vulnerability`/`malicious`). Invalid values are a 422 via `Literal`
    types.
  - `counts` per severity are **unfiltered**, so a UI can show totals while
    filtering.
  - `matched_at` says whether findings have been computed.
  - Each finding carries its batched shortest path and these flags:
    - `direct`: the shortest path is root → package.
    - `dev_only`: npm's own lockfile flag, meaning every install path goes
      through devDependencies. The real fixture showed why this matters: its
      CRITICAL tinypool RCE is reachable only via `vitest[dev]`, so it never
      ships to production. That is a different priority from a runtime
      dependency, and the flag makes it visible without reading paths.
- `GET /scans/{id}/findings/{finding_id}`: the advisory (details and references
  come from the stored raw OSV record), **all** paths (capped, `paths_truncated`)
  and the fix suggestion.
  - The npm client is created once in the app lifespan and injected with a
    FastAPI dependency. Tests swap in a `MockTransport` client through
    `dependency_overrides`, and a test proves the endpoint degrades to
    advisory-derived fixes when the registry returns 503.
- Responses use explicit Pydantic models, so the OpenAPI schema documents the
  contract and internal columns never leak.
- **Live check:** the detail endpoint for a real finding took ~0.9 s cold, which
  includes two live registry packument fetches for the package and its parent.
  Repeat calls hit the Redis cache.

## Step 8: Checked against reality

### The reference: `npm audit`
npm audit asks GitHub's advisory database about a lockfile. That is an independent
implementation of everything stage 2 does: advisory data, version matching,
transitive resolution. Matching it exactly is strong end-to-end evidence; a mismatch
would point at a specific bug.

### Fixtures, all real tool output
- `npm/vulnerable/`: an unmaintained API pinned to releases with famous advisories
  (express 4.17.1, lodash 4.17.20, axios 0.21.1, minimist 1.2.5, jsonwebtoken
  8.5.1, semver 5.7.1, …). npm 10 resolved it to 177 packages.
- `npm/realworld/`: the stage 1 fixture.
- Each has an `npm-audit.json` snapshot.
- `osv/audit-subset.zip`: **every** OSV record that mentions any package in either
  lockfile (352 records, 633 KB), including the many that don't affect the
  installed versions, so over-matching fails too.

### Results
| lockfile | packages | our (package, advisory) pairs | npm audit | agreement |
|---|---|---|---|---|
| vulnerable | 177 | 89 | 89 | **89/89, all severities equal** |
| realworld | 390 | 11 | 11 | **11/11, all severities equal** |

The first comparison was run live (full 230k database through the running API)
before the offline fixtures existed, and gave the same 89/89.

### Offline, deterministic CI test (`tests/test_against_npm_audit.py`)
- It loads the subset through the real ingester and uploads both lockfiles through
  the real API.
- It asserts the finding sets and severities **equal** npm audit's.
- **Cross-check with npm's own range strings:** for all 100 findings, the installed
  version must satisfy npm audit's vulnerable `range` (e.g. `<=4.17.20`), and our
  fixed version must not, both evaluated with our `npm_range`. That validates the
  matcher, the fix logic and the range parser against npm in one pass.
- Spot checks a human can verify (CVE-2021-23337 in lodash, CVE-2021-44906 in
  minimist, …).
- **It can fail:** making `fixed` bounds inclusive in the matcher fails 3 of its 7
  tests.
- Snapshots are frozen and regenerated together (`scripts/refresh_audit_fixtures.py`,
  `tests/fixtures/README.md`), so upstream advisory churn can't make CI flaky.

### Why both snapshots must be from the same moment
If npm audit is re-run later than the OSV subset was taken, a newly published
advisory appears on one side only, and the test fails for reasons unrelated to
our code. Regenerating both together keeps it a test of *logic*, not of timing.

---

# Stage 3: Web app and public demo

## Deployment decision (made before any code, recorded here)
This is **Option B, targeting about $0/month.** Postgres runs in Docker on GCP's
always-free `e2-micro` VM, with no public IPv4 (in-use IPv4 is billed). Cloud Run
reaches it over Direct VPC egress. There is **no Redis in production**: the
registry cache already degrades gracefully, and rate limiting can live in
Postgres. The API runs on Cloud Run (scale to zero), the advisory sync as a
Cloud Run Job triggered by Cloud Scheduler, and the frontend on Vercel Hobby.

The rejected alternative was Cloud SQL `db-f1-micro`, at about $10/month: managed
backups and patching, but over budget. Memorystore (about $25–35+/month) was out
of budget. **Nothing has been provisioned yet;** costs are re-confirmed before
step 7 creates anything.

## Step 1: Next.js app and typed API client

### Stack as installed (version-matched docs read first)
`create-next-app` installed **Next.js 16.4 / React 19.3**, newer than my training
data. Its generated `AGENTS.md` points to the docs bundled in
`node_modules/next/dist/docs`, which I read before writing code. Relevant changes:
- **Turbopack** is the default bundler.
- `next lint` is gone (ESLint CLI instead).
- **Cache Components** is enabled by default: uncached request-time data outside
  `<Suspense>` fails the build.
- `NEXT_PUBLIC_*` variables are inlined at build time.

Scan pages show per-user dynamic data, so they fetch from the browser. That suits
the direct-to-API design below and stays out of Cache Components' way.

### Typed API client: generated types plus a thin fetch wrapper
- `scripts/export_openapi.py` writes FastAPI's schema to `web/openapi.json`
  without a server or database. It is committed, so the web app builds without
  the backend.
- `openapi-typescript` generates `src/lib/api/schema.d.ts` from it: types only,
  no runtime code.
- `openapi-fetch` (~6 KB) type-checks every path, parameter and response body
  against those types. A renamed backend field breaks the frontend **at compile
  time**.
- **Drift checks in CI:** `export_openapi.py --check` fails if the committed
  schema doesn't match the API, and the web job fails if the regenerated types
  differ from the committed ones. This is the same idea as `alembic check`.
- *Rejected:* full client generators (orval, openapi-generator), which produce a
  lot of generated code to maintain for no gain here.

### Browser → API directly, so CORS is needed
A Next.js/Vercel proxy would avoid CORS, but Vercel caps proxied request bodies at
~4.5 MB, smaller than real lockfiles. So the browser calls the API itself.
`CORSMiddleware` allows **exact origins only** (`CORS_ORIGINS`, a JSON list in the
env var, defaulting to `http://localhost:3000`), GET/POST only, and **no
credentials**, since there are no cookies or auth. `Location` and `X-Request-ID`
are exposed. Tests cover the allowed origin, a foreign origin, and env parsing.

### Verified in a real browser
I served the production build (`next start`) and loaded it in headless Microsoft
Edge, which runs the JavaScript and performs the real cross-origin fetch. The
rendered DOM shows **"API ready (postgres: ok, redis: ok)"**. CORS headers were also
checked with curl: the frontend origin gets `access-control-allow-origin`, a
foreign origin gets nothing.

### The scaffold had 5 "high" npm audit findings: assessed, accepted
All five are **one advisory**, braces GHSA-vfj7-8cjw-p6xm (stack-exhaustion DoS
from deeply nested glob patterns):
- **no fixed version** of `braces` exists;
- it is reached **only via `eslint-config-next`**, a dev-time lint tool that
  globs our own patterns, never user input, and never ships to the browser;
- npm's suggested fix is downgrading `eslint-config-next` to 14.2.35, two majors
  back, which is worse than the issue.

Accepted and documented. **Dogfooding:** uploading `web/package-lock.json` to
depgraph itself gave the same conclusion independently: one HIGH finding,
`braces`, `dev_only=true`, path `web > eslint-config-next[dev] > … > braces`,
fix `no_fix`. npm audit counts 5 because it also lists every parent in the chain.

### Local environment issue found (not a repo change)
npm failed with `spawn C:\Program Files\PostgreSQL\18\bin ENOENT`. The Windows
**user-level `ComSpec` variable was set to the PostgreSQL `bin` folder** (probably
meant for `Path`). npm runs scripts through `ComSpec`. I worked around it per
command (`COMSPEC=C:\WINDOWS\system32\cmd.exe`) and left the system setting to the
owner.

### npm 11 install-script policy
npm 11 no longer runs dependency install scripts unless approved
(`allowScripts`). It skipped `unrs-resolver`'s postinstall, an optional native
speed-up for ESLint's import resolver; lint works without it. I left it
unapproved rather than approving scripts blindly, which is the supply-chain
hygiene this project is about.

### Fix: clean-checkout typecheck
The first CI run failed `tsc` with `Cannot find name 'LayoutProps'`. Next 16's route type helpers
(`LayoutProps`, `PageProps`) are generated into `.next/types` by `next typegen`. They existed
locally only because a build had already run. `typecheck` is now `next typegen && tsc
--noEmit`. It was reproduced locally from a clean `.next` before fixing: green locally is
not the same as green from a fresh checkout.

## Step 2: Upload page

### Flow
Drop a file or pick one (a native `<input type=file>` behind a `<label>`, so it is
keyboard and screen-reader friendly with no library). The browser runs quick
**pre-checks**, then the file is sent **byte-for-byte** to `POST /scans`
(`openapi-fetch` with a pass-through `bodySerializer`), and the app navigates to
`/scans/{id}`.

### Pre-checks are for UX, not security
Size (before reading the file), valid JSON, `lockfileVersion` 2/3, a `packages`
section, and filename heuristics (`yarn.lock` / `pnpm-lock.yaml` / `package.json`
get specific advice). They give instant, specific messages and avoid pointless
uploads. **The API remains the trust boundary** and re-validates everything.
Messages never echo file content; React escapes everything anyway. A Vitest case
and an E2E check confirm that a `<img onerror>` payload in the JSON is never
rendered.

### Errors mapped to actions
`ApiError` + `messageForStatus`: 413 too large, 422 shows the server's parse
reason, 429 asks the user to wait (for step 7's rate limit), 5xx gives a generic
message (**server internals are never shown**, which is tested), and network
failure suggests the API may be cold-starting (Cloud Run scale-to-zero).

### Samples: three real lockfiles, one click
1. **Legacy Express API**: the npm-audit-verified backend fixture.
2. **React + Vite dashboard**: the stage 1 fixture.
3. **This website**: a **pinned snapshot** of `web/package-lock.json`, depgraph
   scanning itself.

Served as static files from `public/samples` and uploaded through the **same
path as user files**, so the demo exercises the real flow. `check:samples` (in
CI) keeps samples 1 and 2 byte-identical to the backend fixtures. Sample 3 is
deliberately frozen so the demo doesn't change with every dependency bump. Card
stats were **measured, not guessed**: my first copy said "177 packages" (raw
lockfile entries), while depgraph reports 171 after collapsing duplicates. The
cards now quote the scan's own numbers, dated because advisories keep arriving.

### `/scans/[id]` under Cache Components
The route param is request-time data, so per the Next 16 docs it is awaited
inside `<Suspense>`. The build reports the route as a **partial prerender**: a
static shell, with the scan fetched client-side.

### The typed client caught a contract gap
`counts: dict[Severity, int]` rendered in OpenAPI as an open `string -> number`
map, so the frontend couldn't rely on the keys. Rather than cast it away, I
changed the API contract to an explicit `SeverityCounts` model (five int fields,
default 0). The JSON is identical, but the schema now guarantees the keys.

### Toolchain fixes
- The scaffold's `@types/node@^20` conflicted with Vitest's peer range while we
  run Node 24, so the types now match the runtime and `engines.node` pins `24.x`
  (Vercel builds with the same major). It was fixed properly, not with
  `--legacy-peer-deps`.

### Verified end to end in a real browser
`playwright-core` driving the installed Edge (no browser download, outside the
repo for now; Playwright joins the repo in step 6):
- sample click → scan page with **89 findings in 1.4 s**;
- a real file upload gives 11 findings;
- the `package.json` rejection, hostile-JSON non-injection, server 422 and
  unknown-scan errors all render readably;
- **no horizontal scroll at 375 px**.

### Found for step 7
Production has **no Redis** (Option B), but `/health/ready` reports Redis as a
required check, so the live site would show "degraded". Readiness must treat
Redis as optional when it isn't configured.

## Step 3: Findings dashboard

### Layout
Findings are grouped by severity (Critical → Low, in the API's CVSS-then-name
order within a group). Each row shows `package installed → fix`,
Direct/Transitive, Dev-only, Malicious, the advisory summary, the CVE (or
advisory id) with its CVSS score, and the shortest path (middle-truncated past
5 hops, with non-prod edges labelled). Each row links to the finding detail.
Colour is always paired with a text label, never colour alone.

### Backend addition: `fixed_version_hint` in the list
The authoritative fixed version (detail endpoint) checks the npm registry, so
showing it on every row would mean tens of registry calls per page view.
Instead, the list carries a **hint**: the advisory's own `fixed` bounds,
evaluated with the same matcher and the registry turned off. All findings'
entries are fetched in **one** query (`unnest` over the (advisory, package)
pairs). It is labelled "fixed in". The detail page stays authoritative, because
a `fixed` version may never have been published. Malicious rows say "remove"
instead.

### Filters: client-side, mirrored in the URL
Severity (multi-select chips with counts), kind, reach, "hide dev-only" and a
search (package, advisory id, CVE alias, summary). Scans have at most a few
hundred findings, so filtering in the browser is instant and needs no extra API
calls.
- **The URL holds the filters** (`?severity=CRITICAL&hide_dev=1&q=…`), so views
  survive reloads and can be shared. Parsing treats URLs as **untrusted**:
  unknown values fall back to defaults, and the query is capped at 100
  characters (unit-tested with `<script>` and junk values).
- **Bug found by the E2E test:** with the URL as the *only* source of truth, a
  clicked checkbox snapped back, because `router.replace` commits as a
  transition. Filters now apply from local state immediately and sync to the URL
  in the background. URL changes made elsewhere (back button, pasted link) are
  adopted by adjusting state during render. React's linter rejects
  setState-in-effect for this, which is right.
- The search box debounces (250 ms) before writing the URL, so typing doesn't
  rewrite history on every keystroke.

### Real-data observation (flagged, not "fixed")
Two GitHub advisories, GHSA-35jh-r3h4-6jhm and GHSA-r5fr-rjxr-66jc, alias each
other and the same CVE-2021-23337. Both appear as rows, and npm audit lists both
too, which is part of the exact 89/89 match. Merging aliased advisories into one
row is a product decision; for now the dashboard mirrors the advisory
database faithfully.

### States
- loading: a skeleton;
- error: a readable message and a link home;
- a scan that isn't completed: its status/error;
- **no findings**: a positive empty state that notes new advisories are matched
  automatically;
- **filters hide everything**: an inline reset.

### Verified (Edge, end to end)
- 89 findings in 4 groups;
- a row shows `4.17.20 → 4.17.21` / Direct / CVE-2021-23337 / CVSS 7.2;
- the Critical filter gives 6 and writes `?severity=CRITICAL`, which survives a
  reload;
- a CVE search gives 2 (the alias pair above);
- hiding dev-only gives 68;
- the empty and reset paths work, and junk URL params cause no errors;
- the acme sample shows 9 Dev-only badges;
- 375 px has no horizontal scroll.

## Step 4: Finding detail page

`/scans/[id]/findings/[fid]` renders the existing detail endpoint:
- a header (severity + CVSS, Malicious and Dev-only badges, package@version,
  the advisory id linked to osv.dev, aliases, published date);
- a **fix card** per action;
- **every path**, shortest first;
- the advisory details (Markdown);
- references.

### Fix card: something to act on, not just a sentence
- `update_lockfile` gives `npm update <pkg>` with a copy button.
- `bump_direct` gives `npm install <pkg>@^<fix>`, with a "major version change"
  warning when the fix is outside `^installed`.
- `upgrade_parent_or_override` gives a **blocked by / declares / upgrade to**
  table (e.g. `mocha@8.2.1 | 5.0.1 | 12.0.0`, `vitest@2.1.9 | ^1.0.1 | 4.0.0`)
  plus the `overrides` JSON to copy, with a note that the parent never declared
  support for that version.
- `no_fix` and `remove` get their own explanations.
- The card states whether the fixed version was checked against the registry
  or taken from the advisory (registry outage fallback).

### Third-party content is untrusted (advisory text includes malware reports)
- **Markdown** via `react-markdown` 10 (0 production vulnerabilities), stricter
  than its defaults:
  - an **element allowlist** (text formatting, lists, code, quotes, links), with
    disallowed elements unwrapped to their text;
  - `skipHtml` drops raw HTML;
  - **no images**, so advisory authors can't plant tracking pixels;
  - advisory `#`/`##` headings are demoted below the page's own.
- **Links** go through `safeHttpUrl`: http(s) only, rejecting `javascript:`,
  `data:`, protocol-relative, relative and `user@host` spoof URLs, then open with
  `target=_blank rel="noopener noreferrer nofollow ugc"`. Unsafe links render as
  plain text. This is on top of react-markdown's own `urlTransform`, which
  already neutralises `javascript:`: defence in depth.
- **Tests:** a server-side render of hostile Markdown (`<img onerror>`,
  `<script>`, `<iframe>`, tracking image, five unsafe link forms) asserts no
  `<img>`, no `<script>`, no `<a>` for unsafe URLs, and correct `rel`/`target`
  on safe ones.
- Real advisories contain proof-of-concept exploit code; it renders as inert
  `<pre><code>` text, which is exactly what we want.

### Paths
All paths up to the server cap, shortest first, numbered. Non-prod edges are
labelled (`legacy-api → [dev] mocha@8.2.1 → serialize-javascript@5.0.1`). The
first 10 are shown, with a "show all N" toggle and a note when the server cap
truncated the list.

### Back link keeps the dashboard's filters
The dashboard records its query string in `sessionStorage` (per scan, per tab,
best-effort). The detail page reads it with **`useSyncExternalStore`**: the
server snapshot is empty and the client snapshot is the stored value, so there
is **no hydration mismatch** and no setState-in-effect. An invalid finding id
is detected during render, so no effect is needed. My first draft suppressed
React's lint rule twice; both suppressions are gone.

### Cache Components keeps visited routes mounted (hidden)
In E2E the page had three `<h1>`s. Next 16 keeps recently visited routes
mounted inside React `<Activity mode="hidden">` (`display: none`) so back
navigation restores state. That's not a bug, but it matters for testing
(locators must target visible elements) and for code: effects in hidden routes
are cleaned up and re-run when shown.

### Verified (Edge, end to end)
- dashboard (Critical) → minimist detail: `bump_direct`, the copy button really
  copies `npm install minimist@^1.2.6`, and the back link restores
  `?severity=CRITICAL`;
- a transitive finding shows the blockers table and an `overrides` snippet;
- tinypool shows `vitest@2.1.9 (^1.0.1) → 4.0.0` and the `[dev]` edge in its path;
- references carry the safe `rel`;
- non-numeric and unknown ids give readable errors;
- 375 px has no horizontal scroll.

Vitest gained a config only to mirror the `@/` path alias.

## Step 5: Interactive dependency graph

`/scans/[id]/graph` (a "Dependency graph" tab next to "Findings") uses React Flow
(`@xyflow/react` 12.12, 0 production vulnerabilities). It loads only on this
route (Next splits per route), so the dashboard doesn't pay for it.

### All graph logic is pure and tested first (`lib/graph-model.ts`)
- **Depth** = BFS hop count from the root; unreachable nodes are excluded.
- **Highlight "every path to X"** = edges whose source is reachable from the
  root *and* whose target can reach X. That is two linear passes, **exact and
  uncapped**, with no API call per click, unlike the detail endpoint's
  enumerated, capped list. With cycles it may include an edge that lies only on
  a non-simple walk, which is fine for a visual highlight (documented, tested).
- **Visible set** = nodes within the depth limit ∪ every node on a path to a
  vulnerable package. **Vulnerable paths are always shown, however deep.**
- **Radial layout, custom, no dependency.** Ring *k* holds depth-*k* nodes.
  Ring 1 is spread evenly by name; deeper nodes take the *circular mean* angle
  of their visible parents, then the ring is re-spaced in that order, so
  children sit under their parents and crossings stay down. The ring radius
  grows with depth *and* with how many nodes the ring holds (`n × spacing /
  2π`), so crowded rings never overlap (tested with a 200-node fan-out). It is
  deterministic: the same graph always gives the same picture.
- *Rejected:* dagre or ELK layered layouts (about 150 KB more, and they put the
  root at the top, not the centre) and force simulations (non-deterministic,
  and they need many iterations for 500 nodes).

### Usable on large graphs
- The default is depth 2 plus the vulnerable paths: 85 of 172 nodes for the
  legacy sample, versus an unreadable hairball at full depth.
- The depth slider goes up to the full graph. Above **600 nodes** it asks before
  rendering, offering "reduce depth" or "show anyway".
- React Flow `onlyRenderVisibleElements`, nodes not draggable or connectable, no
  animated edges, memoised node component and arrays. Highlighting changes only
  styles, not the layout.
- **Measured** on the largest sample (505 packages, depgraph's own lockfile):
  the page opens in ~1.2 s; full depth renders all 505 nodes, and a re-layout
  takes ~0.5 s.

### Interaction
- Click a finding in the side list: its paths light up (indigo, thicker), the
  rest dims to 15%, and the view zooms to the lit subgraph.
- Click a node: a panel shows name@version, depth, dev-only, and links to the
  findings on it.
- Nodes are ringed by worst severity, and non-prod edges are dashed; a legend
  explains both.
- Edges join node **centres** (handles centred and hidden), which suits a
  radial picture better than top/bottom ports.

### Bug found by the mobile check: grid `min-width: auto`
At 375 px the toolbar and legend were clipped and `fitView` was off-centre.
Grid items default to `min-width: auto`, so a long line widened the column past
the viewport. Fixed with `minmax(0,1fr)` + `min-w-0`. My first mobile assertion
(`scrollWidth`) missed it because the overflow was *clipped*, not scrollable,
so E2E now also checks that **no element's right edge passes the viewport**.

### Verified (Edge, end to end)
- 85 nodes at depth 2;
- the root at layout origin `translate(0px, 0px)`;
- the serialize-javascript finding lights `legacy-api → mocha →
  serialize-javascript`;
- the node info panel, clear highlight and depth slider work (full depth 172/172;
  depth 1 still 30 nodes thanks to vulnerable paths);
- the 505-node graph and its braces path highlight work;
- no page errors, and mobile is fixed.

## Step 6: States, mobile, and end-to-end + accessibility tests in CI

### Every state, everywhere
- **App-level:** `error.tsx` (render crash → "Try again"), `global-error.tsx`
  (root-layout failure; renders its own `<html>`/`<body>` and imports global
  styles, per the Next 16 docs), and a custom root `not-found.tsx` (unmatched
  URLs return a real **404**, which is tested). In Next 16 the recovery prop is
  **`retry()`**, not the older `reset()`, which I checked in the bundled docs.
- **Retry** on every data view: an `attempt` counter re-runs the fetch. 404s
  get no retry button, because they won't fix themselves.
- **Cold starts:** Cloud Run scales to zero, so any load lasting over 3 s shows
  "the API scales to zero… waking it up" (`useSlow`), which disappears when the
  data arrives (tested with an injected 4.5 s delay).
- **Upload:** a Cancel button (the `AbortController` is wired through
  `openapi-fetch`). Touch screens get "Choose your package-lock.json" instead of
  "Drop…" (Tailwind `pointer-coarse:`).
- **Graph:** an empty state for projects with no dependencies.
- **Server errors** show a generic message; the API's `detail` never reaches the
  page on 5xx (tested with a fake `psycopg` error body).

### Mobile
- The dashboard's filter controls (about 250 px tall) collapse behind a
  "Filters (n)" toggle below `sm`, but stay open whenever a filter is active so
  applied filters are never hidden.
- On touch screens (`pointer-coarse:`), chips, segmented controls, checkboxes and
  buttons are **≥ 44 px tall** (WCAG 2.5.5); the test measures it.
- **Real bug found:** on a 412 px phone the detail page was 553 px wide, because
  of a long unbreakable *inline* exploit string in an advisory. Block code
  already scrolls inside its own `<pre>`. Advisory prose now uses
  `overflow-wrap: anywhere`. The earlier 375 px ad-hoc check had looked at a
  different finding, which is why fixed, repeatable tests beat one-off scripts.

### Accessibility: axe-core in every E2E run
`@axe-core/playwright` runs on home, dashboard, detail, graph and 404, in light
**and dark** mode, and **fails on any "serious" or "critical" violation**. The
first run found real ones: **colour contrast** (grey secondary text, especially
in dark mode, and `opacity-70/80` on counts). Fixed systematically: one
secondary grey per mode that passes (zinc-600 on white, zinc-400 on near-black),
and no translucent text. Only React Flow's canvas internals are excluded; its
controls are still checked.

### Playwright suite in the repo (replaces the ad-hoc scripts)
36 tests across `upload`, `dashboard`, `detail`, `graph`, `states`, `a11y`
(desktop) and `mobile` (Pixel 7 profile). They use **installed browsers via
`channel`**, with no browser downloads: Edge locally, the Chrome preinstalled on
GitHub's runners in CI. A helper scopes locators to visible elements, because
Next 16 keeps recently visited routes mounted but hidden.

### New CI job: `e2e` against the real stack
`docker compose up --wait api` (Postgres, Redis, migrations, API) → load the
**frozen OSV fixture** (`audit-subset.zip`) with the real ingester → `next build`
→ `playwright test` in Chrome. Because the advisory data is frozen, assertions
can be exact (89 findings, 68 without dev-only, 85/172 graph nodes at depth 2).
On failure it uploads the Playwright report and traces, and prints API logs.
Cost: a few CI minutes per push, free for a public repo.

## Step 7a: Production readiness (code only; nothing provisioned)

Deployment shape is Option B (see "Stage 3 decisions"): Postgres in Docker on
an always-free e2-micro VM with no public IP, the API and its jobs on Cloud Run
over Direct VPC egress, no Redis, and the frontend on Vercel. Runbook and cost
table: [infra/README.md](infra/README.md).

### Upload rate limiting in Postgres
- **Why Postgres:** there's no Redis in production, and limits must hold across
  Cloud Run instances. An in-process counter would multiply by the instance
  count.
- **Fixed windows, one upsert per check**
  (`INSERT … ON CONFLICT DO UPDATE … RETURNING hits`). It's atomic under
  concurrency because the upsert serialises on the row. The trade-off: up to
  2× the limit can get through across a window boundary. That's fine for
  protecting a free demo, and far simpler than a sliding log.
- **Limits:** 10 uploads per address per 10 minutes, plus 300 per hour globally.
  The global cap bounds the cost if many addresses are used at once.
- **Checked before the body is read,** so a blocked client costs one upsert,
  not a 5 MB upload and a parse (tested: an oversized body from a blocked
  client gets 429, not 413).
- **Refused requests aren't counted.** The check runs in its own transaction,
  and raising 429 rolls it back. So hammering while blocked doesn't extend the
  block, and the global cap isn't charged for requests the per-address limit
  already refused.
- **`UNLOGGED` table:** counters skip the write-ahead log. A crash empties the
  table, which merely resets limits. The sync job deletes counters older than a
  day.
- **Who the client is:** `CLIENT_IP_PROXY_HOPS=N` takes the Nth
  X-Forwarded-For entry from the right, which is the one our own proxy wrote.
  Entries further left are client-supplied and forgeable. Google's docs only
  say the client is "generally the first" entry, and don't say whether
  client-sent values are kept. So I take the rightmost (N=1). The deploy runbook
  checks it with forged headers: if 11 uploads with different forged values all
  succeed, the setting is wrong. Locally, the smoke test showed
  `X-Forwarded-For: 198.51.100.1, 203.0.113.9` landing in 203.0.113.9's bucket.
- **IPv6 is bucketed per /64,** because one subscriber usually owns a whole /64
  and could otherwise rotate addresses for free. Keys store a truncated SHA-256
  of the address, not the address itself. That's pseudonymous, not anonymous,
  since the IPv4 space is small enough to brute-force, which is why rows are
  also short-lived.
- **The frontend shows the API's message** ("Try again in 7 minutes"), and
  `Retry-After` is exposed through CORS.

### Scan retention
`SCAN_RETENTION_DAYS=7` in production (0, keep forever, by default). The
`sync` command, the scheduled job and the worker loop prune after each sync.
- **Batched deletes** of 200 scans per transaction, which cascade to nodes,
  edges and findings. No statement holds locks on a large share of the graph
  tables.
- **New index** `ix_scans_created_at`.
- **The package catalogue is kept:** it's shared, deduplicated and small.
- **The home page says so:** the server keeps the resolved graph, never the
  file itself, and deletes each scan after 7 days. Links are unguessable but
  not private.

### Redis optional
`REDIS_URL=` (empty) means no Redis. The npm client already treated its cache
as optional. Readiness now reports `"redis": "disabled"` and stays 200, which
fixes the "degraded" status found in step 2. The cost is that every
finding-detail fix lookup goes to the npm registry. That's acceptable at demo
traffic, and the per-process single-flight table still dedupes concurrent
lookups.

### Secrets
`DATABASE_URL` is now plain config (`postgresql+asyncpg://depgraph@10.10.0.10/depgraph`).
`DATABASE_PASSWORD` comes from Secret Manager and is substituted into the URL
(`Settings.sqlalchemy_url()`), so one secret serves Cloud Run, the jobs and the
VM. The password is generated by the bootstrap script straight into Secret
Manager; it never touches a file, the repo or an image. On the VM it is
fetched into tmpfs at boot.

### CORS
Exact production origin, plus `CORS_ORIGIN_REGEX` for Vercel preview deploys,
anchored to my own team slug: `^https://depgraph-[a-z0-9-]+-<team>\.vercel\.app$`.
A bare `*.vercel.app` would admit anyone's deployment. There are no
credentials, so the exposure would be small anyway, but there's no reason to
allow it.

### Infrastructure as idempotent gcloud scripts (`infra/gcp/`)
- **Dry run by default.** Every mutating command is printed until `--apply`,
  so the exact commands can be reviewed before anything is created.
- **Idempotent:** each resource is described first and created only if
  missing, so a half-failed run can just be repeated.
- **Chose gcloud over Terraform:** nothing extra to install, and no state
  bucket to manage. I'd revisit that for a team, for drift detection.
- **The VM:** Container-Optimized OS, with Postgres 17 from Artifact Registry.
  - It has no internet route, by design: no public IP and no Cloud NAT, which
    would cost about $1/month. Images and the secret come via Private Google
    Access.
  - Data lives on its own disk with `auto-delete=no`.
  - The startup script **refuses to start Postgres if the data disk isn't
    mounted.** Otherwise it would silently initdb an empty database on the
    boot disk.
  - The container is recreated on each boot, after the mount, rather than
    using `--restart always`, which could start it before the mount.
- **Postgres tuned for 1 GB RAM:**
  - `shared_buffers` 128 MB, with the rest left to the OS page cache.
  - `max_connections` 40: 2 API instances × 10, plus jobs.
- **Least-privilege service accounts:**
  - api: the secret only;
  - backup: the secret plus object *create* on the bucket (it can't delete
    or read old dumps);
  - db VM: the secret, AR read and log write;
  - scheduler: run jobs.
- **Backups:**
  - A nightly Cloud Run job (`pg_dump -Fc`), built FROM the same Postgres
    image, so client and server versions always match.
  - It dumps to a file before uploading, so a failed dump never uploads a
    truncated "backup".
  - A lifecycle rule deletes dumps after 7 days. GCS **soft delete is turned
    off**, because it would keep (and bill) deleted dumps for another 7 days.
  - Locally, the 522 MB database dumps to 44 MB in 25 s.
- **Artifact Registry cleanup policy:** keep the 3 newest versions of each
  image, which keeps storage under the 0.5 GB free tier.
- **CI** now runs shellcheck on the scripts and builds the backup image.

### Verified locally
The production-config API container ran against the compose Postgres with no
Redis, the password from `DATABASE_PASSWORD`, one proxy hop and a limit of 2:
- readiness `{"postgres":"ok","redis":"disabled"}`;
- uploads → `201 201 429 201`, where the 4th was a different address;
- the 429 carried `Retry-After`;
- a preview-pattern origin was allowed.

Tests: 627 backend (+24), 81 web unit (+1).
