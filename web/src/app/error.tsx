"use client"; // error boundaries must be Client Components

import { ErrorState } from "@/components/states";

/**
 * Catches render errors in any page below the root layout. In production Next
 * replaces server error messages with a generic one plus `digest` (to match
 * server logs), so nothing sensitive reaches the browser.
 */
export default function RouteError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return (
    <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-16 sm:px-6">
      <h1 className="mb-4 text-2xl font-semibold">Something broke on this page.</h1>
      <ErrorState message="An unexpected error stopped this page from rendering. Trying again usually helps." onRetry={retry} />
      {error.digest && <p className="mt-3 font-mono text-xs text-zinc-600 dark:text-zinc-400">Reference: {error.digest}</p>}
    </main>
  );
}
