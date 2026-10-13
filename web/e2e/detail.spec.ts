import { expect, test } from "@playwright/test";

import { openSample, visible } from "./helpers";

const loaded = (page: import("@playwright/test").Page) =>
  expect(page.getByRole("heading", { level: 2, name: /How it gets into your project/ })).toBeVisible({ timeout: 30_000 });

test("direct dependency: bump with a copyable command; back keeps filters", async ({ page, context, browserName }) => {
  await openSample(page, /Legacy Express API/);
  await page.getByRole("button", { name: /^Critical/ }).click();
  await page.waitForURL(/severity=CRITICAL/);
  await visible(page, "li a").filter({ hasText: "Prototype Pollution in minimist" }).click();
  await loaded(page);

  await expect(visible(page, "#fix-heading")).toContainText("bump the direct dependency");
  await expect(visible(page, "section[aria-labelledby=fix-heading] pre code").first()).toHaveText("npm install minimist@^1.2.6");
  if (browserName === "chromium") {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.getByRole("button", { name: "Copy" }).first().click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("npm install minimist@^1.2.6");
  }

  await page.getByRole("link", { name: "← All findings" }).click();
  await page.waitForURL(/severity=CRITICAL/);
  await expect(page.getByRole("button", { name: /^Critical/ })).toHaveAttribute("aria-pressed", "true");
});

test("transitive dependency blocked by a parent: upgrade table + overrides", async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  await visible(page, "li a").filter({ hasText: "serialize-javascript" }).first().click();
  await loaded(page);
  await expect(visible(page, "#fix-heading")).toContainText("upgrade a parent");
  await expect(visible(page, "table")).toContainText("mocha@8.2.1");
  await expect(visible(page, "section[aria-labelledby=fix-heading] pre code").last()).toContainText('"overrides"');
  await expect(visible(page, "ol li").first()).toContainText(/legacy-api.*dev.*mocha.*serialize-javascript/);
});

test("references are safe external links", async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  await visible(page, "li a").filter({ hasText: "Command Injection in lodash" }).first().click();
  await loaded(page);
  const refs = visible(page, "section[aria-labelledby=refs-heading] a");
  expect(await refs.count()).toBeGreaterThan(0);
  await expect(refs.first()).toHaveAttribute("rel", "noopener noreferrer nofollow ugc");
  await expect(refs.first()).toHaveAttribute("target", "_blank");
});

test("invalid and unknown finding ids", async ({ page }) => {
  const scan = await openSample(page, /Legacy Express API/);
  await page.goto(`${scan}/findings/abc`);
  await expect(visible(page, "main [role=alert]")).toContainText("Not a valid finding");
  await page.goto(`${scan}/findings/999999999`);
  await expect(visible(page, "main [role=alert]")).toContainText("finding not found");
  await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0); // 404s aren't retryable
});
