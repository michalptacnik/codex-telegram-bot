import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  try {
    const extensionDevelopmentPath = path.resolve(__dirname, "../../");
    const extensionTestsPath = path.resolve(__dirname, "./suite/index");

    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      launchArgs: [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-gpu",
        "--skip-welcome",
        "--skip-release-notes",
      ],
    });
  } catch (error) {
    console.error("Failed to run VS Code integration tests.");
    console.error(error);
    process.exit(1);
  }
}

void main();
