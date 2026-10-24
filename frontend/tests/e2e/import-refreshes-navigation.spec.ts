import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 导入后章节导航必须跟着换新（回归）。
 *
 * 背景（作者报告）：导入完成后正文能显示，但点左侧章节导航里任意一章，正文又变回
 * 「本章尚未创作，可点击"AI 续写"…」。服务端日志给出了真相：导航点的是**导入前**的
 * 章节 id，那些章节已被这次导入回收，`GET /chapters/{id}` 于是 404，编辑器默默保留空内容，
 * 界面就把「读不到」显示成了「这一章是空的」。
 *
 * 根因：其他章节变更都会发 `novelcraft:project-refresh` 让工作台重新拉项目（大纲、字数），
 * 唯独导入这条路没发 —— 而导入恰恰是唯一会**整批**换掉章节 id 的操作。
 *
 * 覆盖：
 * A. 导入成功后，导航立刻换成导入的章节，点开能读到正文；
 * B. 点开一个已不存在的章节时，编辑器如实说「读不到」，不再谎称「本章尚未创作」。
 */

const PID = 'import-nav-fixture';
const NOW = '2026-10-04T00:00:00Z';

const CHAPTERS = [
  { id: 'c1', fileName: '第01章 雨夜.md', relativePath: '雨夜长卷/第01章 雨夜.md', title: '第一章 雨夜', content: '雨下了一整夜。', wordCount: 7, order: 1 },
  { id: 'c2', fileName: '第02章 旧档.md', relativePath: '雨夜长卷/第02章 旧档.md', title: '第二章 旧档', content: '档案被人动过。', wordCount: 7, order: 2 },
];

const PICKED = [
  { name: '第01章 雨夜.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第一章 雨夜\n\n雨下了一整夜。') },
  { name: '第02章 旧档.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第二章 旧档\n\n档案被人动过。') },
];

/** 导入前项目里的大纲：它的章节已经不在库里了（就是作者当时点的那种节点）。 */
const STALE_NODE = {
  id: 'stale-chapter-id',
  parentId: null,
  type: 'chapter',
  title: '导入前的旧章节',
  summary: '',
  order: 1,
  targetWordCount: 10,
};

function importedOutline() {
  return {
    mainSummary: '',
    structure: 'three_act',
    nodes: CHAPTERS.map((chapter) => ({
      id: chapter.id,
      parentId: null,
      type: 'chapter',
      title: chapter.title,
      summary: '',
      order: chapter.order,
      targetWordCount: chapter.wordCount,
    })),
    outlineVersion: 1,
  };
}

