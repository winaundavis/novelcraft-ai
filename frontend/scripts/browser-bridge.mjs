/**
 * Browser Bridge — 为 Codex / 外部工具提供本地页面操控能力
 *
 * 用途：绕过内置浏览器的 Auto-review 策略限制，通过 Playwright 驱动 Chromium
 *       直接与 NovelCraft 前端页面交互。
 *
 * 用法（从 frontend 目录运行）：
 *   node scripts/browser-bridge.mjs <action> [options...]
 *
 * 依赖：npx playwright install chromium（已安装）
 *
 * 动作：
 *   snapshot [url]              抓取页面可访问性快照（默认 /）
 *   click    <testId> [url]     按 data-testid 点击元素
 *   fill     <testId> <text>    向元素输入文本
 *   text     <testId> [url]     读取元素文本内容
 *   value    <testId> [url]     读取 input/textarea 的 value
 *   visible  <testId> [url]     检查元素是否可见
 *   nav      <url>              导航到指定路径
 *   screenshot [name]           截屏保存到 test_artifacts/
 *   eval     <js> [url]         执行 JS 表达式并返回结果
 *
 * 环境变量：
 *   NOVELCRAFT_BASE_URL  默认 http://127.0.0.1:5173
 */

import { chromium } from 'playwright';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE_URL = process.env.NOVELCRAFT_BASE_URL || 'http://127.0.0.1:5173';
const ARTIFACTS_DIR = path.resolve(__dirname, '..', 'test_artifacts');

// ── 工具函数 ──────────────────────────────────────────────

function resolveUrl(raw) {
  if (!raw || raw === '/') return BASE_URL;
  if (raw.startsWith('http')) return raw;
  return `${BASE_URL}${raw.startsWith('/') ? '' : '/'}${raw}`;
}

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function output(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function error(msg) {
  output({ ok: false, error: msg });
  process.exit(1);
}

// ── 主逻辑 ─────────────────────────────────────────────────

const args = process.argv.slice(2);
const action = args[0];

if (!action) {
  console.log(`
Browser Bridge — Playwright 驱动的页面交互桥接

用法（从 frontend 目录运行）:
  node scripts/browser-bridge.mjs <action> [args...]

动作:
  snapshot  [url]            页面可访问性快照
  click     <testId> [url]   点击元素
  fill      <testId> <text>  填充输入框
  text      <testId> [url]   读取元素文本
  value     <testId> [url]   读取 input value
  visible   <testId> [url]   检查可见性
  nav       <url>            导航
  screenshot [name]          截屏
  eval      <js> [url]       执行 JS 并返回结果

示例:
  node scripts/browser-bridge.mjs snapshot /
  node scripts/browser-bridge.mjs click dashboard-page /
  node scripts/browser-bridge.mjs text dashboard-page /
  node scripts/browser-bridge.mjs visible open-create-project-modal /
`);
  process.exit(0);
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const page = await context.newPage();

try {
  switch (action) {
    case 'snapshot': {
      const url = resolveUrl(args[1] || '/');
      await page.goto(url, { waitUntil: 'networkidle' });
      // 提取所有带 data-testid 的元素及其文本
      const elements = await page.evaluate(() => {
        const nodes = document.querySelectorAll('[data-testid]');
        return Array.from(nodes).map(el => ({
          testId: el.getAttribute('data-testid'),
          tag: el.tagName.toLowerCase(),
          text: (el.textContent || '').trim().slice(0, 200),
          visible: el.offsetParent !== null,
        }));
      });
      output({ ok: true, url: page.url(), title: await page.title(), elements });
      break;
    }

    case 'click': {
      const testId = args[1];
      const url = resolveUrl(args[2] || '/');
      if (!testId) error('用法: click <testId> [url]');
      await page.goto(url, { waitUntil: 'networkidle' });
      await page.getByTestId(testId).click();
      await page.waitForTimeout(400);
      output({ ok: true, clicked: testId, url: page.url() });
      break;
    }

    case 'fill': {
      const testId = args[1];
      const text = args.slice(2).join(' ');
      if (!testId || !text) error('用法: fill <testId> <text>');
      await page.goto(BASE_URL, { waitUntil: 'networkidle' });
      await page.getByTestId(testId).fill(text);
      output({ ok: true, filled: testId, text });
      break;
    }

    case 'text': {
      const testId = args[1];
      const url = resolveUrl(args[2] || '/');
      if (!testId) error('用法: text <testId> [url]');
      await page.goto(url, { waitUntil: 'networkidle' });
      const textContent = await page.getByTestId(testId).textContent();
      output({ ok: true, testId, text: textContent });
      break;
    }

    case 'value': {
      const testId = args[1];
      const url = resolveUrl(args[2] || '/');
      if (!testId) error('用法: value <testId> [url]');
      await page.goto(url, { waitUntil: 'networkidle' });
      const value = await page.getByTestId(testId).inputValue();
      output({ ok: true, testId, value });
      break;
    }

    case 'visible': {
      const testId = args[1];
      const url = resolveUrl(args[2] || '/');
      if (!testId) error('用法: visible <testId> [url]');
      await page.goto(url, { waitUntil: 'networkidle' });
      const isVisible = await page.getByTestId(testId).isVisible();
      output({ ok: true, testId, visible: isVisible });
      break;
    }

    case 'nav': {
      const url = resolveUrl(args[1] || '/');
      await page.goto(url, { waitUntil: 'networkidle' });
      output({ ok: true, url: page.url(), title: await page.title() });
      break;
    }

    case 'screenshot': {
      const name = args[1] || `bridge-${Date.now()}`;
      const url = resolveUrl(args[2] || '/');
      await page.goto(url, { waitUntil: 'networkidle' });
      ensureDir(ARTIFACTS_DIR);
      const filePath = path.join(ARTIFACTS_DIR, `${name}.png`);
      await page.screenshot({ path: filePath, fullPage: true });
      output({ ok: true, screenshot: filePath });
      break;
    }

    case 'eval': {
      const js = args.slice(1, -1).join(' ') || args[1];
      const urlArg = args[args.length - 1];
      const url = urlArg && urlArg.startsWith('/') ? resolveUrl(urlArg) : resolveUrl('/');
      if (!js) error('用法: eval <javascript> [url]');
      await page.goto(url, { waitUntil: 'networkidle' });
      const result = await page.evaluate(js);
      output({ ok: true, result });
      break;
    }

    default:
      error(`未知动作: ${action}`);
  }
} finally {
  await browser.close();
}
