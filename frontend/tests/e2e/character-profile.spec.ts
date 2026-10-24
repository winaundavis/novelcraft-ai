import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 角色档案（基础信息 / 性格倾向 / 背景与驱动力 / 写作参考）回归。
 *
 * 全离线：拦截所有 /api/**。需求：
 * - 去掉 MBTI 与五维打分，改为可手写 + 可由 AI 通读「正文 + 大纲」生成；
 * - AI 结果**绝不自动覆盖**作者内容，由作者逐项「采用」；
 * - 采用后仍需点「保存」才落盘。
 */

const PID = 'profile-fixture';
const NOW = '2026-09-16T00:00:00Z';

const CHAR_A = 'char-a';
const CHAR_B = 'char-b';
const NAME_A = '林晚';
const NAME_B = '陈默';

const HAND_PERSONALITY = '她表面上随和，其实非常固执，认定的事不会回头。';
const AI_PERSONALITY = '她习惯先观察再开口，压力越大越安静，但对熟人会露出意外的固执。';
const AI_ARC = '从只想查清旧案，转成不得不面对自己的过去。';
const AI_AGE = 31;

type AiMode =
  | { kind: 'ok'; delayMs?: number }
  | { kind: 'degraded'; reason: string }
  | { kind: 'status'; status: number; detail: string };

function character(id: string, name: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    projectId: PID,
    name,
    aliases: [],
    role: 'protagonist',
    basicInfo: { age: 24, gender: '女', appearance: '', identity: '调查员', occupation: '' },
    personality: { description: '' },
    background: '',
    coreTrauma: '',
    surfaceMotivation: '',
    deepDesire: '',
    arc: '',
    catchphrase: '',
    speechStyle: '',
    habits: '',
    values: '',
    relations: [],
    ...overrides,
  };
}

function projectPayload(characters: ReturnType<typeof character>[]) {
  return {
    id: PID,
    name: '角色档案回归项目',
    subtitle: '',
    genre: 'mystery',
    status: 'drafting',
    premise: '她在旧档案里发现了自己的名字。',
    currentWordCount: 0,
    targetWordCount: 10000,
    targetChapters: 3,
    characters,
    locations: [],
    worldRules: [],
    outline: {
      mainSummary: '林晚追查旧案，最终发现线索指向自己。',
      premise: '一个不肯回头的调查者。',
      structure: 'three-act',
      nodes: [
        { id: 'v1', type: 'volume', title: '第一卷', summary: '入局', order: 1 },
        { id: 'c1', type: 'chapter', parentId: 'v1', title: '第一章 雨夜', summary: '林晚接到匿名信', order: 1 },
      ],
    },
    createdAt: NOW,
    updatedAt: NOW,
  };
}

async function installFixture(page: Page, mode: AiMode, characters?: ReturnType<typeof character>[]) {
  const state = {
    characters: characters ?? [character(CHAR_A, NAME_A), character(CHAR_B, NAME_B)],
    analyzeCalls: [] as { characterName?: string }[],
    patches: [] as Record<string, any>[],
    writes: [] as string[],
    unexpected: [] as string[],
    pageErrors: [] as string[],
  };
  page.on('pageerror', (error) => state.pageErrors.push(error.message));

  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });
  const base = `/api/projects/${PID}`;

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/\/$/, '');
    const method = req.method();

    const isAdvisoryAnalysis = path.endsWith('/analyze-character-profile') || path.endsWith('/analyze-characters');
    if (method !== 'GET' && !isAdvisoryAnalysis) state.writes.push(`${method} ${path}`);

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    if (path === `${base}/analyze-character-profile` && method === 'POST') {
      const body = req.postDataJSON() as { characterName?: string };
      state.analyzeCalls.push(body);
      if (mode.kind === 'status') return route.fulfill({ status: mode.status, json: { detail: mode.detail } });
      if (mode.kind === 'degraded') {
        return ok(route, {
          suggestions: {}, rejected: {}, omitted: [], inferred: [],
          degraded: true, reason: mode.reason, basis: null,
        });
      }
      if (mode.delayMs) await new Promise((resolve) => setTimeout(resolve, mode.delayMs));
      return ok(route, {
        suggestions: {
          personality: AI_PERSONALITY,
          arc: AI_ARC,
          age: AI_AGE,
          values: '开放度：7，秩序与责任。',
        },
        rejected: { gender: '只能是 男 / 女 / 未知' },
        omitted: ['catchphrase', 'habits'],
        inferred: ['age'],
        degraded: false,
        reason: null,
        basis: { chapterCount: 3, sampledChars: 4200, hasOutline: true },
      });
    }

    if (path === base && method === 'PATCH') {
      const body = req.postDataJSON() as Record<string, any>;
      state.patches.push(body);
      if (Array.isArray(body.characters)) state.characters = body.characters;
      return ok(route, projectPayload(state.characters));
    }

    if (method === 'GET') {
      if (path === base) return ok(route, projectPayload(state.characters));
      if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
      if (path.endsWith('/chapters')) return ok(route, []);
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) return ok(route, {
        styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {},
      });
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
  await page.getByTestId('workspace-tab-characters').click();
  await expect(page.getByTestId('character-edit-button')).toBeVisible();
  return state;
}

