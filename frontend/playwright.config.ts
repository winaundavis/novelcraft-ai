import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.NOVELCRAFT_BASE_URL || 'http://127.0.0.1:5173';

// 少数 spec 直接打真实后端（novelcraft-smoke / projection-panel），需要
// `INVITE_TOKENS` 里的邀请码。它们会创建、删除甚至直接写库，所以必须显式开关：
// 没设 `NOVELCRAFT_E2E_INVITE` 时这些 spec 会整体 skip，而不是每个用例都撞 401。
const e2eInvite = process.env.NOVELCRAFT_E2E_INVITE || '';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  timeout: 90_000,
  expect: {
    timeout: 10_000,
  },
  retries: process.env.CI ? 1 : 0,
  // Codex C4：默认跳过真实 LLM 用例（@live-llm），保证 CI 稳定可复现；
  // 需要跑真实 LLM 时设 RUN_LIVE_LLM=1
  grepInvert: process.env.RUN_LIVE_LLM === '1' ? undefined : /@live-llm/,
  // Codex P2-1：把 RUN_LIVE_LLM 环境标记写入报告 metadata（从测试运行时读取，
  // 而非报告生成时），供验收脚本核验本次运行确实是真实 LLM 全量运行。
  metadata: {
    RUN_LIVE_LLM: process.env.RUN_LIVE_LLM || '0',
    timestamp: new Date().toISOString(),
  },
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
    // Codex P2-4：JSON 逐项结果报告（含名称/状态/耗时），供独立可核验审计。
    // 默认输出仓库外带时间戳文件（不可覆盖）；CI 或本地可显式指定 E2E_REPORT_FILE。
    ['json', { outputFile: process.env.E2E_REPORT_FILE || getDefaultJsonReportPath() }],
  ],
  use: {
    baseURL,
    // 所有请求（含 `request` fixture 发出的后端调用）都带上旧邀请码身份。
    // 离线 spec 拦截了 /api/**，多这个头无影响。
    extraHTTPHeaders: e2eInvite ? { 'X-NovelCraft-Invite': e2eInvite } : {},
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    viewport: { width: 1440, height: 960 },
  },
  projects: [
    {
      name: 'local-browser-audit',
      use: {
        ...devices['Desktop Chrome'],
      },
    },
  ],
});

function getDefaultJsonReportPath(): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  // 仓库外目录：<REPO_ROOT>\creat-reports\（不可被 git 覆盖，每次 live 运行新增文件）
  return `F:/VSC-DSK/creat-reports/e2e-${stamp}.json`;
}
