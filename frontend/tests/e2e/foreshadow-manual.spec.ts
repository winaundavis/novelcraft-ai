import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 手写伏笔登记（伏笔页顶部的小块）回归 —— 全离线，拦截所有 /api/**。
 *
 * 覆盖（产品方 2026-10-06 确认的四条设计）：
 * A. 结构化手写：填名称/说明 → 保存，章 id 与「回收章默认待定」符合约定；
 * B. Q2=C：贴自由文字让 AI 整理成草稿 → 作者核对后再保存（草稿本身不入库）；
 * C. AI 不可用时**如实说明**并让作者手填，不静默伪造；
 * D. Q4=1：顺便把一条已有伏笔改成新状态，并记下备注与所在章节。
 * E. 没有名称时不允许提交，且不发任何请求。
 */

const PID = 'foreshadow-manual-fixture';
const NOW = '2026-10-06T00:00:00Z';
const C1 = 'c1';
const C2 = 'c2';
const C3 = 'c3';

type DraftMode = { kind: 'ok' } | { kind: 'degraded' } | { kind: 'empty' };

function outline() {
  return {
    mainSummary: '测试大纲：少年寻剑。',
    structure: 'three-act',
    nodes: [
      { id: 'v1', type: 'volume', title: '第一卷', summary: '寻剑', order: 1 },
      { id: C1, type: 'chapter', parentId: 'v1', title: '第一章', summary: '开篇', order: 1 },
      { id: C2, type: 'chapter', parentId: 'v1', title: '第二章', summary: '入城', order: 2 },
      { id: C3, type: 'chapter', parentId: 'v1', title: '第三章', summary: '遇袭', order: 3 },
    ],
  };
}

function chapterRecord(id: string, order: number, title: string) {
  return {
    id, projectId: PID, volumeId: 'v1', title, order,
    content: `${title}正文`, wordCount: 6, status: 'completed', createdAt: NOW, updatedAt: NOW,
  };
}

function foreshadowRecord(over: Record<string, any> = {}) {
  return {
    id: 'fs-1', projectId: PID, name: '旧伏笔', description: '早就埋下的东西',
    plantChapterId: C1, targetChapterId: C2, status: 'planted', resolution: '', events: [],
    plantChapter: 1, targetChapter: 2, totalChapters: 3, overdue: false, overdueBy: 0,
    createdAt: NOW, updatedAt: NOW, ...over,
  };
}

