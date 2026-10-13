import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AdvisoryMarkdown } from "./advisory-markdown";

const render = (md: string) => renderToStaticMarkup(<AdvisoryMarkdown>{md}</AdvisoryMarkdown>);

describe("AdvisoryMarkdown with hostile advisory text", () => {
  it("renders ordinary formatting", () => {
    const html = render("Versions **before** `4.17.21` are affected.\n\n- one\n- two");
    expect(html).toContain("<strong>before</strong>");
    expect(html).toContain("<code>4.17.21</code>");
    expect(html).toContain("<li>one</li>");
  });

  it("never emits raw HTML from the advisory", () => {
    const html = render('Hi <img src=x onerror="alert(1)"> <script>alert(2)</script> <iframe src="https://x"></iframe>');
    expect(html).not.toMatch(/<img|<script|<iframe|onerror/i);
  });

  it("drops markdown images (no tracking pixels) but keeps surrounding text", () => {
    const html = render("before ![pixel](https://tracker.example/p.gif) after");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("tracker.example");
    expect(html).toContain("before");
    expect(html).toContain("after");
  });

  it.each([
    "[click](javascript:alert(1))",
    "[click](JAVASCRIPT:alert(1))",
    "[click](data:text/html,<script>alert(1)</script>)",
    "[click](https://bank.example@evil.example/)",
    "[click](/scans/123)",
  ])("renders unsafe link %s as plain text", (md) => {
    const html = render(md);
    expect(html).not.toContain("<a");
    expect(html).toContain("click");
  });

  it("safe links open in a new tab without leaking the opener or referrer", () => {
    const html = render("[GHSA](https://github.com/advisories/GHSA-35jh-r3h4-6jhm)");
    expect(html).toContain('href="https://github.com/advisories/GHSA-35jh-r3h4-6jhm"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer nofollow ugc"');
  });

  it("demotes advisory headings below the page's own", () => {
    expect(render("# Impact")).not.toContain("<h1");
  });
});
