"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  Controls,
  type Edge,
  Handle,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import Link from "next/link";
import { memo, useEffect, useMemo, useState } from "react";

import type { FindingSummary, Graph, Scan, Severity } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { getFindings, getGraph, getScan } from "@/lib/api/scans";
import {
  adjacency,
  depths,
  edgeKey,
  type Highlight,
  pathsToTarget,
  radialLayout,
  visibleNodes,
} from "@/lib/graph-model";
import { SEVERITY_ORDER, SEVERITY_STYLE } from "@/lib/severity";

import { ScanTabs } from "./scan-tabs";

const LARGE = 600; // nodes; above this, ask before rendering
const SEVERITY_STROKE: Record<Severity, string> = {
  CRITICAL: "#dc2626",
  HIGH: "#f97316",
  MODERATE: "#f59e0b",
  LOW: "#0ea5e9",
  UNKNOWN: "#a1a1aa",
};

type Loaded = { scan: Scan; graph: Graph; findings: FindingSummary[] };
type State = { kind: "loading" } | { kind: "error"; message: string } | ({ kind: "ok" } & Loaded);

export function GraphView({ scanId }: { scanId: string }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      getScan(scanId, controller.signal),
      getGraph(scanId, controller.signal),
      getFindings(scanId, controller.signal),
    ])
      .then(([scan, graph, f]) => setState({ kind: "ok", scan, graph, findings: f.findings }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setState({ kind: "error", message: err instanceof ApiError ? err.message : "Couldn't load the graph." });
      });
    return () => controller.abort();
  }, [scanId]);

  return (
    <div className="flex flex-col gap-4">
      <ScanTabs scanId={scanId} />
      {state.kind === "loading" && (
        <div className="h-[65vh] min-h-[420px] animate-pulse rounded-xl bg-zinc-100 dark:bg-zinc-900" aria-busy="true" aria-label="Loading graph" />
      )}
      {state.kind === "error" && (
        <p role="alert" className="text-red-700 dark:text-red-400">
          {state.message}
        </p>
      )}
      {state.kind === "ok" && (
        <ReactFlowProvider>
          <GraphExplorer scanId={scanId} {...state} />
        </ReactFlowProvider>
      )}
    </div>
  );
}

