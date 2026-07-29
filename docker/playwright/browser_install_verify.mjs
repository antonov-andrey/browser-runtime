import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const npmGlobalRoot = process.env.NPM_GLOBAL_ROOT;
if (!npmGlobalRoot) {
  throw new Error("NPM_GLOBAL_ROOT is required");
}

const mcpPackagePath = require.resolve("@playwright/mcp/package.json", {
  paths: [npmGlobalRoot],
});
const mcpRequire = createRequire(mcpPackagePath);
const { chromium } = mcpRequire("playwright");

const browser = await chromium.launch({
  args: ["--no-sandbox"],
  headless: true,
});
await browser.close();
