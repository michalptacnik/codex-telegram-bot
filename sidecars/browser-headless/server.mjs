import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { chromium } from "playwright";

const DEFAULT_TIMEOUT_MS = 15000;
const DEFAULT_CHROME_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36";
const STATE_ROOT = process.env.ZEROCLAW_HEADLESS_STATE_DIR
  ? path.resolve(process.env.ZEROCLAW_HEADLESS_STATE_DIR)
  : path.resolve(process.cwd(), ".state");

const sessions = new Map();
const execFileAsync = promisify(execFile);

let stdoutBroken = false;

function writeJsonLine(payload) {
  if (stdoutBroken || process.stdout.destroyed || !payload) {
    return false;
  }
  try {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
    return true;
  } catch (error) {
    if (error && error.code === 'EPIPE') {
      stdoutBroken = true;
      process.exitCode = 0;
      return false;
    }
    throw error;
  }
}

function nowIso() {
  return new Date().toISOString();
}

function ok(id, output, data = {}) {
  return { id, ok: true, output, data };
}

function fail(id, error, data = {}) {
  return { id, ok: false, error, data };
}

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

function pythonBinForVenv(venvDir) {
  return path.join(venvDir, "bin", "python");
}

async function ensureBrowserCookieRuntime() {
  const venvDir = path.join(STATE_ROOT, "python-browser-cookie3");
  const pythonBin = pythonBinForVenv(venvDir);
  try {
    await fs.access(pythonBin);
  } catch {
    await execFileAsync("python3", ["-m", "venv", venvDir], { cwd: STATE_ROOT });
    await execFileAsync(pythonBin, ["-m", "pip", "install", "browser-cookie3"], { cwd: STATE_ROOT });
  }
  return pythonBin;
}

async function loadChromeCookiesForDomain(domain) {
  const pythonBin = await ensureBrowserCookieRuntime();
  const helperPath = path.join(process.cwd(), "sidecars", "browser-headless", "export_chrome_cookies.py");
  const { stdout } = await execFileAsync(pythonBin, [helperPath, domain], {
    cwd: process.cwd(),
    maxBuffer: 10 * 1024 * 1024
  });
  return JSON.parse(stdout);
}

async function sessionPaths(sessionId) {
  const root = path.join(STATE_ROOT, sessionId);
  const userDataDir = path.join(root, "profile");
  const artifactsDir = path.join(root, "artifacts");
  await ensureDir(userDataDir);
  await ensureDir(artifactsDir);
  return { root, userDataDir, artifactsDir };
}

async function clearChromiumSingletonLocks(userDataDir) {
  const lockNames = [
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "RunningChromeVersion"
  ];
  await Promise.all(
    lockNames.map((name) =>
      fs.rm(path.join(userDataDir, name), { force: true, recursive: true }).catch(() => {})
    )
  );
}

async function launchPersistentContextWithRetry(userDataDir, options) {
  try {
    return await chromium.launchPersistentContext(userDataDir, options);
  } catch (error) {
    const message = error?.message || String(error);
    if (!message.includes("ProcessSingleton")) {
      throw error;
    }
    await clearChromiumSingletonLocks(userDataDir);
    return chromium.launchPersistentContext(userDataDir, options);
  }
}

async function ensureSession(sessionId, options = {}) {
  const existing = sessions.get(sessionId);
  const requestedHeadless = options.headless !== false;
  if (existing) {
    if (existing.headless === requestedHeadless) {
      return existing;
    }
    try {
      if (existing.traceActive) {
        await existing.context.tracing.stop({
          path: path.join(existing.paths.artifactsDir, `trace-switch-${Date.now()}.zip`)
        });
      }
    } catch {}
    try {
      await existing.context.close();
    } catch {}
    sessions.delete(sessionId);
  }

  const paths = await sessionPaths(sessionId);
  const headless = requestedHeadless;
  const context = await withTimeout(
    launchPersistentContextWithRetry(paths.userDataDir, {
      channel: 'chrome',
      headless,
      ignoreDefaultArgs: ['--enable-automation'],
      args: ['--disable-blink-features=AutomationControlled'],
      viewport: { width: 1440, height: 960 },
      userAgent: DEFAULT_CHROME_UA
    }),
    options.timeoutMs || DEFAULT_TIMEOUT_MS,
    "launch_persistent_context"
  );
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });
  context.setDefaultTimeout(options.timeoutMs || DEFAULT_TIMEOUT_MS);
  await context.tracing.start({ screenshots: true, snapshots: true });

  const page = context.pages()[0] || (await context.newPage());
  const session = {
    id: sessionId,
    context,
    page,
    headless,
    paths,
    traceActive: true,
    createdAt: nowIso()
  };
  sessions.set(sessionId, session);
  return session;
}

