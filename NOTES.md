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
