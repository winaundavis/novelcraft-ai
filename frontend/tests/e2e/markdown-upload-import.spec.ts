import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * Markdown 章节「上传式」导入回归（全离线，拦截所有 /api/**）。
 *
 * 背景：路径式接口在 DEBUG=false 下一律 403，而前端此前只有路径模式，
 * 于是生产配置下 Markdown 导入完全不可用、报错还写着「仅支持上传文件」。
 *
 * 覆盖：
 * A. 上传是默认模式；文件夹模式带「仅本地开发可用」提示；
 * B. 选文件：过滤非 Markdown、显示已选清单；
 * C. 扫描：multipart 里必须带上 files 与同序的 relativePaths；
 * D. 入库：提示成功；
 * E. 失败：把后端原因如实显示，不回退到路径模式。
 */

const PID = 'md-upload-fixture';
const NOW = '2026-09-17T00:00:00Z';

type Mode =
  | { kind: 'ok' }
  | { kind: 'status'; status: number; detail: string };

const CHAPTERS = [
  { id: 'c1', fileName: '第01章 雨夜.md', relativePath: '雨夜长卷/第01章 雨夜.md', title: '第一章 雨夜', content: '雨下了一整夜。', wordCount: 7, order: 1 },
  { id: 'c2', fileName: '第02章 旧档.md', relativePath: '雨夜长卷/第02章 旧档.md', title: '第二章 旧档', content: '档案被人动过。', wordCount: 7, order: 2 },
];

