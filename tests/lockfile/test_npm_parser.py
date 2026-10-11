"""Unit tests for the package-lock.json parser.

Each test builds the smallest lockfile that exercises one rule, so a failure
points at exactly one behaviour. Real-world coverage lives in test_npm_fixture.py.
"""

import json
from typing import Any

import pytest

from depgraph.lockfile.npm import (
    ROOT,
    DepType,
    InvalidLockfileError,
    LockEdge,
    LockfileGraph,
    Source,
    UnsupportedLockfileVersionError,
    parse_package_lock,
)
from tests.helpers import REG, lock, pkg


def edges(g: LockfileGraph) -> set[tuple[str, str, str]]:
    return {(e.parent, e.child, e.dep_type.value) for e in g.edges}


# --------------------------------------------------------------- input / errors


@pytest.mark.parametrize("version", [2, 3])
def test_minimal_lockfile(version):
    g = parse_package_lock(lock({}, version=version))
    assert g.lockfile_version == version
    assert list(g.nodes) == [ROOT]
    assert g.root.source is Source.ROOT
    assert g.root.name == "app"
    assert g.edges == () and g.unresolved == ()


def test_accepts_bytes_str_and_mapping_identically():
    doc = lock({"node_modules/a": pkg("a", "1.0.0")}, root={"dependencies": {"a": "^1"}})
    raw = json.dumps(doc)
    assert parse_package_lock(doc) == parse_package_lock(raw) == parse_package_lock(raw.encode())


def test_v1_rejected_with_actionable_message():
    with pytest.raises(UnsupportedLockfileVersionError, match="npm <= 6") as exc:
        parse_package_lock({"lockfileVersion": 1, "dependencies": {}})
    assert exc.value.version == 1


@pytest.mark.parametrize("version", [None, 0, 4, "3", 3.0001])
def test_unknown_versions_rejected(version):
    doc = lock({})
    doc["lockfileVersion"] = version
    with pytest.raises(UnsupportedLockfileVersionError):
        parse_package_lock(doc)


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"\xff\xfe\x00garbage",
        "[]",
        '"a string"',
        json.dumps({"lockfileVersion": 3}),  # no packages
        json.dumps({"lockfileVersion": 3, "packages": []}),
        json.dumps({"lockfileVersion": 3, "packages": {"node_modules/a": {}}}),  # no root
        json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/a": "x"}}),
    ],
)
def test_malformed_input_raises_invalid_lockfile(raw):
    with pytest.raises(InvalidLockfileError):
        parse_package_lock(raw)


def test_errors_share_a_base_class_callers_can_catch():
    from depgraph.lockfile.npm import LockfileError

    assert issubclass(InvalidLockfileError, LockfileError)
    assert issubclass(UnsupportedLockfileVersionError, LockfileError)


# ------------------------------------------------------------------- resolution


def test_hoisted_dependency_resolves_from_root_node_modules():
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg("a", "1.0.0", dependencies={"b": "^1"}),
                "node_modules/b": pkg("b", "1.2.0"),
            },
            root={"dependencies": {"a": "^1"}},
        )
    )
    assert edges(g) == {
        (ROOT, "node_modules/a", "prod"),
        ("node_modules/a", "node_modules/b", "prod"),
    }


def test_nested_copy_shadows_hoisted_copy():
    # a needs b@2, so npm nests b@2 under a; the root keeps b@1.
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg("a", "1.0.0", dependencies={"b": "^2"}),
                "node_modules/a/node_modules/b": pkg("b", "2.0.0"),
                "node_modules/b": pkg("b", "1.0.0"),
            },
            root={"dependencies": {"a": "^1", "b": "^1"}},
        )
    )
    assert ("node_modules/a", "node_modules/a/node_modules/b", "prod") in edges(g)
    assert (ROOT, "node_modules/b", "prod") in edges(g)
    assert ("node_modules/a", "node_modules/b", "prod") not in edges(g)


