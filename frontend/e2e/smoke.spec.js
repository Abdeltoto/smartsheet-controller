import { test, expect } from '@playwright/test';

test('landing page loads connect form', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/Smartsheet Controller/i);
  await expect(page.locator('#ss-token')).toBeVisible();
  await expect(page.locator('#validate-btn')).toBeVisible();
});

test('health endpoint is reachable', async ({ request }) => {
  const res = await request.get('/health');
  expect(res.ok()).toBeTruthy();
  const body = await res.json();
  expect(body.status).toBe('ok');
});
