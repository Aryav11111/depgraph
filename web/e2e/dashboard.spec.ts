import { expect, test } from "@playwright/test";

import { openSample, shownCount, visible } from "./helpers";

let scanUrl: string;

test.beforeEach(async ({ page }) => {
  scanUrl = await openSample(page, /Legacy Express API/);
});

test("findings are grouped most severe first", async ({ page }) => {
  const groups = await visible(page, "main h3").allTextContents();
  expect(groups.map((g) => g.replace(/\d+$/, ""))).toEqual(["Critical", "High", "Moderate", "Low"]);
});

test("a row shows installed → fix, reach and identifiers", async ({ page }) => {
  const row = visible(page, "li").filter({ hasText: "Command Injection in lodash" }).first();
  await expect(row).toContainText(/4\.17\.20\s*→\s*4\.17\.21/);
  await expect(row).toContainText("Direct");
  await expect(row).toContainText("CVE-2021-23337");
});

test("severity filter writes the URL and survives a reload", async ({ page }) => {
  await page.getByRole("button", { name: /^Critical/ }).click();
  await page.waitForURL(/severity=CRITICAL/);
  expect(await shownCount(page)).toBe(6);
  await page.reload();
  await expect(page.getByRole("button", { name: /^Critical/ })).toHaveAttribute("aria-pressed", "true");
  expect(await shownCount(page)).toBe(6);
});

test("search matches CVE aliases", async ({ page }) => {
  await page.getByLabel("Search findings").fill("CVE-2021-23337");
  await page.waitForURL(/q=CVE-2021-23337/);
  // Two GitHub advisories alias each other and this CVE (npm audit lists both too).
  expect(await shownCount(page)).toBe(2);
});

test("hide dev-only", async ({ page }) => {
  await page.getByLabel("Hide dev-only").check();
  await page.waitForURL(/hide_dev=1/);
  expect(await shownCount(page)).toBe(68);
});

test("empty filtered state offers a reset", async ({ page }) => {
  await page.goto(`${scanUrl}?q=definitely-not-a-package`);
  const empty = page.getByText("No findings match these filters.");
  await expect(empty).toBeVisible();
  await empty.getByRole("button", { name: "Reset filters" }).click();
  await expect.poll(() => shownCount(page)).toBe(89);
});

test("junk URL params are tolerated", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${scanUrl}?severity=<script>,critical&kind=x&reach=y&q=${"x".repeat(300)}`);
  await expect(page.getByText(/^Showing \d+ of \d+ findings$/)).toBeVisible();
  expect(errors).toEqual([]);
});
