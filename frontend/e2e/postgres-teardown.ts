import { execFileSync } from "node:child_process";
import path from "node:path";

export default function globalTeardown(): void {
  if (!process.env.BETTER_AGENT_E2E_PG_ROOT) return;
  execFileSync("python", ["scripts/e2e_postgres.py", "down"], {
    cwd: path.resolve(process.cwd(), ".."),
    env: process.env,
    stdio: "inherit",
  });
}
