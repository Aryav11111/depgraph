import { expect, test } from "@playwright/test";

import { expectAccessible, openSample, visible } from "./helpers";

// axe-core on every page type; fails on "serious" or "critical" violations.

test("home", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("API ready")).toBeVisible();
  await expectAccessible(page);
});

test("findings dashboard", async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  await expectAccessible(page);
});

test("finding detail", async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  await visible(page, "li a").filter({ hasText: "serialize-javascript" }).first().click();
  await expect(page.getByRole("heading", { level: 2, name: /How it gets into your project/ })).toBeVisible({ timeout: 30_000 });
  await expectAccessible(page);
});

test("dependency graph", async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  await page.getByRole("link", { name: "Dependency graph" }).click();
  await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 30_000 });
  await expectAccessible(page);
});

test("404", async ({ page }) => {
  await page.goto("/nope");
  await expectAccessible(page);
});

test.describe("dark mode", () => {
  test.use({ colorScheme: "dark" });
  test("dashboard and detail", async ({ page }) => {
    await openSample(page, /Legacy Express API/);
    await expectAccessible(page);
    await visible(page, "li a").first().click();
    await expect(page.getByRole("heading", { level: 2, name: /How it gets into your project/ })).toBeVisible({ timeout: 30_000 });
    await expectAccessible(page);
  });
});