def test_resolution_walks_up_through_each_ancestor():
    # c is required by a/node_modules/b and lives one level up, under a.
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg("a", "1.0.0", dependencies={"b": "*"}),
                "node_modules/a/node_modules/b": pkg("b", "1.0.0", dependencies={"c": "*"}),
                "node_modules/a/node_modules/c": pkg("c", "2.0.0"),
                "node_modules/c": pkg("c", "1.0.0"),
            },
            root={"dependencies": {"a": "*"}},
        )
    )
    assert ("node_modules/a/node_modules/b", "node_modules/a/node_modules/c", "prod") in edges(g)


def test_resolution_does_not_look_in_unrelated_subtrees():
    # b is only nested under x; a cannot see it.
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg("a", "1.0.0", dependencies={"b": "*"}),
                "node_modules/x": pkg("x", "1.0.0"),
                "node_modules/x/node_modules/b": pkg("b", "1.0.0"),
            }
        )
    )
    assert [(u.parent, u.name, u.required) for u in g.unresolved] == [("node_modules/a", "b", True)]


def test_scoped_packages_resolve_and_get_their_full_name():
    g = parse_package_lock(
        lock(
            {
                "node_modules/@scope/a": pkg("@scope/a", "1.0.0", dependencies={"@scope/b": "*"}),
                "node_modules/@scope/a/node_modules/@scope/b": pkg("@scope/b", "2.0.0"),
                "node_modules/@scope/b": pkg("@scope/b", "1.0.0"),
            },
            root={"dependencies": {"@scope/a": "*"}},
        )
    )
    nested = "node_modules/@scope/a/node_modules/@scope/b"
    assert ("node_modules/@scope/a", nested, "prod") in edges(g)
    assert g.nodes[nested].name == "@scope/b"
    assert g.nodes[nested].source is Source.REGISTRY


def test_npm_alias_uses_real_package_name():
    # "my-lodash": "npm:lodash@^4" installs lodash under node_modules/my-lodash.
    g = parse_package_lock(
        lock(
            {"node_modules/my-lodash": {**pkg("lodash", "4.17.21"), "name": "lodash"}},
            root={"dependencies": {"my-lodash": "npm:lodash@^4"}},
        )
    )
    node = g.nodes["node_modules/my-lodash"]
    assert (node.name, node.version, node.source) == ("lodash", "4.17.21", Source.REGISTRY)
    assert g.edges == (LockEdge(ROOT, "node_modules/my-lodash", DepType.PROD, "npm:lodash@^4"),)


def test_cycles_are_represented_not_followed():
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg("a", "1.0.0", dependencies={"b": "*"}),
                "node_modules/b": pkg("b", "1.0.0", dependencies={"a": "*"}),
            },
            root={"dependencies": {"a": "*"}},
        )
    )
    assert {
        ("node_modules/a", "node_modules/b", "prod"),
        ("node_modules/b", "node_modules/a", "prod"),
    } <= edges(g)


# ------------------------------------------------------------- dependency types


def test_dependency_types_and_declared_specs():
    g = parse_package_lock(
        lock(
            {
                "node_modules/p": pkg("p", "1.0.0"),
                "node_modules/d": pkg("d", "1.0.0", dev=True),
                "node_modules/o": pkg("o", "1.0.0", optional=True),
                "node_modules/r": pkg("r", "18.0.0", peer=True),
                "node_modules/ro": pkg("ro", "1.0.0", peer=True),
            },
            root={
                "dependencies": {"p": "^1"},
                "devDependencies": {"d": "~1.0.0"},
                "optionalDependencies": {"o": "1.x"},
                "peerDependencies": {"r": ">=17", "ro": "*"},
                "peerDependenciesMeta": {"ro": {"optional": True}},
            },
        )
    )
    assert {(e.child, e.dep_type, e.spec) for e in g.edges} == {
        ("node_modules/p", DepType.PROD, "^1"),
        ("node_modules/d", DepType.DEV, "~1.0.0"),
        ("node_modules/o", DepType.OPTIONAL, "1.x"),
        ("node_modules/r", DepType.PEER, ">=17"),
        ("node_modules/ro", DepType.PEER_OPTIONAL, "*"),
    }


def test_optional_dependencies_override_same_named_dependencies():
    g = parse_package_lock(
        lock(
            {"node_modules/o": pkg("o", "1.0.0")},
            root={"dependencies": {"o": "^1"}, "optionalDependencies": {"o": "^1"}},
        )
    )
    assert edges(g) == {(ROOT, "node_modules/o", "optional")}