async function getPage(request) {
  const sessionId = request.session || "default";
  const session = await ensureSession(sessionId, {
    headless: request.headless,
    timeoutMs: request.timeout_ms
  });
  if (session.page.isClosed()) {
    session.page = session.context.pages()[0] || (await session.context.newPage());
  }
  return session;
}

async function saveTrace(session, requestedPath) {
  const tracePath =
    requestedPath ||
    path.join(session.paths.artifactsDir, `trace-${Date.now()}.zip`);
  if (!session.traceActive) {
    await session.context.tracing.start({ screenshots: true, snapshots: true });
  }
  await session.context.tracing.stop({ path: tracePath });
  session.traceActive = false;
  await session.context.tracing.start({ screenshots: true, snapshots: true });
  session.traceActive = true;
  return tracePath;
}

async function saveScreenshot(session, requestedPath, fullPage = true) {
  const screenshotPath =
    requestedPath ||
    path.join(session.paths.artifactsDir, `screenshot-${Date.now()}.png`);
  await session.page.screenshot({ path: screenshotPath, fullPage });
  return screenshotPath;
}

async function buildSnapshot(page) {
  const payload = await page.evaluate(() => {
    const text = document.body?.innerText || "";
    const visible = Array.from(
      document.querySelectorAll("a, button, input, textarea, select, [role='button']")
    )
      .slice(0, 40)
      .map((el, index) => {
        const rect = el.getBoundingClientRect();
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute("role") || "";
        const name =
          el.getAttribute("aria-label") ||
          el.textContent?.trim() ||
          el.getAttribute("placeholder") ||
          el.getAttribute("name") ||
          tag;
        return {
          ref: `e${index + 1}`,
          tag,
          role,
          name: name.slice(0, 120),
          selector:
            el.id
              ? `#${el.id}`
              : el.getAttribute("data-testid")
                ? `[data-testid="${el.getAttribute("data-testid")}"]`
                : tag,
          visible: rect.width > 0 && rect.height > 0
        };
      });
    return {
      title: document.title,
      url: window.location.href,
      text: text.slice(0, 5000),
      visible
    };
  });

  const lines = [
    `URL: ${payload.url}`,
    `Title: ${payload.title}`,
    "",
    "Visible elements:"
  ];
  for (const entry of payload.visible) {
    if (!entry.visible) {
      continue;
    }
    lines.push(
      `${entry.ref} ${entry.tag}${entry.role ? ` role=${entry.role}` : ""} :: ${entry.name} :: ${entry.selector}`
    );
  }
  lines.push("", "Text:", payload.text || "(empty)");
  return { output: lines.join("\n"), data: payload };
}

async function typeInto(page, selector, text, replace) {
  const locator = await activeLocator(page, selector);
  await locator.waitFor({ state: "visible" });
  try {
    await locator.click({ timeout: 1000 });
  } catch {
    await locator.evaluate((el) => {
      if (typeof el.scrollIntoView === "function") {
        el.scrollIntoView({ block: "center", inline: "nearest" });
      }
      if (typeof el.focus === "function") {
        el.focus();
      }
    });
  }
  if (replace) {
    try {
      await locator.fill("");
    } catch {
      await locator.evaluate((el) => {
        if (el && typeof el.focus === "function") {
          el.focus();
        }
        if (el && "value" in el) {
          el.value = "";
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return;
        }
        if (el && el.isContentEditable) {
          el.textContent = "";
          el.dispatchEvent(new InputEvent("input", {
            bubbles: true,
            data: "",
            inputType: "deleteContentBackward"
          }));
        }
      });
      await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
      await page.keyboard.press("Backspace");
    }
  }
  try {
    await locator.fill(text);
  } catch {
    await locator.evaluate((el) => {
      if (el && typeof el.focus === "function") {
        el.focus();
      }
    });
    await page.keyboard.type(text, { delay: 40 });
  }
}