function projectPayload(outline: unknown, characters: unknown[] = [], importAnalysis: unknown = null) {
  return {
    id: PID,
    name: '导入导航回归项目',
    subtitle: '',
    genre: 'mystery',
    status: 'drafting',
    premise: '雨夜里的旧档案。',
    currentWordCount: 0,
    targetWordCount: 10000,
    targetChapters: 2,
    characters,
    locations: [],
    worldRules: [],
    importAnalysis,
    outline,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

/**
 * 导入扫描识别到的人物候选：只有姓名 + 出现次数，不编造性别/外貌/性格。
 * 导入**不再**把它们写进 `projects.characters`，候选留在 `importAnalysis` 里，
 * 等作者在「人物」页勾选后才由后端入档。
 */
const CHAR_LINWAN = {
  id: 'char-linwan',
  name: '林晚',
  role: 'supporting',
  basicInfo: { identity: '', gender: '', appearance: '', occupation: '' },
  personality: { description: '' },
  background: '导入扫描时在正文中出现 12 次。姓名由词频/词性启发式识别，可能有误判，请人工确认。',
};

const IMPORT_ANALYSIS = {
  characters: [{ name: '林晚', frequency: 12, aliases: [] }],
  locations: [],
  timeline: [],
  foreshadowing: [],
  summary: '已分析 2 章。',
};

const SCAN_RESULT = {
  success: true,
  folderName: '雨夜长卷',
  totalFiles: CHAPTERS.length,
  totalWords: 14,
  chapters: CHAPTERS,
  analysis: { characters: [], locations: [], timeline: [], foreshadowing: [], summary: '已分析 2 章。' },
};

async function installFixture(page: Page) {
  const state = {
    unexpected: [] as string[],
    pageErrors: [] as string[],
    projectGets: 0,
    acceptedNames: [] as string[],
  };
  // 服务端状态：导入前是那份陈旧大纲，导入后换成新大纲
  let ingested = false;
  // 「人物」页勾选后真正入库的名字（导入本身不写）
  let accepted: string[] = [];
  page.on('pageerror', (error) => state.pageErrors.push(error.message));

  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });
  const base = `/api/projects/${PID}`;
  const currentOutline = () => (ingested ? importedOutline() : { mainSummary: '', structure: 'three_act', nodes: [STALE_NODE] });
  // 导入只写 importAnalysis；人物要等作者勾选后才进 characters（真实后端如此）
  const currentCharacters = () => (accepted.includes('林晚') ? [CHAR_LINWAN] : []);
  const currentAnalysis = () => (ingested ? IMPORT_ANALYSIS : null);

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/\/$/, '');
    const method = request.method();

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    if (path === '/api/import/markdown-files' && method === 'POST') return ok(route, SCAN_RESULT);

    if (path === `/api/import/projects/${PID}/characters/from-analysis` && method === 'POST') {
      const body = (request.postDataJSON() || {}) as { names?: string[] };
      const names = body.names || [];
      state.acceptedNames.push(...names);
      accepted = Array.from(new Set([...accepted, ...names]));
      const added = names.filter((name) => IMPORT_ANALYSIS.characters.some((item) => item.name === name)).length;
      return ok(route, {
        project: projectPayload(currentOutline(), currentCharacters(), currentAnalysis()),
        added,
      });
    }

    if (path === '/api/import/markdown-files/ingest' && method === 'POST') {
      ingested = true;
      return ok(route, {
        project: projectPayload(importedOutline(), currentCharacters(), currentAnalysis()),
        chapters: CHAPTERS,
        importedChapters: CHAPTERS.length,
        totalWords: 14,
        mergeMode: 'replace',
        addedChapters: CHAPTERS.length,
        updatedChapters: 0,
        keptChapters: 0,
        recycledChapters: 1,
        cleanedChapters: 0,
        cleanedDetails: [],
        charactersDetected: IMPORT_ANALYSIS.characters.length,
      });
    }

    if (method === 'GET') {
      if (path === base) {
        state.projectGets += 1;
        return ok(route, projectPayload(currentOutline(), currentCharacters(), currentAnalysis()));
      }
      if (path.endsWith('/chapters')) return ok(route, ingested ? CHAPTERS : []);
      if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) return ok(route, { styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {} });
      if (path.endsWith('/style-profile') || path.endsWith('/style-transition')) return ok(route, null);
      if (path.endsWith('/story-state')) return ok(route, { projectId: PID, version: 1 });
      if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
      if (path === `/api/ai/project/${PID}`) return ok(route, null);
      if (path === '/api/projects' || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) return ok(route, []);
      if (path.includes(`/chapters/`) && path.endsWith('/projections')) return ok(route, []);
      if (path.includes(`/chapters/`)) {
        // 只有导入进来的章节存在；陈旧的节点 id 一律 404（与真实服务端一致）
        const chapterId = path.split('/chapters/')[1];
        const chapter = CHAPTERS.find((item) => item.id === chapterId);
        if (chapter) return ok(route, { ...chapter, projectId: PID });
        return route.fulfill({ status: 404, json: { detail: '章节未找到。' } });
      }
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${PID}`);
  return state;
}

// ── A. 导入后导航换新 ─────────────────────────────────────

test('after an import the chapter navigation shows the imported chapters and they open', async ({ page }) => {
  const state = await installFixture(page);

  // 导入前：导航里是那份陈旧大纲（它的章节已不在库里）
  await expect(page.getByTestId('chapter-tree-item-stale-chapter-id')).toBeVisible();

  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('import-mode-markdown').click();
  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();
  await page.getByTestId('markdown-ingest-button').click();
  await expect(page.getByTestId('markdown-ingest-success')).toContainText('已导入 2 章');

  // 导航必须换成导入进来的章节（此前会一直显示「导入前的旧章节」→ 点开就是 404）
  await expect(page.getByTestId('chapter-tree-item-c1')).toBeVisible();
  await expect(page.getByTestId('chapter-tree-item-c2')).toBeVisible();
  await expect(page.getByTestId('chapter-tree-item-stale-chapter-id')).toHaveCount(0);

  // 点第二章：正文必须真的读出来
  await page.getByTestId('chapter-tree-item-c2').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/档案被人动过/);
  await expect(page.getByTestId('chapter-load-error')).toHaveCount(0);

  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

// ── B. 导入把识别到的人物补进「人物」页 ───────────────────

test('an import lists the detected characters as candidates instead of writing them', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('import-mode-markdown').click();
  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();
  await page.getByTestId('markdown-ingest-button').click();

  // 文案只能说「识别到候选、去挑」，不能说「已补进」——导入确实没写档案
  const success = page.getByTestId('markdown-ingest-success');
  await expect(success).toContainText('识别到 1 位人物候选，已列在「人物」页，请挑选后加入档案。');
  await expect(success).not.toContainText('已把识别到');

  // 切到人物页：候选在，但**不在**角色列表里
  await page.getByTestId('workspace-tab-characters').click();
  await expect(page.getByTestId('import-character-candidates')).toBeVisible();
  await expect(page.getByTestId('import-candidate-林晚')).toContainText('12 次');
  await expect(page.getByTestId('character-card-char-linwan')).toHaveCount(0);

  // 一个都没勾时按钮是禁用的：不能出现「点了却什么都没发生」
  await expect(page.getByTestId('import-candidates-accept')).toBeDisabled();

  // 勾选后加入：真的入档，并且只带事实、不编造性别/外貌/性格
  await page.getByTestId('import-candidate-checkbox-林晚').check();
  await page.getByTestId('import-candidates-accept').click();

  await expect(page.getByTestId('character-card-char-linwan')).toBeVisible();
  await expect(page.getByTestId('character-detail-name')).toHaveText('林晚');
  await expect(
    page.getByText('导入扫描时在正文中出现 12 次。姓名由词频/词性启发式识别，可能有误判，请人工确认。').first(),
  ).toBeVisible();
  // 已经进来的不再是候选，卡片自行消失
  await expect(page.getByTestId('import-character-candidates')).toHaveCount(0);

  expect(state.acceptedNames).toEqual(['林晚']);
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

test('dismissing the candidate list is remembered for that batch', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('import-mode-markdown').click();
  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();
  await page.getByTestId('markdown-ingest-button').click();
  await expect(page.getByTestId('markdown-ingest-success')).toBeVisible();

  await page.getByTestId('workspace-tab-characters').click();
  await page.getByTestId('import-candidates-dismiss').click();
  await expect(page.getByTestId('import-character-candidates')).toHaveCount(0);

  // 切走再回来（组件重建，读的是浏览器里记下的「已忽略」）仍然不显示
  await page.getByTestId('workspace-tab-write').click();
  await page.getByTestId('workspace-tab-characters').click();
  await expect(page.getByTestId('import-character-candidates')).toHaveCount(0);

  expect(state.acceptedNames).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

// ── C. 404 的两种含义必须分开（见 docs/CHANGELOG 2026-10-12）───────────────────
//
// `GET /chapters/{id}` 返回 404 有两种完全不同的成因，**服务端无法区分**，只能靠客户端：
//   ① 节点在这份大纲里、但 chapters 表还没有行 —— **尚未创作**（新建项目/刚生成大纲时，
//      每个章节节点都是这个状态：首次保存才会用 PUT 建行）；
//   ② 节点已不在大纲里 —— 才是被整批替换 / 移入回收站。
// 这里钉的是 ①：作者「新建项目、还没创作，正文页就弹报错」就是把它误判成了 ②。
//
// （旧版本这条用例把 ① 当成「已不存在」：它点击的节点来自服务端大纲，所以那个"陈旧节点"
//   在真实应用里根本渲染不出来 —— 树就是按当前大纲渲染的。改成按可达状态断言。）

test('a chapter node that was never written shows the empty state, not a "no longer exists" error', async ({ page }) => {
  const state = await installFixture(page);

  // 不导入：服务端大纲里有这个节点，但 chapters 表里没有对应的行（从未保存过）
  await page.getByTestId('chapter-tree-item-stale-chapter-id').click();

  await expect(page.getByTestId('chapter-load-error')).toHaveCount(0);
  // 这一章确实读不到正文，但原因不是"服务端没这一章"，而是"还没写过"。
  await expect(page.getByTestId('editor-textarea')).toHaveAttribute('placeholder', /本章尚未创作/);
  await expect(page.getByTestId('editor-textarea')).not.toHaveAttribute('placeholder', /读不到正文/);

  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});
