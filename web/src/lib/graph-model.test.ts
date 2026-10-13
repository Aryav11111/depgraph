import { describe, expect, it } from "vitest";

import {
  adjacency,
  depths,
  edgeKey,
  type GEdge,
  pathsToTarget,
  radialLayout,
  visibleNodes,
} from "./graph-model";

const e = (parent: number, child: number, dep_type = "prod"): GEdge => ({ parent, child, dep_type });

// 0 = root. 0→1→3→5, 0→2→3 (diamond), 3→4, 4→3 (cycle), 6 isolated, 7→5 (7 unreachable)
const EDGES = [e(0, 1), e(0, 2), e(1, 3), e(2, 3), e(3, 4), e(4, 3), e(3, 5), e(7, 5)];
const ADJ = adjacency(EDGES);

describe("depths", () => {
  it("is the shortest hop count from the root; unreachable nodes are absent", () => {
    const d = depths(0, ADJ);
    expect(Object.fromEntries(d)).toEqual({ 0: 0, 1: 1, 2: 1, 3: 2, 4: 3, 5: 3 });
    expect(d.has(6)).toBe(false);
    expect(d.has(7)).toBe(false);
  });
});

describe("pathsToTarget", () => {
  it("highlights the union of every root→target route (diamond)", () => {
    const h = pathsToTarget(0, 5, ADJ, EDGES);
    expect([...h.nodes].sort()).toEqual([0, 1, 2, 3, 4, 5]); // 4 via the 3↔4 cycle
    expect(h.edges.has(edgeKey(0, 1)) && h.edges.has(edgeKey(0, 2))).toBe(true);
    expect(h.edges.has(edgeKey(3, 5))).toBe(true);
    expect(h.edges.has(edgeKey(7, 5))).toBe(false); // 7 isn't reachable from the root
  });

  it("terminates on cycles and excludes unrelated branches", () => {
    const h = pathsToTarget(0, 1, ADJ, EDGES);
    expect([...h.nodes].sort()).toEqual([0, 1]);
    expect([...h.edges]).toEqual([edgeKey(0, 1)]);
  });

  it("an unreachable target highlights nothing", () => {
    expect(pathsToTarget(0, 7, ADJ, EDGES).nodes.size).toBe(0);
  });
});

describe("visibleNodes", () => {
  const d = depths(0, ADJ);
  it("depth limit alone", () => {
    expect([...visibleNodes(d, 1, [])].sort()).toEqual([0, 1, 2]);
  });
  it("always includes paths to focus nodes beyond the limit", () => {
    const focus = pathsToTarget(0, 5, ADJ, EDGES);
    expect([...visibleNodes(d, 1, [focus])].sort()).toEqual([0, 1, 2, 3, 4, 5]);
  });
});

describe("radialLayout", () => {
  const d = depths(0, ADJ);
  const all = new Set(d.keys());
  const name = (id: number) => `pkg${id}`;
  const pos = radialLayout(0, all, d, ADJ, name);

  it("puts the root at the centre and places every visible node", () => {
    expect(pos.get(0)).toEqual({ x: 0, y: 0 });
    expect([...pos.keys()].sort()).toEqual([...all].sort());
  });

  it("deeper nodes sit on larger rings", () => {
    const r = (id: number) => Math.hypot(pos.get(id)!.x, pos.get(id)!.y);
    expect(r(1)).toBeCloseTo(r(2), 0);
    expect(r(3)).toBeGreaterThan(r(1));
    expect(r(5)).toBeGreaterThan(r(3));
  });

  it("is deterministic", () => {
    expect(radialLayout(0, all, d, ADJ, name)).toEqual(pos);
  });

  it("no two nodes on a ring overlap, even on a crowded ring", () => {
    const fan = Array.from({ length: 200 }, (_, i) => e(0, i + 1));
    const adj = adjacency(fan);
    const dd = depths(0, adj);
    const p = radialLayout(0, new Set(dd.keys()), dd, adj, name, { minSpacing: 120 });
    const pts = [...p.entries()].filter(([id]) => id !== 0).map(([, pt]) => pt);
    let minDist = Infinity;
    for (let i = 0; i < pts.length; i++) {
      const j = (i + 1) % pts.length;
      minDist = Math.min(minDist, Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y));
    }
    expect(minDist).toBeGreaterThan(110); // ≈ minSpacing (rounding)
  });
});