async function activeLocator(page, selector) {
  const locator = page.locator(selector);
  const count = await locator.count();
  if (count <= 1) {
    return locator.first();
  }

  let lastVisible = null;
  for (let index = 0; index < count; index += 1) {
    const candidate = locator.nth(index);
    try {
      if (await candidate.isVisible()) {
        lastVisible = candidate;
      }
    } catch {}
  }

  return lastVisible || locator.first();
}

async function dismissXCookieBanner(page) {
  const buttons = ['Accept all cookies', 'Accept', 'Refuse non-essential cookies'];
  for (const label of buttons) {
    const locator = page.getByRole('button', { name: label }).first();
    if (await locator.count()) {
      try {
        await locator.click({ timeout: 1000 });
      } catch {}
    }
  }
}

async function clickFirstVisible(page, selectors, timeoutMs) {
  for (const selector of selectors) {
    const locator = await activeLocator(page, selector);
    if (!(await locator.count())) {
      continue;
    }
    try {
      await locator.click({ timeout: timeoutMs });
      return true;
    } catch {}
  }
  return false;
}

async function fillFirstVisible(page, selectors, value, timeoutMs) {
  for (const selector of selectors) {
    const locator = await activeLocator(page, selector);
    if (!(await locator.count())) {
      continue;
    }
    try {
      await locator.waitFor({ state: 'visible', timeout: timeoutMs });
      await locator.fill(value);
      return true;
    } catch {}
  }
  return false;
}

async function typeFirstVisible(page, selectors, value, timeoutMs) {
  for (const selector of selectors) {
    const locator = await activeLocator(page, selector);
    if (!(await locator.count())) {
      continue;
    }
    try {
      await locator.waitFor({ state: 'visible', timeout: timeoutMs });
      await locator.click({ timeout: timeoutMs });
      await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A');
      await page.keyboard.press('Backspace');
      await page.keyboard.type(value, { delay: 60 });
      return true;
    } catch {}
  }
  return false;
}

async function xAuthState(page, timeoutMs = DEFAULT_TIMEOUT_MS) {
  await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: timeoutMs });
  await page.waitForTimeout(1200);
  await dismissXCookieBanner(page);
  const payload = await page.evaluate(() => {
    const body = document.body?.innerText || '';
    const href = window.location.href;
    const title = document.title;
    const loweredBody = body.toLowerCase();
    const authenticatedSelectors = [
      '[data-testid="SideNav_NewTweet_Button"]',
      '[data-testid="AppTabBar_Home_Link"]',
      '[data-testid="tweetTextarea_0"]',
      '[href="/compose/post"]'
    ];
    const authenticated = authenticatedSelectors.some((selector) => document.querySelector(selector))
      && !href.includes('/i/flow/login');
    let status = 'unauthenticated';
    if (authenticated) {
      status = 'authenticated';
    } else if (loweredBody.includes('suspicious login prevented')) {
      status = 'suspicious_login_prevented';
    } else if (href.includes('/i/flow/login')) {
      status = 'login_required';
    }
    return {
      authenticated,
      status,
      url: href,
      title,
      bodySnippet: body.slice(0, 1000)
    };
  });
  return payload;
}

function sanitizeUsername(value) {
  return String(value || '').trim().replace(/^@+/, '');
}

