"""Pure tests for bounded, shortest-first path enumeration."""

import time
from collections import defaultdict

from depgraph.graph.paths import find_paths


def parents_of(*edges: tuple[int, int] | tuple[int, int, str]) -> dict:
    """Build the parents map from (parent, child[, dep_type]) edges."""
    out: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for e in edges:
        parent, child, dep_type = (*e, "prod") if len(e) == 2 else e
        out[child].append((parent, dep_type))
    return out


def ids(paths) -> list[list[int]]:
    return [[node for node, _ in p] for p in paths]


def test_single_chain():
    r = find_paths(parents_of((0, 1), (1, 2)), 0, [2], max_paths=10, max_depth=10)
    assert r.paths == [[(0, None), (1, "prod"), (2, "prod")]]
    assert not r.truncated


def test_target_is_root():
    r = find_paths({}, 0, [0], max_paths=10, max_depth=10)
    assert r.paths == [[(0, None)]]


def test_unreachable_target_has_no_paths():
    r = find_paths(parents_of((5, 6)), 0, [6], max_paths=10, max_depth=10)
    assert r.paths == [] and not r.truncated


def test_shortest_first_across_branches_and_targets():
    # 0 -> 3 directly; 0 -> 1 -> 2 -> 3; and a second target 4 at depth 2.
    p = parents_of((0, 3), (0, 1), (1, 2), (2, 3), (1, 4))
    r = find_paths(p, 0, [3, 4], max_paths=10, max_depth=10)
    assert ids(r.paths) == [[0, 3], [0, 1, 4], [0, 1, 2, 3]]
    lengths = [len(x) for x in r.paths]
    assert lengths == sorted(lengths)


def test_dep_type_labels_the_edge_into_each_step():
    p = parents_of((0, 1, "dev"), (1, 2, "optional"))
    (path,) = find_paths(p, 0, [2], max_paths=10, max_depth=10).paths
    assert path == [(0, None), (1, "dev"), (2, "optional")]


def test_parallel_edges_of_different_types_are_distinct_paths():
    p = parents_of((0, 1, "prod"), (0, 1, "peer"))
    r = find_paths(p, 0, [1], max_paths=10, max_depth=10)
    assert sorted(r.paths) == [[(0, None), (1, "peer")], [(0, None), (1, "prod")]]


def test_cycles_terminate_and_paths_stay_simple():
    # root -> a <-> b, b -> target
    p = parents_of((0, 1), (1, 2), (2, 1), (2, 3))
    r = find_paths(p, 0, [3], max_paths=10, max_depth=10)
    assert ids(r.paths) == [[0, 1, 2, 3]]
    assert all(len(set(x)) == len(x) for x in ids(r.paths))


def test_cycle_through_the_root_terminates():
    p = parents_of((0, 1), (1, 0))
    assert ids(find_paths(p, 0, [1], max_paths=10, max_depth=10).paths) == [[0, 1]]


def test_max_paths_caps_and_reports_truncation():
    # 0 -> {1,2,3} -> 4: three paths
    p = parents_of((0, 1), (0, 2), (0, 3), (1, 4), (2, 4), (3, 4))
    r = find_paths(p, 0, [4], max_paths=2, max_depth=10)
    assert len(r.paths) == 2 and r.truncated
    full = find_paths(p, 0, [4], max_paths=3, max_depth=10)
    assert len(full.paths) == 3


def test_max_depth_drops_long_paths_and_reports_it():
    p = parents_of((0, 1), (1, 2), (2, 3), (0, 3))
    r = find_paths(p, 0, [3], max_paths=10, max_depth=2)
    assert ids(r.paths) == [[0, 3]]
    assert r.truncated  # the 3-edge path was cut


def test_exact_depth_limit_is_not_truncation():
    r = find_paths(parents_of((0, 1), (1, 2)), 0, [2], max_paths=10, max_depth=2)
    assert len(r.paths) == 1 and not r.truncated


def test_exponential_diamonds_are_bounded():
    # 30 stacked diamonds: 2**30 (~1e9) distinct root->target paths, all equal
    # length. Plain BFS found zero paths here before exhausting its budget.
    p, top = _diamonds(30)
    started = time.perf_counter()
    r = find_paths(p, 0, [top], max_paths=50, max_depth=100, max_expansions=100_000)
    assert time.perf_counter() - started < 5
    assert len(r.paths) == 50 and r.truncated
    assert all(len(x) == 61 for x in r.paths)  # every path crosses all 30 diamonds


def test_dead_end_ancestors_cost_nothing():
    # 499 parents of the target that the root can't reach: pruned before any
    # expansion is spent on them, so a budget of 2 suffices.
    edges = [(i, 1000) for i in range(2, 500)] + [(0, 1), (1, 1000)]
    r = find_paths(parents_of(*edges), 0, [1000], max_paths=10, max_depth=10, max_expansions=2)
    assert ids(r.paths) == [[0, 1, 1000]] and not r.truncated


def _diamonds(n: int) -> tuple[dict, int]:
    edges, top = [], 0
    for layer in range(n):
        left, right, bottom = 3 * layer + 1, 3 * layer + 2, 3 * layer + 3
        edges += [(top, left), (top, right), (left, bottom), (right, bottom)]
        top = bottom
    return parents_of(*edges), top


def test_expansion_budget_stops_the_search():
    p, target = _diamonds(30)
    r = find_paths(p, 0, [target], max_paths=1000, max_depth=100, max_expansions=200)
    assert r.truncated
    assert 0 < len(r.paths) < 1000


def test_work_scales_with_paths_returned_not_paths_existing():
    p, target = _diamonds(30)
    # Enumerating leaves of a binary branch tree costs ~2 node visits per leaf,
    # each visit expanding 2 parents: a small constant per path (~4.6 measured).
    r = find_paths(p, 0, [target], max_paths=100, max_depth=100, max_expansions=100 * 8)
    assert len(r.paths) == 100
