import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** Next keeps recently visited routes mounted but hidden (React Activity), so
 *  locators that could match a hidden route are scoped to visible elements. */
export const visible = (page: Page, selector: string) => page.locator(`${selector}:visible`);

export const shownCount = async (page: Page) => {
  const text = await page.getByText(/^Showing \d+ of \d+ findings$/).textContent();
  return Number(/Showing (\d+)/.exec(text ?? "")![1]);
};

export async function openSample(page: Page, name: RegExp): Promise<string> {
  await page.goto("/");
  await page.getByRole("button", { name }).click();
  await page.waitForURL(/\/scans\/[0-9a-f-]{36}$/);
  await expect(page.getByText(/^Showing \d+ of \d+ findings$/)).toBeVisible({ timeout: 30_000 });
  return page.url();
}

/** Fails on axe violations of impact "serious" or "critical". */
export async function expectAccessible(page: Page) {
  const { violations } = await new AxeBuilder({ page })
    .exclude(".react-flow__renderer") // canvas internals; the graph's controls are still checked
    .analyze();
  const serious = violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(
    serious.map((v) => `${v.id} (${v.impact}): ${v.help} → ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(", ")}`),
  ).toEqual([]);
}