function GraphExplorer({ scanId, scan, graph, findings }: Loaded & { scanId: string }) {
  const { fitView } = useReactFlow();

  // ----- derived model (pure, memoised once per scan) -----
  const model = useMemo(() => {
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const adj = adjacency(graph.edges);
    const depth = depths(graph.root, adj);
    const idByKey = new Map(graph.nodes.map((n) => [`${n.name}@${n.version}`, n.id]));
    const findingsOn = new Map<number, FindingSummary[]>();
    for (const f of findings) {
      const id = idByKey.get(`${f.package}@${f.version}`);
      if (id === undefined) continue;
      if (!findingsOn.has(id)) findingsOn.set(id, []);
      findingsOn.get(id)!.push(f);
    }
    const worst = new Map<number, Severity>();
    for (const [id, fs] of findingsOn) {
      worst.set(id, SEVERITY_ORDER.find((s) => fs.some((f) => f.advisory.severity === s)) ?? "UNKNOWN");
    }
    const focus: Highlight[] = [...findingsOn.keys()].map((id) => pathsToTarget(graph.root, id, adj, graph.edges));
    const maxDepth = Math.max(1, ...depth.values());
    return { byId, adj, depth, idByKey, findingsOn, worst, focus, maxDepth };
  }, [graph, findings]);

  const [depthLimit, setDepthLimit] = useState(Math.min(2, model.maxDepth));
  const [allowLarge, setAllowLarge] = useState(false);
  const [selectedFinding, setSelectedFinding] = useState<FindingSummary | null>(null);
  const [selectedNode, setSelectedNode] = useState<number | null>(null);

  const visible = useMemo(
    () => visibleNodes(model.depth, depthLimit, model.focus),
    [model, depthLimit],
  );
  const tooLarge = visible.size > LARGE && !allowLarge;

  const positions = useMemo(
    () => radialLayout(graph.root, visible, model.depth, model.adj, (id) => model.byId.get(id)?.name ?? ""),
    [graph.root, visible, model],
  );

  const highlight = useMemo<Highlight | null>(() => {
    if (!selectedFinding) return null;
    const target = model.idByKey.get(`${selectedFinding.package}@${selectedFinding.version}`);
    return target === undefined ? null : pathsToTarget(graph.root, target, model.adj, graph.edges);
  }, [selectedFinding, model, graph]);

  const nodes = useMemo<Node<PkgData>[]>(
    () =>
      [...visible].flatMap((id) => {
        const n = model.byId.get(id);
        const p = positions.get(id);
        if (!n || !p) return [];
        return [{
          id: String(id),
          type: "pkg",
          position: p,
          data: {
            name: n.name,
            version: n.version,
            isRoot: id === graph.root,
            dev: n.dev,
            severity: model.worst.get(id) ?? null,
            dimmed: highlight ? !highlight.nodes.has(id) : false,
            selected: id === selectedNode,
          },
        }];
      }),
    [visible, positions, model, graph.root, highlight, selectedNode],
  );

  const edges = useMemo<Edge[]>(
    () =>
      graph.edges
        .filter((e) => visible.has(e.parent) && visible.has(e.child))
        .map((e) => {
          const lit = highlight?.edges.has(edgeKey(e.parent, e.child)) ?? false;
          return {
            id: `${edgeKey(e.parent, e.child)}:${e.dep_type}`,
            source: String(e.parent),
            target: String(e.child),
            type: "straight",
            style: {
              stroke: lit ? "#4f46e5" : "#a1a1aa",
              strokeWidth: lit ? 2.5 : 1,
              strokeDasharray: e.dep_type === "prod" ? undefined : "4 3",
              opacity: highlight && !lit ? 0.12 : 0.7,
            },
          };
        }),
    [graph.edges, visible, highlight],
  );

  // Zoom to the highlighted paths when a finding is picked.
  useEffect(() => {
    if (!highlight || tooLarge) return;
    const ids = [...highlight.nodes].map((id) => ({ id: String(id) }));
    const t = setTimeout(() => fitView({ nodes: ids, padding: 0.25, duration: 400 }), 50);
    return () => clearTimeout(t);
  }, [highlight, fitView, tooLarge]);

  const nodeInfo = selectedNode !== null ? model.byId.get(selectedNode) : undefined;
  const sorted = useMemo(
    () => [...findings].sort((a, b) => SEVERITY_ORDER.indexOf(a.advisory.severity) - SEVERITY_ORDER.indexOf(b.advisory.severity)),
    [findings],
  );

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
      {/* min-w-0: grid items default to min-width:auto and would grow past the
          viewport on phones (clipping the toolbar and skewing fitView). */}
      <div className="flex min-w-0 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
          <label className="inline-flex items-center gap-2">
            Depth
            <input
              type="range"
              min={1}
              max={model.maxDepth}
              value={depthLimit}
              onChange={(e) => setDepthLimit(Number(e.target.value))}
              className="w-32 accent-indigo-600"
              aria-valuetext={`${depthLimit} of ${model.maxDepth}`}
            />
            <span className="tabular-nums text-zinc-500">
              {depthLimit}/{model.maxDepth}
            </span>
          </label>
          <span className="text-zinc-500">
            {visible.size.toLocaleString()} of {model.depth.size.toLocaleString()} packages shown
            {model.findingsOn.size > 0 && " (vulnerable paths always included)"}
          </span>
          {selectedFinding && (
            <button type="button" onClick={() => setSelectedFinding(null)} className="font-medium text-indigo-600 hover:underline dark:text-indigo-400">
              Clear highlight
            </button>
          )}
        </div>

        <div className="relative h-[65vh] min-h-[420px] overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
          {tooLarge ? (
            <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
              <p>
                That&apos;s {visible.size.toLocaleString()} packages. Rendering them all may be slow and hard to read.
              </p>
              <div className="flex gap-2">
                <button type="button" onClick={() => setDepthLimit((d) => Math.max(1, d - 1))} className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm dark:border-zinc-700">
                  Reduce depth
                </button>
                <button type="button" onClick={() => setAllowLarge(true)} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm text-white">
                  Show anyway
                </button>
              </div>
            </div>
          ) : (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={NODE_TYPES}
              fitView
              minZoom={0.05}
              maxZoom={2}
              onlyRenderVisibleElements
              nodesDraggable={false}
              nodesConnectable={false}
              edgesFocusable={false}
              onNodeClick={(_, n) => setSelectedNode(Number(n.id))}
              onPaneClick={() => setSelectedNode(null)}
              proOptions={{ hideAttribution: false }}
            >
              <Background gap={24} size={1} />
              <Controls showInteractive={false} />
            </ReactFlow>
          )}
        </div>
        <Legend />
      </div>

      <aside className="flex min-w-0 flex-col gap-4">
        {nodeInfo && (
          <div className="rounded-xl border border-zinc-200 p-3 text-sm dark:border-zinc-800">
            <p className="font-mono font-medium">
              {nodeInfo.name}
              {nodeInfo.version && <span className="text-zinc-500">@{nodeInfo.version}</span>}
            </p>
            <p className="text-zinc-500">
              depth {model.depth.get(nodeInfo.id) ?? "?"}
              {nodeInfo.dev && " · dev-only"}
            </p>
            {(model.findingsOn.get(nodeInfo.id) ?? []).map((f) => (
              <Link key={f.id} href={`/scans/${scanId}/findings/${f.id}`} className="mt-1 block text-indigo-600 hover:underline dark:text-indigo-400">
                {SEVERITY_STYLE[f.advisory.severity].label}: {f.advisory.summary ?? f.advisory.id}
              </Link>
            ))}
          </div>
        )}

        <div className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">
            Findings <span className="font-normal text-zinc-500">· click to trace its paths</span>
          </h2>
          {sorted.length === 0 ? (
            <p className="text-sm text-zinc-500">No findings in {scan.root_name ?? "this project"}.</p>
          ) : (
            <ul className="flex max-h-[60vh] flex-col gap-1 overflow-y-auto pr-1">
              {sorted.map((f) => {
                const on = selectedFinding?.id === f.id;
                return (
                  <li key={f.id}>
                    <button
                      type="button"
                      aria-pressed={on}
                      onClick={() => setSelectedFinding(on ? null : f)}
                      className={[
                        "flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition",
                        on ? "bg-indigo-50 ring-1 ring-indigo-300 dark:bg-indigo-950/50 dark:ring-indigo-800" : "hover:bg-zinc-50 dark:hover:bg-zinc-900",
                      ].join(" ")}
                    >
                      <span className={`mt-1.5 size-2 shrink-0 rounded-full ${SEVERITY_STYLE[f.advisory.severity].dot}`} aria-hidden />
                      <span className="min-w-0">
                        <span className="font-mono">{f.package}@{f.version}</span>
                        <span className="block truncate text-xs text-zinc-500">{f.advisory.summary ?? f.advisory.id}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </aside>
    </div>
  );
}

// ------------------------------------------------------------- node + legend

type PkgData = {
  name: string;
  version: string | null;
  isRoot: boolean;
  dev: boolean;
  severity: Severity | null;
  dimmed: boolean;
  selected: boolean;
};

// Handles at the centre so straight edges join node centres (radial look).
const HANDLE_STYLE = { left: "50%", top: "50%", opacity: 0, pointerEvents: "none" as const };

const PkgNode = memo(function PkgNode({ data }: NodeProps<Node<PkgData>>) {
  const ring = data.severity ? SEVERITY_STROKE[data.severity] : data.isRoot ? "#4f46e5" : "#d4d4d8";
  return (
    <div
      title={`${data.name}${data.version ? "@" + data.version : ""}`}
      className={[
        "max-w-40 truncate rounded-full border-2 bg-white px-2.5 py-1 font-mono text-[11px] shadow-sm transition-opacity dark:bg-zinc-900",
        data.isRoot ? "px-4 py-2 text-sm font-semibold" : "",
        data.selected ? "ring-2 ring-indigo-500 ring-offset-1" : "",
        data.dimmed ? "opacity-15" : "opacity-100",
        data.dev && !data.severity ? "border-dashed" : "",
      ].join(" ")}
      style={{ borderColor: ring }}
    >
      <Handle type="target" position={Position.Top} style={HANDLE_STYLE} isConnectable={false} />
      {data.name}
      <Handle type="source" position={Position.Bottom} style={HANDLE_STYLE} isConnectable={false} />
    </div>
  );
});

const NODE_TYPES = { pkg: PkgNode };

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-500">
      {SEVERITY_ORDER.slice(0, 4).map((s) => (
        <span key={s} className="inline-flex items-center gap-1.5">
          <span className="size-3 rounded-full border-2 bg-white" style={{ borderColor: SEVERITY_STROKE[s] }} aria-hidden />
          {SEVERITY_STYLE[s].label}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <span className="h-0 w-5 border-t border-dashed border-zinc-400" aria-hidden />
        dev / optional / peer edge
      </span>
      <span>Root at centre; rings = distance from your project.</span>
    </div>
  );
}