function savedCharacter(state: { patches: Record<string, any>[] }, id: string): Record<string, any> | undefined {
  for (let index = state.patches.length - 1; index >= 0; index -= 1) {
    const characters = state.patches[index].characters;
    if (!Array.isArray(characters)) continue;
    const target = characters.find((item: Record<string, any>) => item.id === id);
    if (target) return target;
  }
  return undefined;
}

// ── A. 手写 ───────────────────────────────────────────────

test('the author can hand-write every section and it persists', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  // 只读态：四个区块都在，空值显示「未填写」
  await expect(page.getByTestId('profile-value-personality')).toHaveText('未填写');
  await expect(page.getByTestId('profile-value-arc')).toHaveText('未填写');

  await page.getByTestId('character-edit-button').click();

  await page.getByTestId('profile-field-personality').fill(HAND_PERSONALITY);
  await page.getByTestId('profile-field-age').fill('29');
  await page.getByTestId('profile-field-appearance').fill('黑色长发，左脸有一道旧疤');
  await page.getByTestId('profile-field-background').fill('父亲在旧案里失踪，她从此留在档案科。');
  await page.getByTestId('profile-field-arc').fill('从只想查清旧案，转成面对自己的过去。');
  await page.getByTestId('profile-field-values').fill('秩序、真相，以及不再替别人隐瞒。');
  await expect(page.getByTestId('profile-count-personality')).toContainText(`${HAND_PERSONALITY.length}/200`);

  await page.getByTestId('character-save').click();

  // 回到只读态，内容落到了对应的区块
  await expect(page.getByTestId('profile-value-personality')).toHaveText(HAND_PERSONALITY);
  await expect(page.getByTestId('profile-value-age')).toHaveText('29');
  await expect(page.getByTestId('profile-value-values')).toHaveText('秩序、真相，以及不再替别人隐瞒。');

  const saved = savedCharacter(state, CHAR_A)!;
  expect(saved.personality.description).toBe(HAND_PERSONALITY);
  expect(saved.basicInfo.age).toBe(29);
  expect(saved.arc).toBe('从只想查清旧案，转成面对自己的过去。');
  expect(state.analyzeCalls).toHaveLength(0);
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

test('a saved archive is shown read-only on reload', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' }, [
    character(CHAR_A, NAME_A, { personality: { description: HAND_PERSONALITY }, arc: '已有的弧光。' }),
    character(CHAR_B, NAME_B),
  ]);

  await page.reload();
  await page.getByTestId('workspace-tab-characters').click();

  await expect(page.getByTestId('profile-value-personality')).toHaveText(HAND_PERSONALITY);
  await expect(page.getByTestId('profile-value-arc')).toHaveText('已有的弧光。');
  expect(state.pageErrors).toEqual([]);
});

// ── B. AI 建议不覆盖草稿 ──────────────────────────────────

