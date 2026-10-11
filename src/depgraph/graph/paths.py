"""Enumerate dependency paths from the project root to target nodes.

Pure and bounded. Path counts grow exponentially with "diamonds" (30 stacked
diamonds = 2^30 paths, all the same length), so the search must (a) return paths
shortest first, (b) stop after `max_paths`, and (c) do work proportional to what
it returns, not to what exists.

Plain BFS gets (a) but fails (c): with many equal-length paths it must hold every
partial path of length L before finishing any of length L+1. (The 30-diamond test
returned zero paths under BFS.) So this is a best-first search (A*):

* Partial paths grow backwards from the targets. Each is ranked by
  f = edges so far + exact distance from the root to its head node
  (precomputed by one forward BFS). That bound never overestimates, so
  completed paths come out in non-decreasing length: shortest first.
* Among equal f, the deepest partial path goes first: the search runs
  depth-first along optimal routes, so each extra path costs a handful of steps.
* Heads with no route to the root are dropped immediately.

Only *simple* paths are followed (no node twice), so cycles terminate. Caps:
`max_paths` results, `max_depth` edges per path, `max_expansions` steps of work.
`truncated` reports that a cap stopped the search, i.e. more paths *may* exist.
"""

import heapq
import itertools
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# A step is (node_id, dep_type of the edge *into* this node); the root has None.
Step = tuple[int, str | None]
Parents = Mapping[int, Sequence[tuple[int, str]]]


@dataclass(frozen=True, slots=True)
class PathSearch:
    paths: list[list[Step]]
    truncated: bool


def find_paths(
    parents: Parents,
    root: int,
    targets: Iterable[int],
    *,
    max_paths: int,
    max_depth: int,
    max_expansions: int = 200_000,
) -> PathSearch:
    """`parents[child]` lists `(parent, dep_type)` for every edge parent -> child."""
    dist = _distances_from_root(parents, root)
    tiebreak = itertools.count()
    # Heap entries: (f, -g, tiebreak, head, steps, seen). steps run target-first;
    # the last step's dep_type is filled in when the path is extended past it.
    heap: list = []
    for t in sorted(set(targets)):
        if t in dist:
            heap.append((dist[t], 0, next(tiebreak), t, ((t, None),), frozenset((t,))))
    heapq.heapify(heap)

    found: list[list[Step]] = []
    expansions = 0
    truncated = False

    while heap:
        f, neg_g, _, head, steps, seen = heapq.heappop(heap)
        if f > max_depth:
            # Everything left is at least this long: the depth cap cuts it all.
            return PathSearch(found, truncated=True)
        if head == root:
            found.append(list(reversed(steps)))
            if len(found) >= max_paths:
                return PathSearch(found, truncated=bool(heap) or truncated)
            continue

        g = -neg_g + 1
        for parent, dep_type in sorted(parents.get(head, ())):
            if parent in seen or parent not in dist:
                continue  # a cycle, or a dead end that never reaches the root
            expansions += 1
            if expansions > max_expansions:
                return PathSearch(found, truncated=True)
            labelled = (*steps[:-1], (head, dep_type), (parent, None))
            entry = (g + dist[parent], -g, next(tiebreak), parent, labelled, seen | {parent})
            heapq.heappush(heap, entry)

    return PathSearch(found, truncated=truncated)


def _distances_from_root(parents: Parents, root: int) -> dict[int, int]:
    """Shortest edge count root -> node, for nodes in `parents` reachable from root."""
    children: dict[int, list[int]] = defaultdict(list)
    for child, links in parents.items():
        for parent, _ in links:
            children[parent].append(child)
    dist = {root: 0}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        for child in children[node]:
            if child not in dist:
                dist[child] = dist[node] + 1
                queue.append(child)
    return dist
