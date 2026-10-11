"""Pure tests for install-path -> identity collapsing (no database)."""

from collections import Counter

from depgraph.graph.collapse import collapse
from depgraph.lockfile.npm import DepType, Source, parse_package_lock
from tests.helpers import lock, pkg, realworld_lock

ROOT_KEY = ("root", "app", ".")


def reg(name: str, version: str) -> tuple[str, str, str]:
    return ("registry", name, version)


def _dup_lock(**b_extra) -> dict:
    """x and y both need c@1, but the hoisted c is 2.x, so c@1 is nested twice."""
    return lock(
        {
            "node_modules/x": pkg("x", "1.0.0", dependencies={"c": "^1"}),
            "node_modules/x/node_modules/c": pkg("c", "1.0.0", dependencies={"d": "*"}),
            "node_modules/y": pkg("y", "1.0.0", dependencies={"c": "^1"}),
            "node_modules/y/node_modules/c": pkg("c", "1.0.0", dependencies={"d": "*"}, **b_extra),
            "node_modules/c": pkg("c", "2.0.0"),
            "node_modules/d": pkg("d", "1.0.0"),
        },
        root={"dependencies": {"x": "*", "y": "*", "c": "^2"}},
    )


def test_duplicate_copies_become_one_node_with_every_install_path():
    g = collapse(parse_package_lock(_dup_lock()))
    c1 = g.nodes[reg("c", "1.0.0")]
    assert c1.install_paths == ("node_modules/x/node_modules/c", "node_modules/y/node_modules/c")
    assert c1.ref is None and c1.source is Source.REGISTRY
    assert reg("c", "2.0.0") in g.nodes
    assert len(g.nodes) == 6  # root, x, y, c@1, c@2, d


def test_edges_are_rewired_to_identities_and_deduplicated():
    g = collapse(parse_package_lock(_dup_lock()))
    pairs = Counter((e.parent, e.child, e.dep_type) for e in g.edges)
    assert pairs[(reg("x", "1.0.0"), reg("c", "1.0.0"), DepType.PROD)] == 1
    assert pairs[(reg("y", "1.0.0"), reg("c", "1.0.0"), DepType.PROD)] == 1
    # both copies of c@1 depend on d: one edge, not two
    assert pairs[(reg("c", "1.0.0"), reg("d", "1.0.0"), DepType.PROD)] == 1
    assert max(pairs.values()) == 1


def test_root_identity():
    g = collapse(parse_package_lock(lock({})))
    assert g.root == ROOT_KEY
    root = g.nodes[ROOT_KEY]
    assert (root.source, root.name, root.ref, root.install_paths) == (
        Source.ROOT,
        "app",
        ".",
        ("",),
    )


def test_flags_survive_only_if_every_copy_has_them():
    g = collapse(parse_package_lock(_dup_lock(dev=True)))  # only y's copy is dev
    assert not g.nodes[reg("c", "1.0.0")].dev

    both = lock(
        {
            "node_modules/x": pkg("x", "1.0.0", dev=True, dependencies={"c": "^1"}),
            "node_modules/x/node_modules/c": pkg("c", "1.0.0", dev=True),
            "node_modules/y": pkg("y", "1.0.0", dev=True, dependencies={"c": "^1"}),
            "node_modules/y/node_modules/c": pkg("c", "1.0.0", dev=True),
            "node_modules/c": pkg("c", "2.0.0"),
        }
    )
    assert collapse(parse_package_lock(both)).nodes[reg("c", "1.0.0")].dev


def test_cycles_survive_collapsing():
    g = collapse(
        parse_package_lock(
            lock(
                {
                    "node_modules/a": pkg("a", "1.0.0", dependencies={"b": "*"}),
                    "node_modules/b": pkg("b", "1.0.0", dependencies={"a": "*"}),
                },
                root={"dependencies": {"a": "*"}},
            )
        )
    )
    pairs = {(e.parent, e.child) for e in g.edges}
    assert {(reg("a", "1.0.0"), reg("b", "1.0.0")), (reg("b", "1.0.0"), reg("a", "1.0.0"))} <= pairs


def test_local_and_fetched_non_registry_identities():
    url = "git+ssh://git@github.com/o/g.git#abc123"
    g = collapse(
        parse_package_lock(
            lock(
                {
                    "node_modules/web": {"resolved": "packages/web", "link": True},
                    "packages/web": {"name": "web", "version": "0.1.0", "dependencies": {"g": "*"}},
                    "packages/web/node_modules/g": {"version": "1.0.0", "resolved": url},
                    "node_modules/g": {"version": "1.0.0", "resolved": url},
                },
                root={"workspaces": ["packages/*"], "dependencies": {"g": "*"}},
            )
        )
    )
    web = g.nodes[("workspace", "web", "packages/web")]
    assert web.ref == "packages/web"
    git = g.nodes[("git", "g", url)]  # same commit at two paths: one node
    assert git.ref == url
    assert git.install_paths == ("node_modules/g", "packages/web/node_modules/g")


def test_registry_entry_without_version_is_kept_with_a_warning():
    g = collapse(parse_package_lock(lock({"node_modules/odd": {"inBundle": True}})))
    assert ("remote", "odd", "node_modules/odd") in g.nodes
    assert g.warnings == ({"kind": "registry_package_without_version", "path": "node_modules/odd"},)


def test_conflicting_integrity_between_copies_is_flagged():
    doc = _dup_lock(integrity="sha512-OTHER")
    doc["packages"]["node_modules/x/node_modules/c"]["integrity"] = "sha512-ONE"
    g = collapse(parse_package_lock(doc))
    (w,) = [w for w in g.warnings if w["kind"] == "integrity_conflict"]
    assert w["package"] == "c@1.0.0"
    assert w["integrities"] == ["sha512-ONE", "sha512-OTHER"]


def test_one_copy_missing_integrity_is_not_a_conflict():
    g = collapse(parse_package_lock(_dup_lock(integrity="sha512-OTHER")))
    assert g.warnings == ()
    assert g.nodes[reg("c", "1.0.0")].integrity == "sha512-OTHER"


def test_only_required_unresolved_dependencies_become_warnings():
    g = collapse(
        parse_package_lock(
            lock(
                {}, root={"dependencies": {"gone": "^1"}, "optionalDependencies": {"fsevents": "*"}}
            )
        )
    )
    assert g.warnings == (
        {
            "kind": "unresolved_dependency",
            "parent": "(root)",
            "name": "gone",
            "spec": "^1",
            "dep_type": "prod",
        },
    )


def test_realworld_fixture():
    parsed = parse_package_lock(realworld_lock())
    g = collapse(parsed)
    distinct = {(n.name, n.version) for p, n in parsed.nodes.items() if p}
    assert len(g.nodes) == len(distinct) + 1
    assert len(g.nodes) < len(parsed.nodes)  # duplicates really were merged
    assert len(g.nodes[reg("debug", "2.6.9")].install_paths) >= 2
    assert all(e.parent in g.nodes and e.child in g.nodes for e in g.edges)
    assert g.warnings == ()  # npm output is self-consistent
