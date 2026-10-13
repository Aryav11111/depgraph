import { describe, expect, it } from "vitest";

import { detailOf, messageForStatus } from "./errors";

describe("detailOf", () => {
  it("reads string and validation-list details", () => {
    expect(detailOf({ detail: "scan not found" })).toBe("scan not found");
    expect(detailOf({ detail: [{ msg: "a" }, { msg: "b" }] })).toBe("a; b");
    expect(detailOf({ detail: [] })).toBeNull();
    expect(detailOf("oops")).toBeNull();
    expect(detailOf(null)).toBeNull();
  });
});

describe("messageForStatus", () => {
  it.each([
    [413, {}, "larger than the server accepts"],
    [422, { detail: "unsupported lockfileVersion 1" }, "unsupported lockfileVersion 1"],
    [429, {}, "Too many uploads"],
    [429, { detail: "Too many uploads from your address. Try again in 7 minutes." }, "7 minutes"],
    [404, { detail: "scan not found" }, "scan not found"],
    [409, { detail: "scan is failed; its graph is not available" }, "scan is failed"],
    [500, { detail: "Traceback (most recent call last)" }, "server hit an error"],
    [418, {}, "HTTP 418"],
  ])("%i -> readable message", (status, body, expected) => {
    expect(messageForStatus(status, body)).toContain(expected);
  });

  it("does not surface raw server internals on 5xx", () => {
    expect(messageForStatus(503, { detail: "psycopg error at line 3" })).not.toContain("psycopg");
  });
});
