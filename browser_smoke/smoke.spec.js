const { test, expect } = require('@playwright/test');
const fs = require('fs');

const VIDEO_DIR = '/home/frappe/pw-output/video';

test('login page loads and a WebM video is recorded', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, recordVideo: { dir: VIDEO_DIR } });
  const page = await context.newPage();

  const response = await page.goto('/login');
  expect(response.ok()).toBeTruthy();
  await expect(page.locator('#login_email')).toBeVisible();

  const videoPath = await page.video().path();
  await context.close(); // the .webm is only finalised once the context is closed

  expect(videoPath.endsWith('.webm')).toBeTruthy();
  expect(fs.statSync(videoPath).size).toBeGreaterThan(0);
});
