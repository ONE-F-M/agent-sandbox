// Config for the browser smoke check (WI-004006). Lives outside every app checkout (house rule B7).
// Traces are never recorded; video is recorded by smoke.spec.js itself so it can close the context first.
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: __dirname,
  outputDir: '/home/frappe/pw-output/results',
  reporter: 'list',
  workers: 1,
  use: {
    baseURL: process.env.SMOKE_BASE_URL || 'http://127.0.0.1:8000',
    trace: 'off',
    launchOptions: { args: ['--no-sandbox', '--disable-dev-shm-usage'] },
  },
});