function projectPayload() {
  return {
    id: PID,
    name: '上传导入回归项目',
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
    outline: { mainSummary: '', structure: 'three-act', nodes: [] },
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function styleProfilePayload() {
  return {
    id: 'style-profile-fixture',
    projectId: PID,
    status: 'completed',
    narrativeRules: [{ rule: '通过行动展现人物选择。', key: 'action', source: 'llm' }],
    characterRules: [],
    languageRules: [],
    rhythmRules: [],
    antiAiRules: [],
    strengths: ['叙事清晰。'],
    weaknesses: ['可以增加节奏变化。'],
    dimensions: {
      avgSentenceLength: 18,
      dialogueRatio: 0.3,
      descriptionDensity: 0.2,
      paragraphLength: 'short',
      tone: '平稳克制',
    },
    metrics: {},
    sampleCount: CHAPTERS.length,
    sampleChars: 4800,
    // 把档位推到首屏以外，覆盖分析完成后滚动至报告底部的真实操作路径。
    report: Array.from({ length: 80 }, (_, index) => `采样段落 ${index + 1}：人物通过具体行动推进调查，保留线索与情绪的因果关系。`).join('\n'),
  };
}

const TRANSITION_CONFIG = {
  fast: { intervalChapters: 3, totalSteps: 3 },
  medium: { intervalChapters: 6, totalSteps: 10 },
  slow: { intervalChapters: 10, totalSteps: 20 },
};

function styleTransitionPayload(pace: keyof typeof TRANSITION_CONFIG) {
  const config = TRANSITION_CONFIG[pace];
  return {
    id: 'style-transition-fixture',
    projectId: PID,
    profileId: 'style-profile-fixture',
    pace,
    progress: 0,
    ...config,
    plan: {
      step: 0,
      weight: 0,
      start_chapter_count: CHAPTERS.length,
      steps: Array.from({ length: config.totalSteps }, (_, index) => ({
        step: index + 1,
        weight: (index + 1) / config.totalSteps,
      })),
    },
    startedAt: NOW,
    updatedAt: NOW,
  };
}

async function installFixture(page: Page, mode: Mode) {
  const state = {
    scanBodies: [] as string[],
    ingestBodies: [] as string[],
    styleRequests: [] as string[],
    transitionBodies: [] as string[],
    unexpected: [] as string[],
    pageErrors: [] as string[],
  };
  let styleProfile: ReturnType<typeof styleProfilePayload> | null = null;
  let styleTransition: ReturnType<typeof styleTransitionPayload> | null = null;
  page.on('pageerror', (error) => state.pageErrors.push(error.message));

  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });
  const base = `/api/projects/${PID}`;

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/\/$/, '');
    const method = request.method();

    if ([`${base}/style-profile`, `${base}/style-analysis`, `${base}/style-transition`].includes(path)) {
      state.styleRequests.push(`${method} ${path}`);
    }

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    if (path === `${base}/style-analysis` && method === 'POST') {
      styleProfile = styleProfilePayload();
      return ok(route, styleProfile);
    }

    if (path === `${base}/style-transition` && method === 'PUT') {
      state.transitionBodies.push(request.postData() || '');
      const { pace } = request.postDataJSON();
      if (pace === 'fast' || pace === 'medium' || pace === 'slow') {
        styleTransition = styleTransitionPayload(pace);
        return ok(route, styleTransition);
      }
      state.unexpected.push(`${method} ${path} with invalid pace`);
      return route.fulfill({ status: 400, json: { detail: 'Invalid fixture pace' } });
    }

    if (path === '/api/import/markdown-files' && method === 'POST') {
      state.scanBodies.push(request.postData() || '');
      if (mode.kind === 'status') {
        return route.fulfill({ status: mode.status, json: { detail: mode.detail } });
      }
      return ok(route, {
        success: true,
        folderName: '雨夜长卷',
        totalFiles: CHAPTERS.length,
        totalWords: 14,
        chapters: CHAPTERS,
        analysis: {
          // 每个分类里都故意混一个「不是这一类」的误识别结果：作者要能把它叉掉
          characters: [
            { name: '林晚', frequency: 2 },
            { name: '雨夜', frequency: 1 },
          ],
          locations: [
            { name: '旧档案室', frequency: 2 },
            { name: '雨里', frequency: 1 },
          ],
          timeline: [
            { text: '三年前', type: 'past', chapter: 1 },
            { text: '雨下了一整夜', type: 'scene', chapter: 1 },
          ],
          foreshadowing: [
            { text: '半份手稿', category: '道具', context: '档案被人动过。' },
            { text: '雨', category: '意象', context: '雨下了一整夜。' },
          ],
          summary: '已分析 2 章，识别出 2 个角色、2 个地点、2 个时间节点。',
        },
      });
    }

    if (path === '/api/import/markdown-files/ingest' && method === 'POST') {
      state.ingestBodies.push(request.postData() || '');
      if (mode.kind === 'status') {
        return route.fulfill({ status: mode.status, json: { detail: mode.detail } });
      }
      // 像真后端那样按请求里的写入方式回传结果，避免夹具自己猜
      const merged = fieldValue(request.postData() || '', 'mergeMode') === 'merge';
      return ok(route, {
        project: projectPayload(),
        chapters: CHAPTERS,
        importedChapters: CHAPTERS.length,
        totalWords: 14,
        analysis: { summary: '已分析 2 章。' },
        mergeMode: merged ? 'merge' : 'replace',
        mergeKey: merged ? 'number' : 'index',
        addedChapters: merged ? 1 : CHAPTERS.length,
        updatedChapters: merged ? 1 : 0,
        keptChapters: merged ? 5 : 0,
        recycledChapters: merged ? 1 : 0,
        cleanedChapters: merged ? 1 : 0,
        cleanedDetails: merged ? [{ order: 3, title: '第四章 后窗' }] : [],
      });
    }

    if (method === 'GET') {
      if (path === base) return ok(route, projectPayload());
      if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
      if (path.endsWith('/chapters')) return ok(route, []);
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) return ok(route, {
        styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {},
      });
      // 导入成功后会出现文风分析面板，它会读这两条
      if (path.endsWith('/style-profile')) return ok(route, styleProfile);
      if (path.endsWith('/style-transition')) return ok(route, styleTransition);
      if (path.endsWith('/story-state')) return ok(route, { projectId: PID, version: 1 });
      if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
      if (path === `/api/ai/project/${PID}`) return ok(route, null);
      if (path === '/api/projects' || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) {
        return ok(route, []);
      }
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${PID}`);
  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('import-mode-markdown').click();
  await expect(page.getByTestId('markdown-import-panel')).toBeVisible();
  return state;
}

const PICKED = [
  { name: '第01章 雨夜.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第一章 雨夜\n\n雨下了一整夜。') },
  { name: '第02章 旧档.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第二章 旧档\n\n档案被人动过。') },
];

/** 从 multipart 请求体里取一个表单字段的值（字段之间用空行分隔）。 */
function fieldValue(body: string, name: string): string {
  const match = new RegExp(`name="${name}"\r?\n\r?\n([^\r\n]*)`).exec(body);
  return match ? match[1].trim() : '';
}

// ── A. 模式 ───────────────────────────────────────────────

test('upload is the default mode and the folder mode is marked dev-only', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  // 默认上传模式：没有路径输入框
  await expect(page.getByTestId('markdown-file-input')).toHaveCount(1);
  await expect(page.getByTestId('markdown-folder-input')).toHaveCount(0);

  // 切到文件夹模式才出现路径输入，并且明确标注仅本地开发可用
  await page.getByTestId('markdown-mode-folder').click();
  await expect(page.getByTestId('markdown-folder-input')).toBeVisible();
  await expect(page.getByTestId('markdown-folder-dev-only')).toContainText('DEBUG=true');

  await page.getByTestId('markdown-mode-upload').click();
  await expect(page.getByTestId('markdown-file-input')).toHaveCount(1);
  expect(state.pageErrors).toEqual([]);
});

// ── B. 选文件 ─────────────────────────────────────────────

test('picking files filters out non-markdown and lists what was picked', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles([
    ...PICKED,
    { name: '封面.png', mimeType: 'image/png', buffer: Buffer.from('not an image') },
  ]);

  await expect(page.getByTestId('markdown-picked-files')).toContainText('已选 2 个文件');
  await expect(page.getByTestId('markdown-skipped-notice')).toContainText('已忽略 1 个非 Markdown 文件');
  expect(state.scanBodies).toEqual([]);
});

// ── C. 扫描 ───────────────────────────────────────────────

test('scan uploads every file together with its relative path', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();

  await expect(page.getByTestId('markdown-scan-success')).toContainText('共 2 章');
  await expect(page.getByTestId('markdown-scan-success')).toContainText('约 14 字');

  expect(state.scanBodies).toHaveLength(1);
  const body = state.scanBodies[0];
  // 两个文件都必须上传，且相对路径同序带上（后端按下标对齐）
  expect(body).toContain('第01章 雨夜.md');
  expect(body).toContain('第02章 旧档.md');
  expect((body.match(/name="files"/g) || []).length).toBe(2);
  expect((body.match(/name="relativePaths"/g) || []).length).toBe(2);

  // 章节清单按顺序渲染
  await expect(page.getByText('第一章 雨夜')).toBeVisible();
  await expect(page.getByText('第二章 旧档')).toBeVisible();
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

// ── D. 入库 ───────────────────────────────────────────────

test('ingest re-uploads the same files and reports success', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  await page.getByTestId('markdown-ingest-button').click();

  await expect(page.getByTestId('markdown-ingest-success')).toContainText('已导入 2 章');
  expect(state.ingestBodies).toHaveLength(1);
  expect(state.ingestBodies[0]).toContain(`name="projectId"`);
  expect(state.ingestBodies[0]).toContain(PID);
  expect(state.unexpected).toEqual([]);
  // 回归锁：入库后文风面板会挂载，而 `/style-profile` 此时还没有数据。
  // 这里 fixture 故意回 200 + data:null（后端无画像时是 404），
  // 曾经把 normalize 出来的 {} 当成画像塞进 state，读 strengths.length 抛错、
  // 整棵树被卸载 → 纯白页面。异常可以不致命，但绝不能一个都不留。
  expect(state.pageErrors).toEqual([]);
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
});

test('an imported outline shows its structure in Chinese instead of the raw enum value', async ({ page }) => {
  // 夹具沿用真实导入数据的形状：structure 是连字符写法 'three-act'，
  // 而后端声明的枚举是下划线 'three_act'（schemas.GenerateOutlineRequest）。
  // 直接按枚举查中文名会漏掉导入来的项目，把内部值原样显示给作者
  // （用户报告：大纲页「结构」卡片上写着英文）。
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await page.getByTestId('markdown-ingest-button').click();
  await expect(page.getByTestId('markdown-ingest-success')).toBeVisible();

  await page.getByTestId('workspace-tab-outline').click();
  const label = page.getByTestId('outline-structure-label');
  await expect(label).toHaveText('三幕式结构');
  await expect(label).not.toContainText('three-act');
  await expect(label).not.toContainText('three_act');
  expect(state.pageErrors).toEqual([]);
});

test('mouse clicks on pace labels keep the workspace visible after a long style analysis', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await installFixture(page, { kind: 'ok' });
  const base = `/api/projects/${PID}`;

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await page.getByTestId('markdown-ingest-button').click();
  await expect(page.getByTestId('markdown-ingest-success')).toBeVisible();
  await page.getByTestId('style-analyze-button').click();
  await expect(page.getByTestId('style-profile-report')).toContainText('采样段落 80');
  await expect.poll(() => state.styleRequests.filter((request) => request === `GET ${base}/style-transition`).length).toBeGreaterThanOrEqual(3);
  // 文风面板现在在 enabled=false 时也会读取既有画像与计划：这是「刷新页面后仍能看到
  // 已有计划」的前提（enabled 只决定能否**新建**计划）。所以这里不再固定精确次数
  // （dev 下 StrictMode 会重复执行挂载 effect），改为锁定端点集合与关键次数不变式。
  expect(new Set(state.styleRequests)).toEqual(new Set([
    `GET ${base}/style-profile`,
    `GET ${base}/style-transition`,
    `POST ${base}/style-analysis`,
  ]));
  expect(state.styleRequests.filter((request) => request === `POST ${base}/style-analysis`)).toHaveLength(1);
  // 面板挂载与入库后各至少读取一次画像
  expect(state.styleRequests.filter((request) => request === `GET ${base}/style-profile`).length).toBeGreaterThanOrEqual(2);

  const workspace = page.getByTestId('workspace-root');
  const toolbar = page.getByTestId('toggle-right-panel').locator('..');
  const panel = page.getByTestId('style-analysis-panel');
  const paceGroup = panel.getByRole('radiogroup', { name: '过渡档位' });
  const startButton = page.getByTestId('style-transition-start-button');

  // 只在操作开始前滚至可见标签；点击后不再调用会自动修复滚动位置的 click/scrollIntoView。
  await expect(paceGroup).not.toBeInViewport();
  await startButton.scrollIntoViewIfNeeded();
  await expect(paceGroup).toBeInViewport({ ratio: 1 });
  await expect(toolbar).toBeInViewport({ ratio: 1 });
  const shellScroll = () => workspace.evaluate((element) => ({
    workspace: element.scrollTop,
    main: element.closest('main')?.scrollTop ?? 0,
  }));
  const beforeScroll = await shellScroll();
  const requestsBeforeSelection = [...state.styleRequests];

  for (const pace of ['fast', 'medium', 'slow'] as const) {
    const input = page.getByTestId(`style-pace-${pace}`);
    const label = input.locator('..');
    await expect(label).toBeInViewport({ ratio: 1 });
    const bounds = await label.boundingBox();
    expect(bounds).not.toBeNull();
    if (!bounds) throw new Error(`Missing visible label for ${pace}`);

    await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await expect(input).toBeChecked();
    await expect(input).toBeFocused();
    await expect(label).toBeInViewport({ ratio: 1 });
    await expect(paceGroup).toBeInViewport({ ratio: 1 });
    await expect(toolbar).toBeInViewport({ ratio: 1 });
    await expect(panel).toBeInViewport();
    expect(await shellScroll()).toEqual(beforeScroll);
    await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
    expect(state.pageErrors).toEqual([]);
    expect(state.styleRequests).toEqual(requestsBeforeSelection);
    expect(state.transitionBodies).toEqual([]);
  }

  await expect(startButton).toBeInViewport({ ratio: 1 });
  const startBounds = await startButton.boundingBox();
  expect(startBounds).not.toBeNull();
  if (!startBounds) throw new Error('Missing visible transition start button');
  await page.mouse.click(startBounds.x + startBounds.width / 2, startBounds.y + startBounds.height / 2);

  await expect(panel.getByRole('progressbar')).toBeVisible();
  await expect(panel).toContainText('慢速');
  await expect(panel.getByRole('progressbar')).toBeInViewport();
  await expect(toolbar).toBeInViewport({ ratio: 1 });
  await expect(panel).toBeInViewport();
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
  expect(state.transitionBodies.map((body) => JSON.parse(body))).toEqual([{ pace: 'slow' }]);
  expect(state.styleRequests).toEqual([...requestsBeforeSelection, `PUT ${base}/style-transition`]);
  expect(state.scanBodies).toHaveLength(1);
  expect(state.ingestBodies).toHaveLength(1);
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

// ── E. 失败 ───────────────────────────────────────────────

test('a rejected upload shows the server reason instead of falling back to a folder path', async ({ page }) => {
  const state = await installFixture(page, { kind: 'status', status: 400, detail: '只支持 .md / .markdown 文件：notes.txt' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();

  await expect(page.getByText('只支持 .md / .markdown 文件：notes.txt')).toBeVisible();
  // 仍然停在上传模式的第一步，没有被切回路径模式
  await expect(page.getByTestId('markdown-file-input')).toHaveCount(1);
  await expect(page.getByTestId('markdown-folder-input')).toHaveCount(0);
  expect(state.ingestBodies).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('the default mode never produces the old production 403 wording', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();

  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();
  await expect(page.getByText('生产环境仅支持上传文件。')).toHaveCount(0);
  expect(state.pageErrors).toEqual([]);
});

// ── F. 舍弃误识别的条目 ───────────────────────────────

/**
 * 自动识别用的是词性/词频启发式，四类都会有误判（天气被当成人物、短语被当成地点、
 * 场景句被当成时间线）。作者要能把不认识的条目叉掉，并且这个决定必须传到入库结果里，
 * 否则「导入分析结果」时又会把误判重新拉回对应模块。
 */
test('mis-detected entries in all four categories can be discarded, and the exclusions reach the ingest', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  // 人物：叉掉「雨夜」——它是天气，不是人
  await expect(page.getByTestId('analysis-tab-characters')).toContainText('人物（2）');
  await page.getByTestId('analysis-discard-characters-雨夜').click();
  await expect(page.getByTestId('analysis-tab-characters')).toContainText('人物（1）');
  await expect(page.getByTestId('analysis-discard-characters-雨夜')).toHaveCount(0);
  await expect(page.getByTestId('analysis-discard-notice-characters')).toContainText('已舍弃 1 项');

  // 误点了要能恢复，而且只恢复本类
  await page.getByTestId('analysis-restore-characters').click();
  await expect(page.getByTestId('analysis-tab-characters')).toContainText('人物（2）');
  await page.getByTestId('analysis-discard-characters-雨夜').click();

  // 地点：「雨里」是短语，不是地点
  await page.getByTestId('analysis-tab-locations').click();
  await expect(page.getByTestId('analysis-tab-locations')).toContainText('地点（2）');
  await page.getByTestId('analysis-discard-locations-雨里').click();
  await expect(page.getByTestId('analysis-tab-locations')).toContainText('地点（1）');
  await expect(page.getByTestId('analysis-tab-characters')).toContainText('人物（1）');

  // 时间线：「雨下了一整夜」是场景句，不是时间标记
  await page.getByTestId('analysis-tab-timeline').click();
  await expect(page.getByTestId('analysis-tab-timeline')).toContainText('时间线（2）');
  await page.getByTestId('analysis-discard-timeline-雨下了一整夜').click();
  await expect(page.getByTestId('analysis-tab-timeline')).toContainText('时间线（1）');

  // 伏笔：「雨」是意象，不是伏笔
  await page.getByTestId('analysis-tab-foreshadowing').click();
  await expect(page.getByTestId('analysis-tab-foreshadowing')).toContainText('伏笔（2）');
  await page.getByTestId('analysis-discard-foreshadowing-雨').click();
  await expect(page.getByTestId('analysis-tab-foreshadowing')).toContainText('伏笔（1）');

  await page.getByTestId('markdown-ingest-button').click();
  await expect(page.getByTestId('markdown-ingest-success')).toBeVisible();

  expect(state.ingestBodies).toHaveLength(1);
  const body = state.ingestBodies[0];
  expect(body).toContain('name="excludedAnalysis"');
  // 四类的舍弃值都要在请求里
  for (const value of ['雨夜', '雨里', '雨下了一整夜']) {
    expect(body).toContain(value);
  }
  expect(body).toContain('foreshadowing');
  // 保留的条目不该被当成舍弃名单带上去
  expect(body).not.toContain('半份手稿');
  expect(body).not.toContain('旧档案室');

  // 入库后再改列表就与库里不一致了，所以叉号应当禁用
  await page.getByTestId('analysis-tab-characters').click();
  await expect(page.getByTestId('analysis-discard-characters-林晚')).toBeDisabled();
  expect(state.pageErrors).toEqual([]);
});

// ── F. 写入方式：完全替换 / 增量合并 ─────────────────────
//
// 导入到已有项目此前只有一种行为：把当前项目全部章节移入回收站再写新内容。
// 作者只改了几章就重导整个文件夹时，导入文件里没有的章节同样会被清掉。
// 现在交给作者选，且选中「完全替换」时必须把后果写在按钮旁边。

test('an existing project is asked how to treat its chapters, and replacement is warned about', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  await expect(page.getByTestId('markdown-merge-mode')).toBeVisible();
  // 默认保持历史行为，但后果必须写在旁边（不能只靠一个看不见的默认值）
  await expect(page.getByTestId('markdown-merge-mode-replace')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('markdown-merge-mode-warning')).toContainText('全部章节移入回收站');
  // 合并的说明要点明对齐依据是**文件名里的章节号**，而不是标题或选择顺序
  await expect(page.getByTestId('markdown-merge-mode-merge')).toContainText('文件名里的章节号');
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

test('the chosen write mode reaches the backend and the result reports real counts', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('markdown-scan-success')).toBeVisible();

  await page.getByTestId('markdown-merge-mode-merge').click();
  // 选了合并就不再显示替换的警告，避免自相矛盾的提示
  await expect(page.getByTestId('markdown-merge-mode-warning')).toHaveCount(0);
  await page.getByTestId('markdown-ingest-button').click();

  // 合并模式下必须报「新增/替换/保留」，而不是听起来像整本替换的「已导入 N 章」
  await expect(page.getByTestId('markdown-ingest-success')).toContainText(
    '增量合并完成：新增 1 章、替换 1 章、保留 5 章。',
  );
  // 回收掉的旧编号残留要点名，作者才能去回收站核对
  await expect(page.getByTestId('markdown-ingest-success')).toContainText('顺带回收了 1 章旧编号残留');
  await expect(page.getByTestId('markdown-merged-cleanup')).toContainText('第 3 章 第四章 后窗');
  expect(fieldValue(state.ingestBodies[0], 'mergeMode')).toBe('merge');
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

test('replacement mode is the default that gets sent', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await page.getByTestId('markdown-ingest-button').click();

  await expect(page.getByTestId('markdown-ingest-success')).toContainText('已导入 2 章');
  expect(fieldValue(state.ingestBodies[0], 'mergeMode')).toBe('replace');
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});
