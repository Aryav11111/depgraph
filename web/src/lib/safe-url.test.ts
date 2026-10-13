import { describe, expect, it } from "vitest";

import { safeHttpUrl } from "./safe-url";

describe("safeHttpUrl", () => {
  it.each([
    ["https://github.com/lodash/lodash/commit/3469357", "https://github.com/lodash/lodash/commit/3469357"],
    ["http://example.com/a?b=c#d", "http://example.com/a?b=c#d"],
    ["  https://nvd.nist.gov/vuln/detail/CVE-2021-23337  ", "https://nvd.nist.gov/vuln/detail/CVE-2021-23337"],
  ])("allows %s", (raw, expected) => {
    expect(safeHttpUrl(raw)).toBe(expected);
  });

  it.each([
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(document.cookie)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "//evil.example/x", // protocol-relative
    "/scans/123", // relative: must not resolve against our origin
    "https://bank.example@evil.example/login", // credential-style spoof
    "",
    null,
    undefined,
  ])("rejects %s", (raw) => {
    expect(safeHttpUrl(raw)).toBeNull();
  });
});
