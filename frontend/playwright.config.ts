import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const e2eDataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "better-agent-e2e-"));
process.env.BETTER_AGENT_E2E_DATA_ROOT = e2eDataRoot;
const port = process.env.BETTER_AGENT_E2E_PORT ?? "61129";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", screenshot: "only-on-failure", ...devices["Desktop Chrome"] },
  webServer: {
    command: "python scripts/e2e_server.py",
    cwd: "..",
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { BETTER_AGENT_DATA_ROOT: e2eDataRoot, BETTER_AGENT_GOAL_REVIEW_DELAY: "0", PORT: port, BETTER_AGENT_HOST: "127.0.0.1" },
  },
});
