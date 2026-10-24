import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 导入页「切走再回来不丢进度」回归（全离线，拦截所有 /api/**）。
 *
 * 背景：导入面板此前把状态放在组件里，而 `ProjectWorkspace` 是按
 * `activeTab === 'import'` 条件渲染的 —— 切到别的页签就卸载，回来时已选文件、
 * 扫描结果、舍弃标记、当前步骤全部归零。上百章的扫描要几十秒（实测 114 章约 47 秒），
 * 作者只是去大纲页看一眼，回来就得重新选文件、重新等。
 *
 * 现状：状态存在 `components/import/importStore.ts`（按 projectId 分开、纯内存），
 * 于是切页签不丢，但**刷新页面即清空** —— 这一条也被下面的用例钉住，
 * 因为已选文件是浏览器的 File 对象，本来就没法序列化保存。
 *
 * 覆盖：
 * A. 只选了文件：切页签再回来，已选清单还在；
 * B. 扫描完成：切页签再回来，结果与「已舍弃」标记都还在；
 * C. 清空要二次确认，点「取消」进度不变；
 * D. 确认清空后回到第一步；
 * E. Markdown / Word 子页签来回切换不重置；
 * F. 刷新页面回到第一步（内存态，刻意不落 storage）。
 */

const PID = 'md-import-state-fixture';
const NOW = '2026-10-03T00:00:00Z';

const CHAPTERS = [
  { id: 'c1', fileName: '第01章 雨夜.md', relativePath: '雨夜长卷/第01章 雨夜.md', title: '第一章 雨夜', content: '雨下了一整夜。', wordCount: 7, order: 1 },
  { id: 'c2', fileName: '第02章 旧档.md', relativePath: '雨夜长卷/第02章 旧档.md', title: '第二章 旧档', content: '档案被人动过。', wordCount: 7, order: 2 },
];

const PICKED = [
  { name: '第01章 雨夜.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第一章 雨夜\n\n雨下了一整夜。') },
  { name: '第02章 旧档.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第二章 旧档\n\n档案被人动过。') },
];

function projectPayload() {
  return {
    id: PID,
    name: '导入状态回归项目',
    subtitle: '',
    genre: 'mystery',
    status: 'drafting',
    premise: '雨夜里的旧档案。',
    currentWordCount: 0,
    targetWordCount: 10000,
    targetChapters: 2,
    characters: [],
    locations: [],
    worldRules: [],
    outline: { mainSummary: '', structure: 'three_act', nodes: [] },
    createdAt: NOW,
    updatedAt: NOW,
  };
}

const SCAN_RESULT = {
  success: true,
  folderName: '雨夜长卷',
  totalFiles: CHAPTERS.length,
  totalWords: 14,
  chapters: CHAPTERS,
  analysis: {
    characters: [
      { name: '林晚', frequency: 2 },
      { name: '雨夜', frequency: 1 },
    ],
    locations: [{ name: '旧档案室', frequency: 2 }],
    timeline: [{ text: '三年前', type: 'past', chapter: 1 }],
    foreshadowing: [{ text: '半份手稿', category: '道具', context: '档案被人动过。' }],
    summary: '已分析 2 章，识别出 2 个角色、1 个地点。',
  },
};

