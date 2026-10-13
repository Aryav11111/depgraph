import { expect, type Page, test } from "@playwright/test";

import { openSample, visible } from "./helpers";

// Runs in the "mobile" project (Pixel 7: 412px wide, touch).

async function expectFitsViewport(page: Page) {
  const width = page.viewportSize()!.width;
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  // Clipped overflow doesn't show up in scrollWidth: also check element edges.
  const right = await page.evaluate(() =>
    Math.max(
      ...[...document.querySelectorAll("main *")]
        .filter((el) => (el as HTMLElement).offsetParent !== null && !el.closest(".react-flow__viewport, pre"))
        .map((el) => el.getBoundingClientRect().right),
    ),
  );
  expect(right).toBeLessThanOrEqual(width + 1);
}

test("home uses touch wording and fits", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText(/Choose your\s*package-lock\.json/)).toBeVisible();
  await expect(page.getByText(/Drop your/)).toBeHidden();
  await expectFitsViewport(page);
});

test("dashboard filters collapse behind a toggle", async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  const toggle = page.getByRole("button", { name: /^Filters/ });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: /^Critical/ })).toBeHidden();
  await toggle.click();
  await page.getByRole("button", { name: /^Critical/ }).click();
  await page.waitForURL(/severity=CRITICAL/);
  await expect(page.getByRole("button", { name: /^Filters \(1\)/ })).toBeVisible();
  // tap targets on touch screens are at least 44px tall
  const box = await page.getByRole("button", { name: /^Critical/ }).boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(44);
  await expectFitsViewport(page);
});

test("detail and graph fit the screen", async ({ page }) => {
  const scan = await openSample(page, /Legacy Express API/);
  await visible(page, "li a").first().click();
  await expect(page.getByRole("heading", { level: 2, name: /How it gets into your project/ })).toBeVisible({ timeout: 30_000 });
  await expectFitsViewport(page);
  await page.goto(`${scan}/graph`);
  await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 30_000 });
  await expectFitsViewport(page);
});
