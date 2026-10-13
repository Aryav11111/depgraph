import type { FindingDetail } from "@/lib/api/client";

import { CodeBlock } from "./copy-button";

type Fix = FindingDetail["fix"];

const TITLES: Record<Fix["action"], string> = {
  update_lockfile: "Fix: update the lockfile",
  bump_direct: "Fix: bump the direct dependency",
  upgrade_parent_or_override: "Fix: upgrade a parent, or override",
  no_fix: "No fixed version exists yet",
  remove: "Remove this package",
};

const TONES: Record<Fix["action"], string> = {
  update_lockfile: "border-emerald-300 dark:border-emerald-900",
  bump_direct: "border-emerald-300 dark:border-emerald-900",
  upgrade_parent_or_override: "border-amber-300 dark:border-amber-900",
  no_fix: "border-zinc-300 dark:border-zinc-700",
  remove: "border-red-400 dark:border-red-900",
};

export function FixCard({ fix, pkg, installed }: { fix: Fix; pkg: string; installed: string }) {
  const blockers = fix.parents.filter((p) => p.allows_fix !== true);
  return (
    <section aria-labelledby="fix-heading" className={`flex flex-col gap-3 rounded-xl border-2 p-4 ${TONES[fix.action]}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="fix-heading" className="font-semibold">
          {TITLES[fix.action]}
        </h2>
        {fix.fixed_version && (
          <span className="font-mono text-sm">
            <span className="text-red-700 dark:text-red-400">{installed}</span>
            {" → "}
            <span className="text-emerald-700 dark:text-emerald-400">{fix.fixed_version}</span>
            {fix.breaking && (
              <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 font-sans text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                major version change
              </span>
            )}
          </span>
        )}
      </div>

      <p className="text-sm text-zinc-700 dark:text-zinc-300">{fix.message}</p>

      {fix.action === "update_lockfile" && <CodeBlock code={`npm update ${pkg}`} />}

      {fix.action === "bump_direct" && fix.fixed_version && (
        <CodeBlock code={`npm install ${pkg}@^${fix.fixed_version}`} />
      )}

      {fix.action === "upgrade_parent_or_override" && (
        <>
          {blockers.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[28rem] text-left text-sm">
                <caption className="sr-only">Packages whose declared range blocks the fix</caption>
                <thead className="text-xs uppercase text-zinc-500">
                  <tr>
                    <th className="py-1 pr-3 font-medium">Blocked by</th>
                    <th className="py-1 pr-3 font-medium">Declares</th>
                    <th className="py-1 font-medium">Upgrade to</th>
                  </tr>
                </thead>
                <tbody>
                  {blockers.map((p) => (
                    <tr key={`${p.name}@${p.version}:${p.dep_type}`} className="border-t border-zinc-200 dark:border-zinc-800">
                      <td className="py-1.5 pr-3 font-mono">
                        {p.is_root ? "package.json" : `${p.name}@${p.version ?? "?"}`}
                      </td>
                      <td className="py-1.5 pr-3 font-mono">{p.declared_range ?? "?"}</td>
                      <td className="py-1.5 font-mono">
                        {p.upgrade_to ?? (p.is_root ? `^${fix.fixed_version}` : <span className="text-zinc-500">no release yet</span>)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {Object.keys(fix.overrides).length > 0 && (
            <div className="flex flex-col gap-1">
              <p className="text-xs text-zinc-500">
                Or force it in package.json (npm 8.3+). Test it: the parent never declared support for this version.
              </p>
              <CodeBlock code={JSON.stringify({ overrides: fix.overrides }, null, 2)} />
            </div>
          )}
        </>
      )}

      {fix.fixed_version && (
        <p className="text-xs text-zinc-500">
          {fix.source === "registry"
            ? "Fixed version checked against releases published on npm."
            : "Fixed version taken from the advisory (npm registry unavailable); confirm it was published."}
        </p>
      )}
    </section>
  );
}