async function bootstrapXSession(session, request, timeoutMs) {
  if (!request.username || !request.password || !request.email) {
    throw new Error('bootstrap_x_session requires username, password, and email');
  }

  const stepTimeout = Math.min(Math.max(Math.floor(timeoutMs / 8), 1500), 8000);
  const page = session.page;
  await page.goto('https://x.com/i/flow/login', { waitUntil: 'domcontentloaded', timeout: stepTimeout });
  await page.waitForTimeout(2000);
  await Promise.race([
    page.locator('input[autocomplete="username"], input[name="text"]').first().waitFor({
      state: 'visible',
      timeout: stepTimeout
    }),
    page.getByText('Phone, email, or username').first().waitFor({
      state: 'visible',
      timeout: stepTimeout
    })
  ]).catch(() => {});
  await dismissXCookieBanner(page);

  const usernameFilled = await typeFirstVisible(
    page,
    ['input[autocomplete="username"]', 'input[name="text"]'],
    request.email,
    stepTimeout
  );
  if (!usernameFilled) {
    const debug = await page.evaluate(() => ({
      url: window.location.href,
      title: document.title,
      bodySnippet: (document.body?.innerText || '').slice(0, 500),
      inputCount: document.querySelectorAll('input[autocomplete="username"], input[name="text"]').length
    }));
    throw new Error(
      `Unable to find X username/email input during bootstrap. URL=${debug.url} title=${debug.title} inputCount=${debug.inputCount} body=${JSON.stringify(debug.bodySnippet)}`
    );
  }
  await clickFirstVisible(
    page,
    ['div[role="button"][data-testid="LoginForm_Login_Button"]', 'button:has-text("Next")', 'div[role="button"]:has-text("Next")'],
    stepTimeout
  );
  await page.waitForTimeout(1500);
  await Promise.race([
    page.locator('input[name="password"]').first().waitFor({
      state: 'visible',
      timeout: stepTimeout
    }),
    page.locator('input[data-testid="ocfEnterTextTextInput"], input[name="text"]').first().waitFor({
      state: 'visible',
      timeout: stepTimeout
    }),
    page.getByText('Enter your phone number or username').first().waitFor({
      state: 'visible',
      timeout: stepTimeout
    }),
    page.getByText('Enter your email').first().waitFor({
      state: 'visible',
      timeout: stepTimeout
    })
  ]).catch(() => {});

  const passwordVisible = await page.locator('input[name="password"]').count();
  if (!passwordVisible) {
    const challengeFilled = await typeFirstVisible(
    page,
    ['input[data-testid="ocfEnterTextTextInput"]', 'input[name="text"]'],
      sanitizeUsername(request.username),
      stepTimeout
    );
    if (challengeFilled) {
      await clickFirstVisible(
        page,
        ['button:has-text("Next")', 'div[role="button"]:has-text("Next")'],
        stepTimeout
      );
      await page.waitForTimeout(1500);
      await page.locator('input[name="password"]').first().waitFor({
        state: 'visible',
        timeout: stepTimeout
      }).catch(() => {});
    }
  }

  const passwordFilled = await typeFirstVisible(
    page,
    ['input[name="password"]'],
    request.password,
    stepTimeout
  );
  if (!passwordFilled) {
    throw new Error('Unable to find X password input during bootstrap');
  }

  const clickedLogin = await clickFirstVisible(
    page,
    ['button:has-text("Log in")', 'div[role="button"]:has-text("Log in")', '[data-testid="LoginForm_Login_Button"]'],
    stepTimeout
  );
  if (!clickedLogin) {
    throw new Error('Unable to submit X login form during bootstrap');
  }

  await page.waitForTimeout(4000);
  const auth = await xAuthState(page, stepTimeout);
  if (!auth.authenticated) {
    // Do NOT fall back to importXSessionFromChrome here. bootstrapXSession is called with
    // explicit credentials (username, password, email are required above). Silently loading
    // system Chrome cookies when headless login fails would authenticate as the wrong account.
    if (auth.status === 'suspicious_login_prevented') {
      throw new Error(
        `X blocked true headless login as suspicious activity. Final URL: ${auth.url}. Body: ${JSON.stringify(auth.bodySnippet)}`
      );
    }
    throw new Error(
      `X bootstrap did not reach an authenticated session. Final URL: ${auth.url}. Status: ${auth.status}. Body: ${JSON.stringify(auth.bodySnippet)}`
    );
  }
  return auth;
}