async function installFixture(
  page: Page,
  options: { draft?: DraftMode; items?: Record<string, any>[] } = {},
) {
  const base = `/api/projects/${PID}`;
  const mode: DraftMode = options.draft || { kind: 'ok' };
  const state = {
    items: options.items ? [...options.items] : [],
    chapters: [chapterRecord(C1, 1, '第一章'), chapterRecord(C2, 2, '第二章'), chapterRecord(C3, 3, '第三章')],
    createBodies: [] as any[],
    draftBodies: [] as any[],
    patchBodies: [] as any[],
    unexpected: [] as string[],
    errors: [] as string[],
  };
  page.on('pageerror', (error) => state.errors.push(error.message));

  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const method = req.method();
    const path = url.pathname.replace(/\/$/, '');

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    // 手写登记：AI 整理草稿（不落库）
    if (path === `${base}/foreshadows/draft` && method === 'POST') {
      const body = req.postDataJSON();
      state.draftBodies.push(body);
      if (mode.kind === 'degraded') {
        return ok(route, { items: [], degraded: true, reason: 'AI 未配置或不可用（请到「模型设置」检查 provider 与密钥）。' });
      }
      if (mode.kind === 'empty') {
        return ok(route, { items: [], degraded: false, reason: 'AI 没从这段文字里认出长程伏笔，请手动填写。' });
      }
      return ok(route, {
        items: [{
          name: '剑鞘上的刻痕', description: '主角注意到剑鞘内侧刻着半个名字，没多想',
          plant_chapter: 1, target_chapter: 0, plant_chapter_id: C1, target_chapter_id: '',
        }],
        degraded: false,
        reason: '',
      });
    }

    if (path === `${base}/foreshadows` && method === 'POST') {
      const body = req.postDataJSON();
      state.createBodies.push(body);
      const created = (body.items || []).map((item: any, index: number) =>
        foreshadowRecord({
          id: `fs-new-${state.items.length + index + 1}`,
          name: item.name,
          description: item.description,
          plantChapterId: item.plantChapterId,
          targetChapterId: item.targetChapterId,
          plantChapter: item.plantChapterId === C1 ? 1 : 0,
          targetChapter: item.targetChapterId === C2 ? 2 : 0,
        }),
      );
      state.items.push(...created);
      return ok(route, created);
    }

    if (path.startsWith(`${base}/foreshadows/`) && method === 'PATCH') {
      const fid = path.slice(`${base}/foreshadows/`.length);
      const body = req.postDataJSON();
      state.patchBodies.push({ fid, body });
      const target = state.items.find((item) => item.id === fid);
      if (target) {
        if (body.status) target.status = body.status;
        if (body.note) {
          target.events = [...(target.events || []), { chapter_id: body.chapterId, note: body.note, status: target.status }];
        }
      }
      return ok(route, target || foreshadowRecord({ id: fid }));
    }

    if (path === `${base}/foreshadows` && method === 'GET') {
      return ok(route, {
        items: state.items,
        consistency: { passed: true, total: state.items.length, byType: {}, issues: [] },
        alertCount: 0,
      });
    }

    if (path === `${base}/fulltext-stats`) {
      return ok(route, { chars: 100, chapters: 3, estimatedTokens: 100, estimatedCostUsd: null });
    }
    if (path === `${base}/chapters` && method === 'GET') return ok(route, state.chapters);
    if (path.startsWith(`${base}/chapters/`)) {
      const chapterId = path.slice(`${base}/chapters/`.length);
      if (chapterId.endsWith('/projections')) return ok(route, []);
      const target = state.chapters.find((item) => item.id === chapterId) || state.chapters[0];
      return ok(route, target);
    }

    if (method === 'GET') {
      if (path === base) {
        return ok(route, {
          id: PID, name: '手写伏笔回归项目', subtitle: '', genre: 'xuanhuan', status: 'drafting',
          premise: '少年寻剑。', currentWordCount: 18, targetWordCount: 100000, targetChapters: 3,
          outline: outline(), createdAt: NOW, updatedAt: NOW,
        });
      }
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) return ok(route, { styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {} });
      if (path.endsWith('/story-state')) return ok(route, { projectId: PID, version: 1 });
      if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
      if (path === `/api/ai/project/${PID}`) return ok(route, null);
      if (path === '/api/projects' || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) return ok(route, []);
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${PID}`);
  await page.getByTestId('workspace-tab-foreshadow').click();
  await expect(page.getByTestId('foreshadow-manual-entry')).toBeVisible();
  return state;
}

test('A: 手写一条伏笔（埋设章默认当前章、回收章默认待定）并保存进账本', async ({ page }) => {
  const state = await installFixture(page);

  await expect(page.getByTestId('foreshadow-manual-plant-0')).toHaveValue(C1); // 默认 = 当前打开的章
  await expect(page.getByTestId('foreshadow-manual-target-0')).toHaveValue(''); // 默认待定

  await page.getByTestId('foreshadow-manual-name-0').fill('剑鞘上的刻痕');
  await page.getByTestId('foreshadow-manual-description-0').fill('主角看到半个名字，没多想');
  await page.getByTestId('foreshadow-manual-add-row').click();
  await page.getByTestId('foreshadow-manual-name-1').fill('无名老人的一句叮嘱');
  await page.getByTestId('foreshadow-manual-target-1').selectOption(C3);

  await page.getByTestId('foreshadow-manual-save').click();

  await expect(page.getByTestId('foreshadow-manual-success')).toContainText('已登记 2 条伏笔');
  expect(state.createBodies).toHaveLength(1);
  expect(state.createBodies[0].items).toEqual([
    { name: '剑鞘上的刻痕', description: '主角看到半个名字，没多想', plantChapterId: C1, targetChapterId: '' },
    { name: '无名老人的一句叮嘱', description: '', plantChapterId: C1, targetChapterId: C3 },
  ]);
  // 列表刷新后能看到刚登记的两条
  await expect(page.getByTestId('foreshadow-list')).toContainText('剑鞘上的刻痕');
  await expect(page.getByTestId('foreshadow-list')).toContainText('无名老人的一句叮嘱');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('B: 贴一段自由文字让 AI 整理成草稿，作者核对后再保存', async ({ page }) => {
  const state = await installFixture(page, { draft: { kind: 'ok' } });

  await page.getByTestId('foreshadow-manual-text').fill('本章主角注意到剑鞘内侧刻着半个名字，他没多想。');
  await page.getByTestId('foreshadow-manual-draft').click();

  // 草稿填进了表单：名称/说明来自 AI，回收章仍是待定
  await expect(page.getByTestId('foreshadow-manual-name-0')).toHaveValue('剑鞘上的刻痕');
  await expect(page.getByTestId('foreshadow-manual-description-0')).toHaveValue(/半个名字/);
  await expect(page.getByTestId('foreshadow-manual-target-0')).toHaveValue('');
  await expect(page.getByTestId('foreshadow-manual-draft-reason')).toContainText('请核对后再保存');
  // 草稿**没有**入库
  expect(state.createBodies).toHaveLength(0);
  // 整理请求带上了「当前章」，方便 AI 判断埋设章
  expect(state.draftBodies).toEqual([{ text: '本章主角注意到剑鞘内侧刻着半个名字，他没多想。', plantChapterId: C1 }]);

  await page.getByTestId('foreshadow-manual-save').click();
  await expect(page.getByTestId('foreshadow-manual-success')).toContainText('已登记 1 条伏笔');
  expect(state.createBodies[0].items[0].name).toBe('剑鞘上的刻痕');
  expect(state.unexpected).toEqual([]);
});

test('C: AI 不可用时如实说明原因，且不伪造成果', async ({ page }) => {
  const state = await installFixture(page, { draft: { kind: 'degraded' } });

  await page.getByTestId('foreshadow-manual-text').fill('我埋了一条线');
  await page.getByTestId('foreshadow-manual-draft').click();

  const reason = page.getByTestId('foreshadow-manual-draft-reason');
  await expect(reason).toContainText('模型设置');
  // 没有草稿行被填进来（只有最初那一行空表单），也没有入库
  await expect(page.getByTestId('foreshadow-manual-name-1')).toHaveCount(0);
  await expect(page.getByTestId('foreshadow-manual-name-0')).toHaveValue('');
  expect(state.createBodies).toHaveLength(0);

  // 作者仍可手写保存
  await page.getByTestId('foreshadow-manual-name-0').fill('手动填的伏笔');
  await page.getByTestId('foreshadow-manual-save').click();
  await expect(page.getByTestId('foreshadow-manual-success')).toContainText('已登记 1 条伏笔');
  expect(state.unexpected).toEqual([]);
});

test('D: 顺便把一条已有伏笔标记为已回收，并记下备注与所在章节', async ({ page }) => {
  const state = await installFixture(page, { items: [foreshadowRecord()] });

  await page.getByTestId('foreshadow-manual-existing').selectOption('fs-1');
  await page.getByTestId('foreshadow-manual-existing-status').selectOption('resolved');
  await page.getByTestId('foreshadow-manual-note').fill('本章揭晓了刻痕的来历');
  await page.getByTestId('foreshadow-manual-name-0').fill('刻痕的来历');

  await page.getByTestId('foreshadow-manual-save').click();

  await expect(page.getByTestId('foreshadow-manual-success')).toContainText('已标记为「已回收」');
  expect(state.patchBodies).toEqual([{
    fid: 'fs-1',
    body: { status: 'resolved', note: '本章揭晓了刻痕的来历', chapterId: C1 },
  }]);
  // 列表里的状态跟着更新
  await expect(page.getByTestId('foreshadow-status-fs-1')).toContainText('已回收');
  expect(state.unexpected).toEqual([]);
});

test('E: 没有填写名称时拒绝提交，且不发任何请求', async ({ page }) => {
  const state = await installFixture(page, { items: [foreshadowRecord()] });

  await page.getByTestId('foreshadow-manual-save').click();

  await expect(page.getByTestId('foreshadow-manual-error')).toContainText('至少填一条伏笔名称');
  expect(state.createBodies).toHaveLength(0);
  expect(state.patchBodies).toHaveLength(0);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});
