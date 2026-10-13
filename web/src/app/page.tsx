import { UploadPanel } from "@/components/upload-panel";

// Mirrors the API's SCAN_RETENTION_DAYS (set for the public demo).
const RETENTION_DAYS = Number(process.env.NEXT_PUBLIC_SCAN_RETENTION_DAYS ?? 0);

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col gap-10 px-4 py-12 sm:px-6 sm:py-16">
      <div className="flex flex-col gap-4">
        <h1 className="text-3xl font-semibold tracking-tight sm:text-5xl">
          What&apos;s really in your <span className="text-indigo-600 dark:text-indigo-400">node_modules</span>?
        </h1>
        <p className="max-w-2xl text-lg text-zinc-600 dark:text-zinc-400">
          Upload a <code className="font-mono text-[0.9em]">package-lock.json</code> to find every vulnerable or
          known-malicious package in your dependency tree, see exactly how each one got there, and get the
          smallest change that fixes it.
        </p>
      </div>
      <UploadPanel />
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        The server keeps the resolved dependency graph, never the file itself
        {RETENTION_DAYS > 0 ? `, and deletes each scan after ${RETENTION_DAYS} days` : ""}. Scan links are
        unguessable but not private: anyone you share one with can see it.
      </p>
    </main>
  );
}
