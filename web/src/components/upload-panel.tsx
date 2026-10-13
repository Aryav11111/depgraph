"use client";

import { useRouter } from "next/navigation";
import { useCallback, useId, useRef, useState } from "react";

import { SlowHint } from "./states";

import { createScan } from "@/lib/api/scans";
import { ApiError } from "@/lib/api/errors";
import { checkSize, precheckLockfile } from "@/lib/lockfile-check";
import { SAMPLES, type Sample } from "@/lib/samples";

type State =
  | { kind: "idle" }
  | { kind: "busy"; label: string }
  | { kind: "error"; message: string };

export function UploadPanel() {
  const router = useRouter();
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<State>({ kind: "idle" });
  const [dragging, setDragging] = useState(false);
  const busy = state.kind === "busy";
  const inflight = useRef<AbortController | null>(null);

  const submit = useCallback(
    async (blob: Blob, label: string) => {
      const controller = new AbortController();
      inflight.current = controller;
      setState({ kind: "busy", label });
      try {
        const scan = await createScan(blob, controller.signal);
        router.push(`/scans/${scan.id}`);
      } catch (err) {
        if (controller.signal.aborted) return setState({ kind: "idle" });
        setState({
          kind: "error",
          message: err instanceof ApiError ? err.message : "Something went wrong. Try again.",
        });
      } finally {
        inflight.current = null;
      }
    },
    [router],
  );

  const handleFile = useCallback(
    async (file: File) => {
      const sizeError = checkSize(file.size);
      if (sizeError) return setState({ kind: "error", message: sizeError });
      const check = precheckLockfile(await file.text(), file.name);
      if (!check.ok) return setState({ kind: "error", message: check.error });
      await submit(file, `Resolving ${check.packageCount.toLocaleString()} packages…`);
    },
    [submit],
  );

  const handleSample = useCallback(
    async (sample: Sample) => {
      setState({ kind: "busy", label: `Loading ${sample.title}…` });
      try {
        const res = await fetch(sample.file);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await submit(await res.blob(), `Scanning ${sample.title}…`);
      } catch {
        setState({ kind: "error", message: "Couldn't load that sample. Try again." });
      }
    },
    [submit],
  );

  return (
    <div className="flex flex-col gap-8">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const file = e.dataTransfer.files[0];
          if (file && !busy) void handleFile(file);
        }}
        className={[
          "flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed px-6 py-12 text-center transition-colors",
          dragging
            ? "border-indigo-500 bg-indigo-50 dark:bg-indigo-950/40"
            : "border-zinc-300 dark:border-zinc-700",
          busy ? "opacity-60" : "",
        ].join(" ")}
      >
        <UploadIcon />
        {/* "Drop" means nothing on a phone: touch screens get different wording. */}
        <p className="text-lg font-medium pointer-coarse:hidden">
          Drop your <code className="font-mono text-[0.95em]">package-lock.json</code> here
        </p>
        <label htmlFor={inputId} className="hidden cursor-pointer text-lg font-medium pointer-coarse:block">
          Choose your <code className="font-mono text-[0.95em]">package-lock.json</code>
        </label>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          or{" "}
          <label
            htmlFor={inputId}
            className="cursor-pointer font-medium text-indigo-600 underline-offset-4 hover:underline dark:text-indigo-400"
          >
            choose a file
          </label>
          . It&apos;s parsed on our server, never executed.
        </p>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept=".json,application/json"
          className="sr-only"
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void handleFile(file);
            e.target.value = ""; // allow re-selecting the same file after an error
          }}
        />
      </div>

      <StatusLine state={state} onCancel={() => inflight.current?.abort()} />

      <section aria-labelledby="samples-heading" className="flex flex-col gap-3">
        <h2 id="samples-heading" className="text-sm font-medium uppercase tracking-wide text-zinc-600 dark:text-zinc-400">
          No lockfile handy? Try a sample
        </h2>
        <div className="grid gap-3 sm:grid-cols-3">
          {SAMPLES.map((sample) => (
            <button
              key={sample.id}
              type="button"
              disabled={busy}
              onClick={() => void handleSample(sample)}
              className="flex min-h-11 flex-col gap-1 rounded-xl border border-zinc-200 p-4 text-left transition hover:border-indigo-400 hover:shadow-sm focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-800 dark:hover:border-indigo-500"
            >
              <span className="font-medium">{sample.title}</span>
              <span className="text-sm text-zinc-600 dark:text-zinc-400">{sample.blurb}</span>
              <span className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">{sample.highlights}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

function StatusLine({ state, onCancel }: { state: State; onCancel: () => void }) {
  if (state.kind === "idle") return null;
  if (state.kind === "busy") {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <p role="status" aria-live="polite" className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-400">
            <span className="size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-indigo-500" aria-hidden />
            {state.label}
          </p>
          <button
            type="button"
            onClick={onCancel}
            className="min-h-11 rounded-lg px-3 text-sm font-medium text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900"
          >
            Cancel
          </button>
        </div>
        <SlowHint active />
      </div>
    );
  }
  return (
    <p
      role="alert"
      className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300"
    >
      {state.message}
    </p>
  );
}

function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-10 text-zinc-500 dark:text-zinc-400" fill="none" stroke="currentColor" strokeWidth={1.5} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
    </svg>
  );
}