def test_same_package_as_dependency_and_peer_gives_two_typed_edges():
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg(
                    "a", "1.0.0", dependencies={"r": "^18"}, peerDependencies={"r": ">=16"}
                ),
                "node_modules/r": pkg("r", "18.0.0"),
            }
        )
    )
    assert {
        ("node_modules/a", "node_modules/r", "prod"),
        ("node_modules/a", "node_modules/r", "peer"),
    } <= edges(g)


def test_dev_dependencies_of_installed_packages_are_ignored():
    # Registry packages' devDependencies are never installed; don't invent edges.
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg("a", "1.0.0", devDependencies={"jest": "*"}),
                "node_modules/jest": pkg("jest", "29.0.0"),
            }
        )
    )
    assert not any(e.parent == "node_modules/a" for e in g.edges)
    assert g.unresolved == ()


def test_missing_optional_deps_are_soft_missing_required_deps_are_hard():
    g = parse_package_lock(
        lock(
            {},
            root={
                "dependencies": {"needed": "^1"},
                "optionalDependencies": {"fsevents": "^2"},
                "peerDependencies": {"react": "*", "sass": "*"},
                "peerDependenciesMeta": {"sass": {"optional": True}},
            },
        )
    )
    assert {(u.name, u.dep_type, u.required) for u in g.unresolved} == {
        ("needed", DepType.PROD, True),
        ("fsevents", DepType.OPTIONAL, False),
        ("react", DepType.PEER, True),
        ("sass", DepType.PEER_OPTIONAL, False),
    }


def test_lockfile_flags_are_copied_onto_nodes():
    g = parse_package_lock(
        lock(
            {
                "node_modules/a": pkg(
                    "a",
                    "1.0.0",
                    dev=True,
                    optional=True,
                    devOptional=True,
                    peer=True,
                    integrity="sha512-abc",
                ),
            }
        )
    )
    a = g.nodes["node_modules/a"]
    assert (a.dev, a.optional, a.dev_optional, a.peer, a.integrity) == (
        True, True, True, True, "sha512-abc",
    )  # fmt: skip


# ---------------------------------------------------------------- source kinds


@pytest.mark.parametrize(
    ("name", "entry", "expected"),
    [
        ("a", pkg("a", "1.0.0"), Source.REGISTRY),
        ("@s/a", pkg("@s/a", "1.0.0"), Source.REGISTRY),
        # Private registry: classified by URL shape, not host.
        (
            "a",
            {"version": "1.0.0", "resolved": "https://art.corp/api/npm/npm/a/-/a-1.0.0.tgz"},
            Source.REGISTRY,
        ),
        # Some registries percent-encode the scope separator.
        ("@s/a", {"version": "1.0.0", "resolved": f"{REG}/@s%2fa/-/a-1.0.0.tgz"}, Source.REGISTRY),
        # npm can omit `resolved` for registry packages (omit-lockfile-registry-resolved).
        ("a", {"version": "1.0.0"}, Source.REGISTRY),
        ("a", {"version": "1.0.0", "inBundle": True}, Source.REGISTRY),
        (
            "a",
            {"version": "1.0.0", "resolved": "git+ssh://git@github.com/o/a.git#0123abc"},
            Source.GIT,
        ),
        ("a", {"version": "1.0.0", "resolved": "github:o/a#0123abc"}, Source.GIT),
        ("a", {"version": "1.0.0", "resolved": "file:vendor/a-1.0.0.tgz"}, Source.FILE),
        ("a", {"version": "1.0.0", "resolved": "https://cdn.example.com/a.tgz"}, Source.REMOTE),
        # Registry-shaped URL whose version doesn't match the entry: not trusted.
        ("a", {"version": "1.0.0", "resolved": f"{REG}/a/-/a-9.9.9.tgz"}, Source.REMOTE),
    ],
)
def test_source_classification(name, entry, expected):
    g = parse_package_lock(lock({f"node_modules/{name}": entry}))
    assert g.nodes[f"node_modules/{name}"].source is expected


