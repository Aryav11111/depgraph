import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col items-start justify-center gap-4 px-4 py-16 sm:px-6">
      <p className="font-mono text-sm text-zinc-600 dark:text-zinc-400">404</p>
      <h1 className="text-2xl font-semibold">There&apos;s nothing at this address.</h1>
      <p className="text-zinc-600 dark:text-zinc-400">
        Scan links stay valid as long as the scan exists; this one may have a typo.
      </p>
      <Link href="/" className="inline-flex min-h-11 items-center font-medium text-indigo-700 hover:underline dark:text-indigo-300">
        ← Scan a lockfile
      </Link>
    </main>
  );
}