test('AI suggestions appear per field and never overwrite the draft on their own', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('profile-field-personality').fill(HAND_PERSONALITY);

  await page.getByTestId('character-ai-analyze').click();

  // 逐字段建议就地出现
  await expect(page.getByTestId('profile-suggestion-personality-text')).toHaveText(AI_PERSONALITY);
  await expect(page.getByTestId('profile-suggestion-arc-text')).toHaveText(AI_ARC);
  await expect(page.getByTestId('profile-suggestion-age-text')).toHaveText(String(AI_AGE));

  // 依据可追溯 + 推断标记 + 透明的省略/拒绝说明
  await expect(page.getByTestId('character-ai-summary')).toContainText('3 章正文');
  await expect(page.getByTestId('character-ai-summary')).toContainText('含大纲');
  await expect(page.getByTestId('character-ai-rejected')).toContainText('性别');
  await expect(page.getByTestId('character-ai-omitted')).toContainText('口头禅');
  expect(state.analyzeCalls[0]?.characterName).toBe(NAME_A);

  // 关键：草稿一字未动
  await expect(page.getByTestId('profile-field-personality')).toHaveValue(HAND_PERSONALITY);
  await expect(page.getByTestId('profile-field-arc')).toHaveValue('');
  expect(state.patches).toHaveLength(0);

  // 「忽略」也只收起建议，不动草稿
  await page.getByTestId('profile-suggestion-personality-ignore').click();
  await expect(page.getByTestId('profile-suggestion-personality')).toHaveCount(0);
  await expect(page.getByTestId('profile-field-personality')).toHaveValue(HAND_PERSONALITY);

  await page.getByTestId('character-ai-dismiss-all').click();
  await expect(page.getByTestId('character-ai-summary')).toHaveCount(0);
  await expect(page.getByTestId('profile-field-personality')).toHaveValue(HAND_PERSONALITY);
  expect(state.patches).toHaveLength(0);
});

// ── C. 采用 ───────────────────────────────────────────────

test('accepting a suggestion fills the draft but still needs an explicit save', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('profile-field-personality').fill(HAND_PERSONALITY);

  await page.getByTestId('character-ai-analyze').click();
  await expect(page.getByTestId('profile-suggestion-personality-text')).toBeVisible();

  await page.getByTestId('profile-suggestion-personality-accept').click();

  // 采用后填入草稿，但还没落盘
  await expect(page.getByTestId('profile-field-personality')).toHaveValue(AI_PERSONALITY);
  await expect(page.getByTestId('profile-suggestion-personality')).toHaveCount(0);
  expect(state.patches).toHaveLength(0);

  await page.getByTestId('character-ai-accept-all').click();
  await expect(page.getByTestId('profile-field-arc')).toHaveValue(AI_ARC);
  await expect(page.getByTestId('profile-field-age')).toHaveValue(String(AI_AGE));
  await expect(page.getByTestId('character-ai-summary')).toHaveCount(0);
  expect(state.patches).toHaveLength(0);

  await page.getByTestId('character-save').click();
  await expect(page.getByTestId('profile-value-personality')).toHaveText(AI_PERSONALITY);

  const saved = savedCharacter(state, CHAR_A)!;
  expect(saved.personality.description).toBe(AI_PERSONALITY);
  expect(saved.arc).toBe(AI_ARC);
  expect(saved.basicInfo.age).toBe(AI_AGE);
  // 被拒绝的性别字段不得被写坏
  expect(saved.basicInfo.gender).toBe('女');
});

test('age suggestions are reduced to digits', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('character-ai-analyze').click();
  await expect(page.getByTestId('profile-suggestion-age-text')).toBeVisible();

  await page.getByTestId('profile-suggestion-age-accept').click();
  await expect(page.getByTestId('profile-field-age')).toHaveValue('31');

  await page.getByTestId('character-save').click();
  expect(savedCharacter(state, CHAR_A)!.basicInfo.age).toBe(31);
});

// ── D. 降级与限流 ─────────────────────────────────────────

