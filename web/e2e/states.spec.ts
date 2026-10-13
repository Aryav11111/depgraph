import { expect, test } from "@playwright/test";

import { openSample, visible } from "./helpers";


test("API down → readable error with a working retry", async ({ page }) => {
  const scan = await openSample(page, /Legacy Express API/);
  let blocked = true;
  await page.route(`**/scans/*`, (route) => (blocked && route.request().url().includes(":8080") ? route.abort("connectionrefused") : route.continue()));
  await page.goto(scan);
  await expect(visible(page, "main [role=alert]")).toContainText("Couldn't reach the depgraph API");
  blocked = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByText("Showing 89 of 89 findings")).toBeVisible();
});

test("slow API → explains cold starts after a few seconds", async ({ page }) => {
  const scan = await openSample(page, /Legacy Express API/);
  await page.route(`**/scans/*`, async (route) => {
    if (route.request().url().includes(":8080")) await new Promise((r) => setTimeout(r, 4_500));
    await route.continue();
  });
  await page.goto(scan);
  await expect(page.getByText(/API scales to zero when idle/)).toBeVisible({ timeout: 6_000 });
  await expect(page.getByText("Showing 89 of 89 findings")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/API scales to zero when idle/)).toHaveCount(0);
});

test("server error on a scan → generic message, no internals", async ({ page }) => {
  const scan = await openSample(page, /Legacy Express API/);
  await page.route(`**/scans/*`, (route) =>
    route.request().url().includes(":8080")
      ? route.fulfill({ status: 500, json: { detail: "psycopg.OperationalError at line 3" } })
      : route.continue(),
  );
  await page.goto(scan);
  await expect(visible(page, "main [role=alert]")).toContainText("The server hit an error");
  await expect(page.getByText("psycopg")).toHaveCount(0);
});

test("unknown URL → custom 404 page", async ({ page }) => {
  const res = await page.goto("/definitely/not/a/page");
  expect(res?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "There's nothing at this address." })).toBeVisible();
});

test("unknown scan → not found, no retry offered on the scan's error", async ({ page }) => {
  await page.goto("/scans/00000000-0000-0000-0000-000000000000");
  await expect(visible(page, "main [role=alert]")).toContainText("scan not found");
});