def test_bundled_flag_is_recorded():
    g = parse_package_lock(lock({"node_modules/a": {"version": "1.0.0", "inBundle": True}}))
    assert g.nodes["node_modules/a"].in_bundle


# ------------------------------------------------------------ workspaces/links


def _workspace_lock(**root_extra: Any) -> dict:
    return lock(
        {
            "node_modules/web": {"resolved": "packages/web", "link": True},
            "node_modules/api": {"resolved": "packages/api", "link": True},
            "packages/web": {
                "name": "web",
                "version": "0.1.0",
                "dependencies": {"api": "*", "react": "^18"},
                "devDependencies": {"vite": "^5"},
            },
            "packages/web/node_modules/react": pkg("react", "18.3.1"),
            "packages/api": {"name": "api", "version": "0.2.0", "dependencies": {"lodash": "^4"}},
            "node_modules/lodash": pkg("lodash", "4.17.21"),
            "node_modules/vite": pkg("vite", "5.4.8", dev=True),
        },
        root={"workspaces": ["packages/*"], **root_extra},
    )


def test_workspaces_are_nodes_and_implicit_root_dependencies():
    g = parse_package_lock(_workspace_lock())
    assert g.nodes["packages/web"].source is Source.WORKSPACE
    assert g.nodes["packages/web"].name == "web"
    assert "node_modules/web" not in g.nodes  # links are redirects, not packages
    assert {(ROOT, "packages/web", "prod"), (ROOT, "packages/api", "prod")} <= edges(g)


def test_workspace_dependencies_resolve_through_links_and_local_node_modules():
    g = parse_package_lock(_workspace_lock())
    assert {
        ("packages/web", "packages/api", "prod"),  # via the node_modules/api link
        ("packages/web", "packages/web/node_modules/react", "prod"),  # local node_modules
        ("packages/web", "node_modules/vite", "dev"),  # workspace dev deps count
        ("packages/api", "node_modules/lodash", "prod"),  # hoisted to the root
    } <= edges(g)
    assert g.unresolved == ()


def test_root_declaring_a_workspace_does_not_duplicate_the_edge():
    g = parse_package_lock(_workspace_lock(dependencies={"web": "*"}))
    root_to_web = [e for e in g.edges if e.parent == ROOT and e.child == "packages/web"]
    assert root_to_web == [LockEdge(ROOT, "packages/web", DepType.PROD, "*")]


def test_yarn_style_workspaces_object_is_accepted():
    doc = _workspace_lock()
    doc["packages"][ROOT]["workspaces"] = {"packages": ["packages/*"]}
    assert parse_package_lock(doc).nodes["packages/web"].source is Source.WORKSPACE


def test_linked_directory_outside_workspaces_is_a_file_source():
    g = parse_package_lock(
        lock(
            {
                "node_modules/shared": {"resolved": "../shared", "link": True},
                "../shared": {"name": "shared", "version": "1.0.0"},
            },
            root={"dependencies": {"shared": "file:../shared"}},
        )
    )
    assert g.nodes["../shared"].source is Source.FILE
    assert edges(g) == {(ROOT, "../shared", "prod")}


def test_link_to_missing_target_is_unresolved():
    g = parse_package_lock(
        lock(
            {"node_modules/ghost": {"resolved": "packages/ghost", "link": True}},
            root={"dependencies": {"ghost": "*"}},
        )
    )
    assert [(u.name, u.required) for u in g.unresolved] == [("ghost", True)]


# ------------------------------------------------------------------ determinism


def test_output_is_sorted_and_deterministic():
    doc = lock(
        {
            "node_modules/z": pkg("z", "1.0.0"),
            "node_modules/a": pkg("a", "1.0.0"),
        },
        root={"dependencies": {"z": "*", "a": "*"}},
    )
    g = parse_package_lock(doc)
    assert list(g.nodes) == [ROOT, "node_modules/a", "node_modules/z"]
    assert list(g.edges) == sorted(g.edges)


def test_result_is_immutable():
    g = parse_package_lock(lock({}))
    with pytest.raises(AttributeError):
        g.root.name = "x"  # type: ignore[misc]
