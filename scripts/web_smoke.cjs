#!/usr/bin/env node
/* Smoke test for the built GitHub Pages site: boot it in Chromium and walk the dashboard.
 *
 *   node scripts/web_smoke.cjs http://127.0.0.1:8000/Referral-Pilot/ [--out dir]
 *
 * Needs the `playwright` package. Set PW_CHANNEL=chrome to use an installed Google Chrome,
 * or PW_EXECUTABLE=/path/to/chrome. Exits non-zero when a check fails.
 */
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const url = process.argv[2];
const outIndex = process.argv.indexOf("--out");
const out = outIndex > 0 ? process.argv[outIndex + 1] : "web-smoke";
if (!url) {
  console.error("usage: web_smoke.cjs <site-url> [--out dir]");
  process.exit(2);
}
fs.mkdirSync(out, { recursive: true });

// Boards that refuse browser requests show up as CORS / network console errors: expected.
const EXPECTED_NOISE = /CORS|net::|ERR_|Failed to load resource|blocked the request/i;
const results = [];
let failed = false;
function check(name, ok, detail, advisory = false) {
  results.push(`${ok ? "PASS" : advisory ? "WARN" : "FAIL"} ${name}${detail ? ` - ${detail}` : ""}`);
  if (!ok && !advisory) failed = true;
}

(async () => {
  const launch = {};
  if (process.env.PW_CHANNEL) launch.channel = process.env.PW_CHANNEL;
  if (process.env.PW_EXECUTABLE) launch.executablePath = process.env.PW_EXECUTABLE;
  const browser = await chromium.launch(launch);
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const problems = [];
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !EXPECTED_NOISE.test(message.text())) problems.push(`console: ${message.text()}`);
  });
  page.on("dialog", (dialog) => dialog.accept());

  try {
    const started = Date.now();
    await page.goto(url);
    await page.waitForSelector("#board", { timeout: 240000 });
    check("site boots and renders the board", true, `${((Date.now() - started) / 1000).toFixed(1)}s`);
    await page.waitForSelector("#status :text('sent')", { timeout: 30000 });
    check("status bar loads", true);

    for (const [link, heading] of [["Outbox", "Outbox"], ["Companies", "Target companies"],
      ["Profile", "Candidate profile"], ["Activity", "Activity log"], ["Settings", "Settings"]]) {
      await page.click(`nav a:has-text("${link}")`);
      await page.waitForSelector(`h1:has-text("${heading}")`, { timeout: 30000 });
    }
    check("every page renders", true);

    await page.fill('#settings-form input[name="harvest_interval_hours"]', "8");
    await page.click('#settings-form button:has-text("Save settings")');
    await page.waitForSelector('#toasts :text("Settings saved")', { timeout: 30000 });
    check("settings save", true);

    await page.click('nav a:has-text("Board")');
    await page.waitForSelector("#board");
    const hasJobs = await page.waitForSelector(".job-card", { timeout: 120000 }).then(() => true, () => false);
    // Advisory: a crawl that found nothing (boards down) must not block publishing the app itself.
    check("job board has postings", hasJobs, hasJobs ? `${await page.locator(".job-card").count()} cards` : "none", true);
    if (hasJobs) {
      await page.locator(".job-card").first().click();
      await page.waitForSelector("#drawer-root");
      await page.click('#drawer-root button:has-text("Compile tailored resume")');
      await page.waitForSelector('#drawer-root a:has-text("View PDF")', { timeout: 120000 });
      check("tailored resume compiles in the browser", true);
    }
  } catch (error) {
    check("walkthrough", false, error.message.split("\n")[0]);
  }
  await page.screenshot({ path: path.join(out, "smoke.png") }).catch(() => {});
  check("no unexpected errors", problems.length === 0, problems.slice(0, 5).join(" | "));
  await browser.close();
  console.log(results.join("\n"));
  process.exit(failed ? 1 : 0);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