async function importXSessionFromChrome(session, timeoutMs) {
  const cookies = await loadChromeCookiesForDomain('x.com');
  const normalized = cookies
    .filter((cookie) => String(cookie.domain || '').includes('x.com'))
    .map((cookie) => ({
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain,
      path: cookie.path || '/',
      secure: Boolean(cookie.secure),
      httpOnly: Boolean(cookie.httpOnly),
      expires: typeof cookie.expires === 'number' && cookie.expires > 0 ? cookie.expires : undefined
    }));
  if (!normalized.length) {
    throw new Error('No X cookies were available from the local Chrome profile.');
  }
  await session.context.addCookies(normalized);
  await session.page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: timeoutMs });
  await session.page.waitForTimeout(2000);
  return xAuthState(session.page, timeoutMs);
}

async function bootstrapXSessionInteractive(sessionId, request, timeoutMs) {
  const session = await ensureSession(sessionId, {
    headless: false,
    timeoutMs
  });
  try {
    return await bootstrapXSession(session, request, Math.min(timeoutMs, 90_000));
  } catch {}

  const page = session.page;
  await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: Math.min(timeoutMs, DEFAULT_TIMEOUT_MS) });
  await dismissXCookieBanner(page);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const auth = await xAuthState(page, Math.min(DEFAULT_TIMEOUT_MS, timeoutMs));
    if (auth.authenticated) {
      return auth;
    }
    await page.waitForTimeout(2000);
  }
  const auth = await xAuthState(page, Math.min(DEFAULT_TIMEOUT_MS, timeoutMs));
  throw new Error(
    `Interactive X bootstrap timed out before authentication. Final URL: ${auth.url}. Status: ${auth.status}. Body: ${JSON.stringify(auth.bodySnippet)}`
  );
}

function normalizeScript(script) {
  const trimmed = script.trim();
  if (!trimmed) {
    return 'undefined';
  }
  if (trimmed.includes(';') || trimmed.startsWith('return ') || trimmed.includes('\n')) {
    return `(function(){ ${trimmed} })()`;
  }
  return trimmed;
}

async function withTimeout(promise, timeoutMs, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
    })
  ]);
}

