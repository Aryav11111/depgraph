/**
 * Quick, friendly pre-checks on a file *before* uploading it.
 *
 * These exist for fast, specific error messages ("that's a yarn.lock") and to
 * avoid uploading obviously wrong files. They are not a security boundary: the
 * API treats every upload as untrusted and re-validates everything.
 */

// Mirrors the API's MAX_LOCKFILE_BYTES; the server's limit is the one that counts.
export const MAX_LOCKFILE_BYTES = Number(
  process.env.NEXT_PUBLIC_MAX_LOCKFILE_BYTES ?? 25 * 1024 * 1024,
);

export type PrecheckResult =
  | { ok: true; lockfileVersion: 2 | 3; packageCount: number; name: string | null }
  | { ok: false; error: string };

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Size check, done before reading the file at all. */
export function checkSize(size: number, max: number = MAX_LOCKFILE_BYTES): string | null {
  if (size === 0) return "That file is empty.";
  if (size > max) {
    return `That file is ${formatBytes(size)}; the limit is ${formatBytes(max)}.`;
  }
  return null;
}

export function precheckLockfile(text: string, filename = ""): PrecheckResult {
  const lower = filename.toLowerCase();
  if (lower.endsWith("yarn.lock") || lower.endsWith("pnpm-lock.yaml")) {
    return { ok: false, error: "Only npm's package-lock.json is supported for now." };
  }
  if (lower === "package.json") {
    return {
      ok: false,
      error:
        "That's package.json. Upload package-lock.json instead: it records the exact versions installed.",
    };
  }

  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { ok: false, error: "That file isn't valid JSON." };
  }
  if (typeof doc !== "object" || doc === null || Array.isArray(doc)) {
    return { ok: false, error: "That JSON isn't a package-lock.json." };
  }

  const record = doc as Record<string, unknown>;
  const version = record.lockfileVersion;
  if (version === 1) {
    return {
      ok: false,
      error:
        "This is a lockfileVersion 1 file (npm 6 or older). Regenerate it with npm 7+ (`npm install --package-lock-only`).",
    };
  }
  if (version !== 2 && version !== 3) {
    return { ok: false, error: "That JSON isn't a package-lock.json (no lockfileVersion 2 or 3)." };
  }
  const packages = record.packages;
  if (typeof packages !== "object" || packages === null || Array.isArray(packages)) {
    return { ok: false, error: 'This lockfile has no "packages" section.' };
  }

  return {
    ok: true,
    lockfileVersion: version,
    packageCount: Math.max(0, Object.keys(packages).length - 1), // minus the root entry
    name: typeof record.name === "string" ? record.name : null,
  };
}