test('a degraded analysis explains why and leaves every draft field untouched', async ({ page }) => {
  const state = await installFixture(page, {
    kind: 'degraded',
    reason: 'AI 返回的内容不符合格式要求（疑似出现了 MBTI 或打分内容），请重试。',
  });

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('profile-field-personality').fill(HAND_PERSONALITY);

  await page.getByTestId('character-ai-analyze').click();

  await expect(page.getByTestId('character-ai-error')).toContainText('MBTI');
  await expect(page.getByTestId('character-ai-summary')).toHaveCount(0);
  await expect(page.getByTestId('profile-field-personality')).toHaveValue(HAND_PERSONALITY);

  await page.getByTestId('character-save').click();
  expect(savedCharacter(state, CHAR_A)!.personality.description).toBe(HAND_PERSONALITY);
});

test('a rate-limited analysis blames the quota instead of the model', async ({ page }) => {
  const state = await installFixture(page, { kind: 'status', status: 429, detail: '请求过于频繁，请稍后再试。' });

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('character-ai-analyze').click();

  const error = page.getByTestId('character-ai-error');
  await expect(error).toBeVisible();
  await expect(error).toContainText('请求过于频繁');
  await expect(error).toContainText('与模型无关');
  expect(state.unexpected).toEqual([]);
});

// ── E. 切换角色：不串数据、不丢手写 ───────────────────────

test('switching characters preserves the unsaved draft and keeps AI results per character', async ({ page }) => {
  test.setTimeout(60_000);
  const state = await installFixture(page, { kind: 'ok', delayMs: 3_000 });

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('profile-field-personality').fill(HAND_PERSONALITY);
  await page.getByTestId('character-ai-analyze').click();
  await expect(page.getByTestId('character-ai-status')).toBeVisible();

  // 分析还没回来就切到另一个角色：B 必须是干净的只读态
  await page.getByTestId(`character-card-${CHAR_B}`).click();
  await expect(page.getByTestId('profile-field-personality')).toHaveCount(0);
  await expect(page.getByTestId('profile-value-personality')).toHaveText('未填写');

  // 切回 A：草稿与建议都还在
  await page.getByTestId(`character-card-${CHAR_A}`).click();
  await expect(page.getByTestId('profile-field-personality')).toHaveValue(HAND_PERSONALITY);
  await expect(page.getByTestId('profile-suggestion-personality-text')).toHaveText(AI_PERSONALITY);

  // B 始终没有被写入
  await page.getByTestId(`character-card-${CHAR_B}`).click();
  await expect(page.getByTestId('profile-value-personality')).toHaveText('未填写');
  expect(state.patches).toHaveLength(0);
  expect(state.pageErrors).toEqual([]);
});

test('cancelling discards the unsaved draft only for the current character', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('profile-field-personality').fill(HAND_PERSONALITY);
  await page.getByTestId('character-cancel').click();

  await expect(page.getByTestId('profile-value-personality')).toHaveText('未填写');
  expect(state.patches).toHaveLength(0);
  expect(state.writes).toEqual([]);
});

// ── F. 旧数据 ─────────────────────────────────────────────

test('legacy five-factor + MBTI data renders as plain text and saves in the new shape', async ({ page }) => {
  const legacy = {
    ...character(CHAR_A, NAME_A),
    personality: { openness: 8, conscientiousness: 7, extraversion: 3, agreeableness: 5, neuroticism: 6, mbti: 'INTJ' },
  } as Record<string, any>;
  const state = await installFixture(page, { kind: 'ok' }, [legacy, character(CHAR_B, NAME_B)]);

  // 旧分数/MBTI 不再展示，退化为「未填写」
  await expect(page.getByTestId('profile-value-personality')).toHaveText('未填写');
  await expect(page.locator('body')).not.toContainText('INTJ');

  await page.getByTestId('character-edit-button').click();
  await page.getByTestId('profile-field-personality').fill('她话少但记性极好，被逼急时会突然强硬。');
  await page.getByTestId('character-save').click();

  const saved = savedCharacter(state, CHAR_A)!;
  expect(saved.personality).toEqual({ description: '她话少但记性极好，被逼急时会突然强硬。' });
  expect(JSON.stringify(saved.personality)).not.toMatch(/mbti|openness|INTJ/i);
  expect(state.pageErrors).toEqual([]);
});

