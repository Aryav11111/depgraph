"use client";

import { useState } from "react";

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setState("copied");
        } catch {
          setState("failed"); // clipboard needs a secure context + permission
        }
        setTimeout(() => setState("idle"), 1500);
      }}
      className="shrink-0 rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
      aria-live="polite"
    >
      {state === "copied" ? "Copied" : state === "failed" ? "Select & copy" : label}
    </button>
  );
}

/** A command or snippet with a copy button. */
export function CodeBlock({ code }: { code: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg bg-zinc-950 p-3 text-zinc-100">
      <pre className="min-w-0 flex-1 overflow-x-auto font-mono text-xs leading-relaxed">
        <code>{code}</code>
      </pre>
      <CopyButton text={code} />
    </div>
  );
}
