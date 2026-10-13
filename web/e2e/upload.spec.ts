import path from "node:path";

import { expect, test } from "@playwright/test";

import { openSample, visible } from "./helpers";

const REPO = path.resolve(__dirname, "../..");
const alert = (page: import("@playwright/test").Page) => visible(page, "main [role=alert]");

test("home shows the API ready and three samples", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("API ready")).toBeVisible();
  await expect(page.getByRole("button", { name: /Legacy Express API|React \+ Vite|This website/ })).toHaveCount(3);
});

test("package.json is rejected with advice before uploading", async ({ page }) => {
  await page.goto("/");
  await page.setInputFiles('input[type="file"]', {
    name: "package.json", mimeType: "application/json", buffer: Buffer.from('{"name":"x"}'),
  });
  await expect(alert(page)).toContainText("Upload package-lock.json instead");
});

test("hostile JSON content is never rendered as HTML", async ({ page }) => {
  await page.goto("/");
  await page.setInputFiles('input[type="file"]', {
    name: "package-lock.json", mimeType: "application/json",
    buffer: Buffer.from('{"lockfileVersion":"<img src=x onerror=alert(1)>"}'),
  });
  await expect(alert(page)).toBeVisible();
  await expect(page.locator("img[src=x]")).toHaveCount(0);
});

test("server-side validation errors are shown readably", async ({ page }) => {
  await page.goto("/");
  await page.setInputFiles('input[type="file"]', {
    name: "package-lock.json", mimeType: "application/json",
    buffer: Buffer.from('{"lockfileVersion":3,"packages":{"node_modules/a":{}}}'),
  });
  await expect(alert(page)).toContainText("couldn't read that lockfile");
});

test("a sample opens its scan with the npm-audit-verified finding count", async ({ page }) => {
  await openSample(page, /Legacy Express API/);
  await expect(visible(page, "h1")).toHaveText("legacy-api");
  await expect(page.getByText("Showing 89 of 89 findings")).toBeVisible();
});

test("uploading a real lockfile file", async ({ page }) => {
  await page.goto("/");
  await page.setInputFiles('input[type="file"]', path.join(REPO, "tests/fixtures/npm/realworld/package-lock.v3.json"));
  await page.waitForURL(/\/scans\//);
  await expect(visible(page, "h1")).toHaveText("acme-dashboard");
  await expect(page.getByText("Showing 11 of 11 findings")).toBeVisible({ timeout: 30_000 });
});

test("an upload can be cancelled", async ({ page }) => {
  await page.route("**/scans", async (route) => {
    if (route.request().method() === "POST") await new Promise((r) => setTimeout(r, 5_000));
    await route.continue().catch(() => {});
  });
  await page.goto("/");
  await page.getByRole("button", { name: /Legacy Express API/ }).click();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Cancel" })).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
});
