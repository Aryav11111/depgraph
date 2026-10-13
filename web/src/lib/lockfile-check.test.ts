import { describe, expect, it } from "vitest";

import { checkSize, formatBytes, precheckLockfile } from "./lockfile-check";

const lock = (extra: Record<string, unknown> = {}) =>
  JSON.stringify({
    name: "app",
    lockfileVersion: 3,
    packages: { "": { name: "app" }, "node_modules/a": { version: "1.0.0" } },
    ...extra,
  });

describe("precheckLockfile", () => {
  it("accepts v2 and v3 lockfiles and counts packages without the root", () => {
    expect(precheckLockfile(lock())).toEqual({
      ok: true,
      lockfileVersion: 3,
      packageCount: 1,
      name: "app",
    });
    expect(precheckLockfile(lock({ lockfileVersion: 2 }))).toMatchObject({
      ok: true,
      lockfileVersion: 2,
    });
  });

  it.each([
    ["not json", "{nope", "isn't valid JSON"],
    ["array", "[]", "isn't a package-lock.json"],
    ["null", "null", "isn't a package-lock.json"],
    ["no version", JSON.stringify({ packages: {} }), "no lockfileVersion 2 or 3"],
    ["string version", lock({ lockfileVersion: "3" }), "no lockfileVersion 2 or 3"],
    ["v1", lock({ lockfileVersion: 1 }), "npm 6 or older"],
    ["no packages", JSON.stringify({ lockfileVersion: 3 }), 'no "packages"'],
    ["packages array", lock({ packages: [] }), 'no "packages"'],
  ])("rejects %s with a specific message", (_, text, message) => {
    const result = precheckLockfile(text);
    expect(result.ok).toBe(false);
    expect(!result.ok && result.error).toContain(message);
  });

  it.each([
    ["yarn.lock", "Only npm's package-lock.json"],
    ["frontend/pnpm-lock.yaml", "Only npm's package-lock.json"],
    ["package.json", "Upload package-lock.json instead"],
  ])("recognises %s by name before parsing", (filename, message) => {
    const result = precheckLockfile("{}", filename);
    expect(!result.ok && result.error).toContain(message);
  });

  it("never echoes file contents back (untrusted input)", () => {
    const hostile = '{"lockfileVersion": "<img src=x onerror=alert(1)>"}';
    const result = precheckLockfile(hostile);
    expect(!result.ok && result.error).not.toContain("<img");
  });
});

describe("checkSize", () => {
  it("rejects empty and oversized files, accepts the rest", () => {
    expect(checkSize(0, 100)).toContain("empty");
    expect(checkSize(101, 100)).toContain("limit is 100 B");
    expect(checkSize(100, 100)).toBeNull();
  });
});

describe("formatBytes", () => {
  it.each([
    [512, "512 B"],
    [2048, "2 KB"],
    [5 * 1024 * 1024, "5.0 MB"],
  ])("%i -> %s", (n, s) => expect(formatBytes(n)).toBe(s));
});