async function installFixture(page: Page) {
  const state = { scanBodies: [] as string[], ingestBodies: [] as string[], unexpected: [] as string[], pageErrors: [] as string[] };
  page.on('pageerror', (error) => state.pageErrors.push(error.message));

  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });
  const base = `/api/projects/${PID}`;

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/\/$/, '');
    const method = request.method();

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    if (path === '/api/import/markdown-files' && method === 'POST') {
      state.scanBodies.push(request.postData() || '');
      return ok(route, SCAN_RESULT);
    }

    if (path === '/api/import/markdown-files/ingest' && method === 'POST') {
      state.ingestBodies.push(request.postData() || '');
      return ok(route, { project: projectPayload(), chapters: CHAPTERS, importedChapters: CHAPTERS.length, totalWords: 14 });
    }

    if (method === 'GET') {
      if (path === base) return ok(route, projectPayload());
      if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
      if (path.endsWith('/chapters')) return ok(route, []);
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) return ok(route, { styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {} });
      if (path.endsWith('/style-profile') || path.endsWith('/style-transition')) return ok(route, null);
      if (path.endsWith('/story-state')) return ok(route, { projectId: PID, version: 1 });
      if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
      if (path === `/api/ai/project/${PID}`) return ok(route, null);
      if (path === '/api/projects' || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) return ok(route, []);
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${PID}`);
  await page.getByTestId('workspace-tab-import').click();
  await expect(page.getByTestId('markdown-import-panel')).toBeVisible();
  return state;
}

// ── A. 只选了文件 ─────────────────────────────────────────

test('switching tabs keeps the picked files', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await expect(page.getByTestId('markdown-picked-files')).toContainText('已选 2 个文件');

  await page.getByTestId('workspace-tab-outline').click();
  await expect(page.getByTestId('markdown-import-panel')).toHaveCount(0);

  await page.getByTestId('workspace-tab-import').click();
  // 不需要再选一次文件
  await expect(page.getByTestId('markdown-picked-files')).toContainText('已选 2 个文件');
  await expect(page.getByTestId('markdown-upload-scan-button')).toBeEnabled();
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

// ── B. 扫描结果与舍弃标记 ─────────────────────────────────

test('switching tabs keeps the scan result and the discard marks', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toContainText('共 2 章');

  // 叉掉一个误识别的人物
  await page.getByTestId('analysis-discard-characters-雨夜').click();
  await expect(page.getByTestId('analysis-discard-notice-characters')).toContainText('已舍弃 1 项人物');

  await page.getByTestId('workspace-tab-outline').click();
  await page.getByTestId('workspace-tab-import').click();

  // 结果、章节清单、舍弃标记都在 —— 不需要重新扫描
  await expect(page.getByTestId('markdown-scan-success')).toContainText('共 2 章');
  await expect(page.getByText('第一章 雨夜')).toBeVisible();
  await expect(page.getByTestId('analysis-discard-notice-characters')).toContainText('已舍弃 1 项人物');
  // 扫描请求只发过一次：回来时没有偷偷重扫
  expect(state.scanBodies).toHaveLength(1);
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

// ── C/D. 清空 ─────────────────────────────────────────────

test('clearing asks for confirmation first, and cancel keeps the progress', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  await page.getByTestId('markdown-import-clear').click();
  await expect(page.getByTestId('markdown-import-clear-confirm')).toBeVisible();
  await page.getByTestId('markdown-import-clear-cancel').click();

  // 取消后进度不变
  await expect(page.getByTestId('markdown-scan-success')).toContainText('共 2 章');
  await expect(page.getByTestId('markdown-import-clear-confirm')).toHaveCount(0);
  expect(state.pageErrors).toEqual([]);
});

test('confirming the clear goes back to the first step', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  await page.getByTestId('markdown-import-clear').click();
  await page.getByTestId('markdown-import-clear-confirm').click();

  // 回到第一步：扫描结果没了、已选文件也没了，按钮本身也随之隐藏
  await expect(page.getByTestId('markdown-scan-success')).toHaveCount(0);
  await expect(page.getByTestId('markdown-picked-files')).toHaveCount(0);
  // 文件输入框本身带 hidden 样式，用存在性判定；可见的是包着它的按钮文案
  await expect(page.getByTestId('markdown-file-input')).toHaveCount(1);
  await expect(page.getByText('选择 .md 文件（可多选）')).toBeVisible();
  await expect(page.getByTestId('markdown-import-clear')).toHaveCount(0);

  // 再切一次页签，确认清空后不会「复活」成旧结果
  await page.getByTestId('workspace-tab-outline').click();
  await page.getByTestId('workspace-tab-import').click();
  await expect(page.getByTestId('markdown-file-input')).toHaveCount(1);
  await expect(page.getByTestId('markdown-scan-success')).toHaveCount(0);
  expect(state.pageErrors).toEqual([]);
});

test('a scan that finishes after the clear does not resurrect the panel', async ({ page }) => {
  const state = await installFixture(page);

  // 让扫描卡住，制造「清空时请求还在路上」的场景
  await page.route('**/api/import/markdown-files', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    return route.fulfill({ json: { success: true, data: SCAN_RESULT } });
  });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByText('正在扫描并分析章节')).toBeVisible();

  await page.getByTestId('workspace-tab-outline').click();
  await page.getByTestId('workspace-tab-import').click();
  // 切回来还在扫（状态没丢），这时清空
  await page.getByTestId('markdown-import-clear').click();
  await page.getByTestId('markdown-import-clear-confirm').click();

  // 等那次被作废的请求返回
  await page.waitForTimeout(1600);
  await expect(page.getByTestId('markdown-file-input')).toHaveCount(1);
  await expect(page.getByTestId('markdown-scan-success')).toHaveCount(0);
  expect(state.pageErrors).toEqual([]);
});

// ── E. 子页签 ─────────────────────────────────────────────

test('the markdown / word sub-tabs keep their own state', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  // 去 Word 建议书，再切回 Markdown 章节
  await page.getByTestId('import-mode-word').click();
  await expect(page.getByTestId('word-import-panel')).toBeVisible();
  await page.getByTestId('import-mode-markdown').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  // 停在 Word 页签时离开整个导入页，回来还应该停在 Word
  await page.getByTestId('import-mode-word').click();
  await page.getByTestId('workspace-tab-outline').click();
  await page.getByTestId('workspace-tab-import').click();
  await expect(page.getByTestId('word-import-panel')).toBeVisible();

  await page.getByTestId('import-mode-markdown').click();
  await expect(page.getByTestId('markdown-scan-success')).toContainText('共 2 章');
  expect(state.pageErrors).toEqual([]);
});

// ── F. 刷新 ───────────────────────────────────────────────

test('reloading the page clears the import flow on purpose', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  await page.reload();
  await page.getByTestId('workspace-tab-import').click();

  // 已选文件是 File 对象，本来就存不下来；保留一半只会让作者以为「文件还在」而导入失败
  await expect(page.getByTestId('markdown-file-input')).toHaveCount(1);
  await expect(page.getByTestId('markdown-scan-success')).toHaveCount(0);
  await expect(page.getByTestId('markdown-picked-files')).toHaveCount(0);
  expect(state.pageErrors).toEqual([]);
});