async function dispatch(request) {
  const session = await getPage(request);
  const page = session.page;
  const timeoutMs = request.timeout_ms || DEFAULT_TIMEOUT_MS;
  session.context.setDefaultTimeout(timeoutMs);

  switch (request.action) {
    case "open_url":
    case "navigate_url": {
      if (!request.url) {
        throw new Error("Missing url");
      }
      await page.goto(request.url, {
        waitUntil: request.wait_until || "domcontentloaded",
        timeout: timeoutMs
      });
      return ok(request.id, `Opened ${page.url()}`, {
        url: page.url(),
        title: await page.title()
      });
    }
    case "snapshot": {
      const snapshot = await buildSnapshot(page);
      return ok(request.id, snapshot.output, snapshot.data);
    }
    case "click": {
      if (!request.selector) {
        throw new Error("Missing selector");
      }
      await (await activeLocator(page, request.selector)).click({ timeout: timeoutMs });
      return ok(request.id, `Clicked ${request.selector}`, { url: page.url() });
    }
    case "type": {
      if (!request.selector) {
        throw new Error("Missing selector");
      }
      if (typeof request.text !== "string") {
        throw new Error("Missing text");
      }
      await typeInto(page, request.selector, request.text, request.replace === true);
      return ok(request.id, `Typed into ${request.selector}`, { selector: request.selector });
    }
    case "wait_for": {
      if (typeof request.ms === "number") {
        await page.waitForTimeout(request.ms);
        return ok(request.id, `Waited ${request.ms}ms`);
      }
      if (request.selector) {
        await (await activeLocator(page, request.selector)).waitFor({ state: "visible" });
        return ok(request.id, `Waited for ${request.selector}`);
      }
      if (request.text) {
        await page.getByText(request.text).first().waitFor({ state: "visible" });
        return ok(request.id, `Waited for text '${request.text}'`);
      }
      throw new Error("wait_for requires ms, selector, or text");
    }
    case "get_text": {
      const text = request.selector
        ? await (await activeLocator(page, request.selector)).innerText({ timeout: timeoutMs })
        : await page.locator("body").innerText({ timeout: timeoutMs });
      return ok(request.id, text, { text });
    }
    case "run_script": {
      if (!request.script) {
        throw new Error("Missing script");
      }
      const value = await page.evaluate(({ script }) => {
        // eslint-disable-next-line no-eval
        return eval(script);
      }, { script: normalizeScript(request.script) });
      return ok(
        request.id,
        typeof value === "string" ? value : JSON.stringify(value, null, 2),
        { value }
      );
    }
    case "screenshot":
    case "save_screenshot": {
      const filePath = await saveScreenshot(session, request.path, request.full_page !== false);
      return ok(request.id, filePath, { path: filePath });
    }
    case "save_trace": {
      const filePath = await saveTrace(session, request.path);
      return ok(request.id, filePath, { path: filePath });
    }
    case "x_status": {
      const auth = await withTimeout(xAuthState(page, timeoutMs), timeoutMs, "x_status");
      return ok(
        request.id,
        auth.authenticated
          ? `Authenticated X session at ${auth.url}`
          : `Unauthenticated X session at ${auth.url}`,
        auth
      );
    }
    case "export_x_cookies": {
      const cookies = await session.context.cookies("https://x.com");
      return ok(
        request.id,
        `Exported ${cookies.length} X cookies`,
        { cookies }
      );
    }
    case "bootstrap_x_session": {
      const auth = await withTimeout(
        bootstrapXSession(session, request, timeoutMs),
        timeoutMs,
        "bootstrap_x_session"
      );
      return ok(
        request.id,
        `Bootstrapped authenticated X session at ${auth.url}`,
        auth
      );
    }
    case "bootstrap_x_session_interactive": {
      const auth = await withTimeout(
        bootstrapXSessionInteractive(request.session || "default", request, timeoutMs),
        timeoutMs,
        "bootstrap_x_session_interactive"
      );
      return ok(
        request.id,
        `Bootstrapped interactive authenticated X session at ${auth.url}`,
        auth
      );
    }
    case "import_x_session_from_chrome": {
      const auth = await withTimeout(
        importXSessionFromChrome(session, timeoutMs),
        timeoutMs,
        "import_x_session_from_chrome"
      );
      return ok(
        request.id,
        auth.authenticated
          ? `Imported authenticated X session from Chrome at ${auth.url}`
          : `Imported Chrome cookies but X is still unauthenticated at ${auth.url}`,
        auth
      );
    }
    case "act":
    case "extract":
    case "observe": {
      throw new Error(
        "Stagehand-backed high-level actions are reserved in this sidecar surface but not enabled yet. Use deterministic Playwright actions first."
      );
    }
    default:
      throw new Error(`Unknown action '${request.action}'`);
  }
}

async function shutdownAll() {
  for (const session of sessions.values()) {
    try {
      if (session.traceActive) {
        await session.context.tracing.stop({
          path: path.join(session.paths.artifactsDir, `trace-final-${Date.now()}.zip`)
        });
      }
    } catch {}
    try {
      await session.context.close();
    } catch {}
  }
  sessions.clear();
}

process.on("SIGINT", async () => {
  await shutdownAll();
  process.exit(0);
});

process.on("SIGTERM", async () => {
  await shutdownAll();
  process.exit(0);
});

await ensureDir(STATE_ROOT);

const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity
});

process.stdout.on('error', (error) => {
  if (error && error.code === 'EPIPE') {
    stdoutBroken = true;
    process.exitCode = 0;
    rl.close();
    return;
  }
  throw error;
});

for await (const line of rl) {
  if (stdoutBroken) {
    break;
  }
  const trimmed = line.trim();
  if (!trimmed) {
    continue;
  }
  let request;
  try {
    request = JSON.parse(trimmed);
  } catch (error) {
    writeJsonLine(fail(null, `Invalid JSON: ${error.message}`));
    continue;
  }

  try {
    const response = await dispatch(request);
    writeJsonLine(response);
  } catch (error) {
    writeJsonLine(fail(request.id ?? null, error.message || String(error)));
  }
}

await shutdownAll();