test('人物关系图不会因父组件重渲染而被销毁重建（回调必须是稳定的）', async ({ page }) => {
  // 钉住全仓 hook 审计发现的一处缺陷：CharacterRelationGraph 的建图 effect 曾经依赖
  // `onSelectCharacter`，而 CharacterPanel 传的是内联箭头函数（每次渲染都是新身份）。
  // 后果是父组件每次重渲染都会 cy.destroy() 再新建一个 Cytoscape 实例
  // （含 cose 布局 800 次迭代），连"点一下节点切换选中"都会触发重建 ——
  // 正好抵消了那段代码注释里"避免点击重建图"的本意。
  const state = await installFixture(page, { kind: 'ok' });

  const graph = page.getByTestId('character-relation-graph');
  await expect(graph).toBeVisible();

  // Cytoscape 会往容器里插 <canvas>；给它打标记，用来判断图有没有被销毁重建。
  const marked = await page.evaluate(() => {
    const canvas = document.querySelector('[data-testid="character-relation-graph"] canvas');
    if (!canvas) return false;
    canvas.setAttribute('data-probe-mark', 'kept');
    return true;
  });
  expect(marked, '关系图应当渲染出 canvas').toBe(true);

  // 切换选中的角色：父组件会重渲染，但 characters 数组本身没变
  await page.getByTestId(`character-card-${CHAR_B}`).click();
  await expect(page.getByTestId(`character-card-${CHAR_B}`)).toBeVisible();

  // 图必须**原样还在**：标记还在说明 canvas 没被替换、建图 effect 没有重跑
  const stillMarked = await page.evaluate(
    () => document.querySelector('[data-testid="character-relation-graph"] canvas[data-probe-mark="kept"]') !== null,
  );
  expect(stillMarked, '切换选中不该重建关系图').toBe(true);
  expect(state.pageErrors).toEqual([]);
});

test('手机端 12 个 tab 收进菜单，桌面端保持横滑条不变', async ({ page }) => {
  // 需求：手机上不再横滑 12 个 tab，改成"当前视图 + 菜单"。
  // 硬约束：**桌面端必须一像素不变** —— 所以这条同时钉两边。
  const state = await installFixture(page, { kind: 'ok' });

  // 桌面（默认 1440）：横滑条可见、菜单按钮不出现
  await expect(page.getByTestId('workspace-tab-write')).toBeVisible();
  await expect(page.getByTestId('workspace-tab-stats')).toBeVisible();
  await expect(page.getByTestId('open-tab-menu')).toBeHidden();

  // 切到手机宽度：横滑条隐藏、菜单按钮出现并显示当前视图（此刻是「人物」）
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId('workspace-tab-write')).toBeHidden();
  const menuButton = page.getByTestId('open-tab-menu');
  await expect(menuButton).toBeVisible();
  await expect(menuButton).toContainText('人物');

  // 菜单里必须是**全部 12 个** tab（5 主 + 7 工具），不能只放常用的
  await menuButton.click();
  await expect(page.getByTestId('tab-menu-dialog')).toBeVisible();
  for (const id of [
    'outline', 'write', 'characters', 'chat', 'inspiration',
    'review', 'import', 'world', 'reference', 'tasks', 'foreshadow', 'stats',
  ]) {
    await expect(page.getByTestId(`tab-menu-item-${id}`)).toBeVisible();
  }

  // 选一个工具 tab：菜单关闭、按钮文案跟着变、该视图真的渲染出来
  await page.getByTestId('tab-menu-item-stats').click();
  await expect(page.getByTestId('tab-menu-dialog')).toBeHidden();
  await expect(page.getByTestId('open-tab-menu')).toContainText('统计');
  await expect(page.getByTestId('workspace-root')).toBeVisible();
  expect(state.pageErrors).toEqual([]);
});
