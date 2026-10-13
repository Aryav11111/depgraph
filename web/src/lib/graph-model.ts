/**
 * Pure graph logic for the dependency-graph view: depths, path highlighting,
 * which nodes to show, and a radial layout with the project root at the centre.
 * No React, no React Flow: everything here is unit-tested.
 */

export type GNode = { id: number; name: string; version: string | null; dev: boolean };
export type GEdge = { parent: number; child: number; dep_type: string };

export type Adjacency = {
  children: Map<number, number[]>;
  parents: Map<number, number[]>;
};

export function adjacency(edges: GEdge[]): Adjacency {
  const children = new Map<number, number[]>();
  const parents = new Map<number, number[]>();
  for (const e of edges) {
    if (!children.has(e.parent)) children.set(e.parent, []);
    if (!parents.has(e.child)) parents.set(e.child, []);
    children.get(e.parent)!.push(e.child);
    parents.get(e.child)!.push(e.parent);
  }
  return { children, parents };
}

function reach(start: number[], next: Map<number, number[]>): Set<number> {
  const seen = new Set(start);
  const queue = [...start];
  while (queue.length) {
    for (const n of next.get(queue.shift()!) ?? []) {
      if (!seen.has(n)) {
        seen.add(n);
        queue.push(n);
      }
    }
  }
  return seen;
}

/** Shortest hop count from the root (BFS). Nodes the root can't reach are absent. */
export function depths(root: number, adj: Adjacency): Map<number, number> {
  const depth = new Map([[root, 0]]);
  const queue = [root];
  while (queue.length) {
    const n = queue.shift()!;
    for (const c of adj.children.get(n) ?? []) {
      if (!depth.has(c)) {
        depth.set(c, depth.get(n)! + 1);
        queue.push(c);
      }
    }
  }
  return depth;
}

export type Highlight = { nodes: Set<number>; edges: Set<string> };
export const edgeKey = (parent: number, child: number) => `${parent}->${child}`;

/**
 * Every edge on some root → target route: reachable from the root *and* able to
 * reach the target. Two linear passes, exact and uncapped (the detail page lists
 * individual paths; this shows their union). With cycles it can include an edge
 * that only lies on a non-simple walk, which is acceptable for a visual highlight.
 */
export function pathsToTarget(root: number, target: number, adj: Adjacency, edges: GEdge[]): Highlight {
  const fromRoot = reach([root], adj.children);
  const toTarget = reach([target], adj.parents);
  if (!fromRoot.has(target)) return { nodes: new Set(), edges: new Set() };
  const nodes = new Set([...fromRoot].filter((n) => toTarget.has(n)));
  const keys = new Set<string>();
  for (const e of edges) {
    if (nodes.has(e.parent) && nodes.has(e.child)) keys.add(edgeKey(e.parent, e.child));
  }
  return { nodes, edges: keys };
}

/** Nodes within `maxDepth` hops, plus every node on a path to a focus node. */
export function visibleNodes(
  depth: Map<number, number>,
  maxDepth: number,
  focus: Highlight[],
): Set<number> {
  const visible = new Set<number>();
  for (const [n, d] of depth) if (d <= maxDepth) visible.add(n);
  for (const h of focus) for (const n of h.nodes) visible.add(n);
  return visible;
}

export type Point = { x: number; y: number };

/**
 * Radial layout: ring k holds the visible nodes at depth k. Ring 1 is spread
 * evenly (sorted by name); deeper rings place each node at the mean angle of its
 * visible parents, then re-space the ring in that order. Children sit "under"
 * their parents, which keeps crossings down without a force simulation, and
 * the result is deterministic (same graph, same picture).
 */
export function radialLayout(
  root: number,
  visible: Set<number>,
  depth: Map<number, number>,
  adj: Adjacency,
  nameOf: (id: number) => string,
  opts: { ringGap?: number; minSpacing?: number } = {},
): Map<number, Point> {
  const ringGap = opts.ringGap ?? 220;
  const minSpacing = opts.minSpacing ?? 120;
  const pos = new Map<number, Point>([[root, { x: 0, y: 0 }]]);
  const angle = new Map<number, number>([[root, 0]]);

  const rings = new Map<number, number[]>();
  for (const n of visible) {
    const d = depth.get(n);
    if (d === undefined || d === 0) continue;
    if (!rings.has(d)) rings.set(d, []);
    rings.get(d)!.push(n);
  }

  let radius = 0;
  for (const d of [...rings.keys()].sort((a, b) => a - b)) {
    const ring = rings.get(d)!;
    const desired = new Map<number, number>();
    for (const n of ring) {
      const ps = (adj.parents.get(n) ?? []).filter((p) => angle.has(p) && (depth.get(p) ?? Infinity) < d);
      desired.set(n, ps.length ? circularMean(ps.map((p) => angle.get(p)!)) : 0);
    }
    ring.sort(
      (a, b) =>
        desired.get(a)! - desired.get(b)! || nameOf(a).localeCompare(nameOf(b)) || a - b,
    );
    // Radius grows with depth, and enough to fit the ring's nodes at minSpacing.
    radius = Math.max(radius + ringGap, (ring.length * minSpacing) / (2 * Math.PI));
    const offset = d === 1 ? -Math.PI / 2 : desired.get(ring[0])!; // ring 1 starts at 12 o'clock
    ring.forEach((n, i) => {
      const a = offset + (2 * Math.PI * i) / ring.length;
      angle.set(n, a);
      pos.set(n, { x: Math.round(radius * Math.cos(a)), y: Math.round(radius * Math.sin(a)) });
    });
  }
  return pos;
}

function circularMean(angles: number[]): number {
  const x = angles.reduce((s, a) => s + Math.cos(a), 0);
  const y = angles.reduce((s, a) => s + Math.sin(a), 0);
  const m = Math.atan2(y, x);
  return m < 0 ? m + 2 * Math.PI : m;
}
