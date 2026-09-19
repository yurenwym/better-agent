import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const e2eDataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "better-agent-e2e-"));
process.env.BETTER_AGENT_E2E_DATA_ROOT = e2eDataRoot;
const port = process.env.BETTER_AGENT_E2E_PORT ?? "61129";
const executablePath = process.env.BETTER_AGENT_E2E_BROWSER;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", screenshot: "only-on-failure", ...devices["Desktop Chrome"], ...(executablePath ? { launchOptions: { executablePath } } : {}) },
  webServer: {
    command: "python scripts/e2e_server.py",
    cwd: "..",
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      BETTER_AGENT_DATA_ROOT: e2eDataRoot,
      BETTER_AGENT_TEST_ALLOW_SQLITE: "1",
      BETTER_AGENT_GOAL_REVIEW_DELAY: "0",
      PORT: port,
      BETTER_AGENT_HOST: "127.0.0.1",
      LLM_AP_PATH: "",
      AGENT_MODEL_BASE_URL: "",
      AGENT_MODEL_ID: "",
      AGENT_MODEL_API_KEY: "",
      AGENT_FALLBACK_MODEL_BASE_URL: "",
      AGENT_FALLBACK_MODEL_ID: "",
      AGENT_FALLBACK_MODEL_API_KEY: "",
      EMBEDDING_API_KEY_ENV: "",
      MEMORY_EMBEDDING_API_KEY_ENV: "",
      EMBEDDING_API_KEY: "",
      MEMORY_EMBEDDING_API_KEY: "",
    },
  },
});
