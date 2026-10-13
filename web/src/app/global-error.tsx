"use client"; // error boundaries must be Client Components

import "./globals.css";

/**
 * Last-resort boundary for errors in the root layout itself. It replaces the
 * whole document, so it renders its own <html>/<body> and imports the global
 * styles (Next doesn't apply them here).
 */
export default function GlobalError({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
        <title>depgraph: something went wrong</title>
        <h1 className="text-2xl font-semibold">depgraph hit an unexpected error.</h1>
        <button
          type="button"
          onClick={() => retry()}
          className="min-h-11 rounded-lg bg-indigo-700 px-4 text-sm font-medium text-white hover:bg-indigo-800"
        >
          Try again
        </button>
      </body>
    </html>
  );
}
