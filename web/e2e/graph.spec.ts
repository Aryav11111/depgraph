import { expect, test } from "@playwright/test";

import { openSample, visible } from "./helpers";

test.beforeEach(async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  await page.getByRole("link", { name: "Dependency graph" }).click();
  await page.waitForURL(/\/graph$/);
  await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 30_000 });
});

test("root sits at the centre of the layout", async ({ page }) => {
  const root = visible(page, ".react-flow__node").filter({ hasText: /^legacy-api$/ });
  await expect(root).toHaveCount(1);
  expect(await root.evaluate((el) => (el as HTMLElement).style.transform)).toMatch(/translate\(0px,\s*0px\)/);
  await expect(page.getByText(/85 of 172 packages shown/)).toBeVisible();
});

test("clicking a finding lights up its paths and dims the rest", async ({ page }) => {
  await page.locator("aside button", { hasText: "serialize-javascript@5.0.1" }).first().click();
  await expect.poll(() => visible(page, ".react-flow__node .opacity-15").count()).toBeGreaterThan(0);
  for (const name of ["legacy-api", "mocha", "serialize-javascript"]) {
    await expect(visible(page, ".react-flow__node").filter({ hasText: new RegExp(`^${name}$`) }).locator(".opacity-100")).toHaveCount(1);
  }
  await page.getByRole("button", { name: "Clear highlight" }).click();
  await expect(visible(page, ".react-flow__node .opacity-15")).toHaveCount(0);
});

test("depth slider; vulnerable paths stay visible at depth 1", async ({ page }) => {
  const slider = page.locator('input[type="range"]');
  await slider.fill(await slider.getAttribute("max") ?? "1");
  await expect(page.getByText(/172 of 172 packages shown/)).toBeVisible();
  await slider.fill("1");
  await expect(page.getByText(/30 of 172 packages shown/)).toBeVisible();
});

test("clicking a node shows its details and findings", async ({ page }) => {
  await visible(page, ".react-flow__node").filter({ hasText: /^lodash$/ }).click();
  const aside = page.locator("aside").first();
  await expect(aside).toContainText("lodash@4.17.20");
  await expect(aside.getByRole("link").first()).toHaveAttribute("href", /\/findings\/\d+$/);
});
