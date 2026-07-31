import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const npmPackageRoot = process.env.NPM_PACKAGE_ROOT;
if (!npmPackageRoot) {
  throw new Error("NPM_PACKAGE_ROOT is required");
}

const mcpPackagePath = require.resolve("@playwright/mcp/package.json", {
  paths: [npmPackageRoot],
});
const mcpRequire = createRequire(mcpPackagePath);
const { chromium } = mcpRequire("playwright");

const browser = await chromium.launch({
  args: ["--no-sandbox"],
  headless: true,
});
await browser.close();
