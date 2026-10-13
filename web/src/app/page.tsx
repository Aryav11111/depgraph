import { ApiStatus } from "@/components/api-status";

// Placeholder home page for step 1: proves the app builds and talks to the API
// through the generated, typed client. The upload page replaces it in step 2.
export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col justify-center gap-6 px-4 py-16 sm:px-6">
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">depgraph</h1>
      <p className="max-w-prose text-zinc-600 dark:text-zinc-400">
        Upload a <code className="font-mono text-[0.9em]">package-lock.json</code> and see every
        vulnerable or malicious package in your dependency tree, how it got there, and the smallest
        change that fixes it.
      </p>
      <ApiStatus />
    </main>
  );
}
