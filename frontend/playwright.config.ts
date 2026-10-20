import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const e2eDataRoot = process.env.BETTER_AGENT_E2E_DATA_ROOT ?? fs.mkdtempSync(path.join(os.tmpdir(), "better-agent-e2e-"));
const scriptedDataRoot = process.env.BETTER_AGENT_E2E_SCRIPTED_DATA_ROOT ?? fs.mkdtempSync(path.join(os.tmpdir(), "better-agent-e2e-scripted-"));
const e2ePgRoot = process.env.BETTER_AGENT_E2E_PG_ROOT ?? fs.mkdtempSync(path.join(os.tmpdir(), "better-agent-e2e-pg-"));
process.env.BETTER_AGENT_E2E_PG_ROOT = e2ePgRoot;
process.env.BETTER_AGENT_E2E_DATA_ROOT = e2eDataRoot;
process.env.BETTER_AGENT_E2E_SCRIPTED_DATA_ROOT = scriptedDataRoot;
const port = process.env.BETTER_AGENT_E2E_PORT ?? "61129";
const scriptedPort = process.env.BETTER_AGENT_E2E_SCRIPTED_PORT ?? "61130";
process.env.BETTER_AGENT_E2E_PORT = port;
process.env.BETTER_AGENT_E2E_SCRIPTED_PORT = scriptedPort;
const executablePath = process.env.BETTER_AGENT_E2E_BROWSER;
const isolatedCredentials = {
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
  DATABASE_URL: "",
  BETTER_AGENT_E2E_POSTGRES: "1",
  BETTER_AGENT_E2E_PG_ROOT: e2ePgRoot,
  AGENT_MODEL_PROVIDER: "",
  OPENAI_API_KEY: "",
  OPENAI_BASE_URL: "",
  OPENAI_MODEL: "",
  ANTHROPIC_API_KEY: "",
  ANTHROPIC_BASE_URL: "",
  ANTHROPIC_MODEL: "",
  DEEPSEEK_API_KEY: "",
  DEEPSEEK_BASE_URL: "",
  DEEPSEEK_MODEL: "",
  BETTER_AGENT_LEARNING_V3: "OFF",
};

export default defineConfig({
  testDir: "./e2e",
  globalTeardown: "./e2e/postgres-teardown.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", screenshot: "only-on-failure", ...devices["Desktop Chrome"], ...(executablePath ? { launchOptions: { executablePath } } : {}) },
  webServer: [
    {
      command: "python scripts/e2e_server.py",
      cwd: "..",
      url: `http://127.0.0.1:${port}/api/health`,
      reuseExistingServer: false,
      timeout: 300_000,
      env: {
        ...isolatedCredentials,
        BETTER_AGENT_DATA_ROOT: e2eDataRoot,
        BETTER_AGENT_E2E_DATA_ROOT: e2eDataRoot,
        BETTER_AGENT_E2E_ROLE: "plain",
        BETTER_AGENT_GOAL_REVIEW_DELAY: "0",
        PORT: port,
        BETTER_AGENT_HOST: "127.0.0.1",
      },
    },
    {
      command: "python scripts/e2e_server.py",
      cwd: "..",
      url: `http://127.0.0.1:${scriptedPort}/api/health`,
      reuseExistingServer: false,
      timeout: 300_000,
      env: {
        ...isolatedCredentials,
        BETTER_AGENT_DATA_ROOT: scriptedDataRoot,
        BETTER_AGENT_E2E_DATA_ROOT: scriptedDataRoot,
        BETTER_AGENT_E2E_SCRIPTED: "1",
        BETTER_AGENT_E2E_ROLE: "scripted",
        BETTER_AGENT_GOAL_REVIEW_DELAY: "0",
        PORT: scriptedPort,
        BETTER_AGENT_HOST: "127.0.0.1",
        AGENT_MODEL_BASE_URL: "http://127.0.0.1:9/v1",
        AGENT_MODEL_ID: "e2e-scripted",
        AGENT_MODEL_API_KEY: "e2e-local-fixed",
        AGENT_MODEL_CAPABILITIES: "streaming,tool_calling,json_object",
        AGENT_MODEL_CONTEXT_WINDOW: "131072",
        AGENT_MODEL_MAX_OUTPUT_TOKENS: "4096",
        AGENT_MODEL_TIMEOUT_SECONDS: "120",
        AGENT_MODEL_MAX_ATTEMPTS: "1",
      },
    },
  ],
});
