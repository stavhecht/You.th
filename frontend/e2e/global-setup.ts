/**
 * Reseed the test database before every browser run, so every run starts from
 * the same three published articles and two drafts. The seed script refuses
 * any database not named *_test.
 */
import { execFileSync } from "node:child_process";

export default function globalSetup(): void {
  const python = process.env.E2E_PYTHON ?? "../backend/.venv/bin/python";
  execFileSync(python, ["-m", "tests.e2e.browser_seed"], {
    cwd: "../backend",
    stdio: ["ignore", "inherit", "inherit"],
  });
}
