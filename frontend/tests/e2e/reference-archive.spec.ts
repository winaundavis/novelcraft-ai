import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 参考书档案（「参考分析」页）回归 —— 全离线，拦截所有 /api/**。
 *
 * 覆盖：
 * A. 建档：粘贴文本 + 名称 → 落库进列表，请求体正确（含来源备注）；
 * B. AI 不可用（degraded）：如实显示原因，档案仍可用（规则来自本地统计）；
 * C. 报告：本地实测指标 + 技法规则 + 采样说明；**页面不得再出现「你的基线 / 风格量化对比」**
 *    —— 那一栏是旧的造假数字，本 spec 是防止它复活的绊线；
 * D. 上传 .txt：读进文本框并自动用文件名当档案名；
 * E. 改名 / 重新拆解 / 删除（带二次确认）；
 * F. 列表**不请求原文**，点「查看报告」才拉详情（原文可能几十万字）。
 */

const PID = 'reference-archive-fixture';
const NOW = '2026-10-06T00:00:00Z';
const C1 = 'c1';
const RAW = '第一章 夜雨\n雨敲在瓦上。阿七把刀放在膝上，听楼下脚步。\n“把东西给我。”来人开口。';
const RAW_STRIPPED = RAW.trim();

const METRICS = {
  avgSentenceLength: 12.5,
  dialogueRatio: 0.24,
  descriptionDensity: 0.61,
  paragraphShortRatio: 0.5,
  totalParagraphs: 42,
};

function record(over: Record<string, any> = {}) {
  return {
    id: 'rw-1',
    project_id: PID,
    name: '夜雨',
    source_note: '某短篇集第 3 篇',
    raw_text: RAW_STRIPPED,
    char_count: 18000,
    stored_chars: 18000,
    truncated: 0,
    sample_chars: 18000,
    sample_count: 1,
    status: 'completed',
    status_reason: '',
    metrics: METRICS,
    dimensions: {},
    narrative_rules: [{ rule: '用具体动作推进场景', evidence: '把刀放在膝上' }],
    character_rules: [],
    language_rules: [{ rule: '短句为主', evidence: '桥断了。' }],
    rhythm_rules: [],
    anti_ai_rules: [{ rule: '不解释情绪', evidence: '' }],
    strengths: ['动作干净'],
    weaknesses: ['信息密度偏低'],
    report: '整体是克制的动作流叙事。',
    llm_used: 1,
    tags: [],
    created_at: NOW,
    updated_at: NOW,
    ...over,
  };
}

async function installFixture(
  page: Page,
  options: {
    items?: Record<string, any>[];
    degraded?: boolean;
    activeId?: string;
    /** 预置启用集合（多份 + 强度） */
    activeItems?: Array<{ id: string; weight?: number; name?: string }>;
  } = {},
) {
  const base = `/api/projects/${PID}`;
  const state = {
    items: options.items ? [...options.items] : [],
    activeItems: (options.activeItems || (options.activeId ? [{ id: options.activeId }] : [])).map((entry) => ({
      id: entry.id,
      weight: entry.weight ?? 6,
      name: entry.name || entry.id,
    })),
    createBodies: [] as any[],
    patchBodies: [] as any[],
    activeBodies: [] as any[],
    detailCalls: [] as string[],
    reanalyzeCalls: 0,
    deletes: [] as string[],
    unexpected: [] as string[],
    errors: [] as string[],
  };
  const sortedActive = () => [...state.activeItems].sort((a, b) => b.weight - a.weight);
  page.on('pageerror', (error) => state.errors.push(error.message));

  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));
  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/\/$/, '');
    const method = req.method();

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    if (path === `${base}/reference-works` && method === 'GET') {
      return ok(route, {
        items: state.items,
        activeReferences: sortedActive().map((entry) => ({
          id: entry.id,
          weight: entry.weight,
          name: state.items.find((item) => item.id === entry.id)?.name || entry.name,
        })),
        activeReferenceId: sortedActive()[0]?.id || '',
      });
    }

    // 启用/停用「创作参考」（必须放在 {reference_id} 分支之前，否则 active 会被当成档案 id）
    if (path === `${base}/reference-works/active` && method === 'PUT') {
      const body = req.postDataJSON();
      state.activeBodies.push(body);
      // 后端语义：整体替换；空数组 = 全部停用
      const incoming = Array.isArray(body.items) ? body.items : body.referenceId ? [{ referenceId: body.referenceId }] : [];
      state.activeItems = incoming
        .filter((entry: any) => entry?.referenceId)
        .map((entry: any) => ({
          id: String(entry.referenceId),
          weight: Number(entry.weight) || 6,
          name: state.items.find((item) => item.id === entry.referenceId)?.name || String(entry.referenceId),
        }));
      return ok(route, {
        activeReferences: sortedActive(),
        activeReferenceId: sortedActive()[0]?.id || '',
      });
    }

    if (path === `${base}/reference-works` && method === 'POST') {
      const body = req.postDataJSON();
      state.createBodies.push(body);
      const created = record(
        options.degraded
          ? {
              id: 'rw-degraded',
              name: body.name || '第一章 夜雨',
              source_note: body.sourceNote || '',
              tags: body.tags || [],
              raw_text: String(body.text || '').trim(),
              status: 'degraded',
              status_reason: 'AI 未配置或不可用（请到「模型设置」检查 provider 与密钥），已按本地统计生成规则。',
              llm_used: 0,
              narrative_rules: [],
              language_rules: [{ rule: '句子短促，动作密集', evidence: '' }],
            }
          : {
              name: body.name || '第一章 夜雨',
              source_note: body.sourceNote || '',
              tags: body.tags || [],
              raw_text: String(body.text || '').trim(),
            },
      );
      state.items = [created, ...state.items];
      return ok(route, created);
    }

    if (path.startsWith(`${base}/reference-works/`)) {
      const rest = path.slice(`${base}/reference-works/`.length);
      const [rid, action] = rest.split('/');

      if (action === 'reanalyze' && method === 'POST') {
        state.reanalyzeCalls += 1;
        const target = state.items.find((item) => item.id === rid);
        if (!target) return route.fulfill({ status: 404, json: { detail: '参考档案未找到。' } });
        target.report = '第二次拆解：节奏更克制。';
        target.status = 'completed';
        target.llm_used = 1;
        return ok(route, target);
      }

      if (method === 'GET') {
        state.detailCalls.push(rid);
        const target = state.items.find((item) => item.id === rid);
        return target ? ok(route, target) : route.fulfill({ status: 404, json: { detail: '参考档案未找到。' } });
      }
      if (method === 'PATCH') {
        const body = req.postDataJSON();
        state.patchBodies.push({ rid, body });
        const target = state.items.find((item) => item.id === rid);
        if (!target) return route.fulfill({ status: 404, json: { detail: '参考档案未找到。' } });
        if (body.name !== undefined) target.name = body.name;
        if (body.sourceNote !== undefined) target.source_note = body.sourceNote;
        if (body.tags !== undefined) target.tags = body.tags;
        return ok(route, target);
      }
      if (method === 'DELETE') {
        state.deletes.push(rid);
        state.items = state.items.filter((item) => item.id !== rid);
        state.activeItems = state.activeItems.filter((entry) => entry.id !== rid);
        return ok(route, null);
      }
    }

    if (path === `${base}/chapters` && method === 'GET') {
      return ok(route, [
        {
          id: C1, projectId: PID, volumeId: 'v1', title: '第一章', order: 1,
          content: '正文', wordCount: 2, status: 'completed', createdAt: NOW, updatedAt: NOW,
        },
      ]);
    }
    if (path.startsWith(`${base}/chapters/`)) {
      const chapterId = path.slice(`${base}/chapters/`.length);
      if (chapterId.endsWith('/projections')) return ok(route, []);
      return ok(route, {
        id: C1, projectId: PID, volumeId: 'v1', title: '第一章', order: 1,
        content: '正文', wordCount: 2, status: 'completed', createdAt: NOW, updatedAt: NOW,
      });
    }

    if (method === 'GET') {
      if (path === base) {
        return ok(route, {
          id: PID, name: '参考档案回归项目', subtitle: '', genre: 'xuanhuan', status: 'drafting',
          premise: '测试。', currentWordCount: 2, targetWordCount: 100000, targetChapters: 1,
          outline: {
            mainSummary: '测试。',
            structure: 'three-act',
            nodes: [
              { id: 'v1', type: 'volume', title: '第一卷', summary: '', order: 1 },
              { id: C1, type: 'chapter', parentId: 'v1', title: '第一章', summary: '', order: 1 },
            ],
          },
          createdAt: NOW, updatedAt: NOW,
        });
      }
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) return ok(route, { styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {} });
      if (path.endsWith('/story-state')) return ok(route, { projectId: PID, version: 1 });
      if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
      if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
      if (path === `/api/ai/project/${PID}`) return ok(route, null);
      if (path === '/api/projects' || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) return ok(route, []);
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${PID}`);
  await page.getByTestId('workspace-tab-reference').click();
  await expect(page.getByTestId('reference-create')).toBeVisible();
  return state;
}

async function archive(page: Page, name = '夜雨') {
  await page.getByTestId('reference-name-input').fill(name);
  await page.getByTestId('reference-source-input').fill('某短篇集第 3 篇');
  await page.getByTestId('reference-text-input').fill(RAW);
  await page.getByTestId('reference-create-button').click();
}

test('A: 粘贴文本建档 → 落进列表，请求体带上名称与来源备注', async ({ page }) => {
  const state = await installFixture(page);

  await expect(page.getByTestId('reference-empty')).toBeVisible();
  await archive(page);

  await expect(page.getByTestId('reference-item-rw-1')).toBeVisible();
  await expect(page.getByTestId('reference-item-rw-1')).toContainText('夜雨');
  await expect(page.getByTestId('reference-status-rw-1')).toContainText('AI 已拆解');
  expect(state.createBodies).toEqual([
    { name: '夜雨', text: RAW_STRIPPED, sourceNote: '某短篇集第 3 篇', tags: [] },
  ]);
  // 建档后自动展开报告
  await expect(page.getByTestId('reference-report')).toBeVisible();
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('B: AI 不可用时如实说明原因，档案仍可用（规则来自本地统计）', async ({ page }) => {
  const state = await installFixture(page, { degraded: true });

  await archive(page);

  await expect(page.getByTestId('reference-status-rw-degraded')).toContainText('统计降级');
  await expect(page.getByTestId('reference-reason-rw-degraded')).toContainText('模型设置');
  // 降级档案依然有可用的本地统计规则
  await expect(page.getByTestId('reference-rules-语言习惯')).toContainText('句子短促');
  expect(state.unexpected).toEqual([]);
});

test('C: 报告只展示本地实测指标，绝不出现旧的「你的基线 / 风格量化对比」', async ({ page }) => {
  const state = await installFixture(page, { items: [record()] });

  await page.getByTestId('reference-view-rw-1').click();

  const metrics = page.getByTestId('reference-metrics');
  await expect(metrics).toContainText('本地实测指标');
  await expect(metrics).toContainText('平均句长');
  await expect(metrics).toContainText('12.5');       // 本地统计原值（不做四舍五入美化）
  await expect(metrics).toContainText('24%');         // dialogueRatio 0.24 → 24%
  await expect(metrics).toContainText('61%');         // descriptionDensity 0.61 → 61%
  await expect(metrics).toContainText('不经过模型');

  // 绊线：旧的造假对比栏不许复活
  await expect(page.getByText('风格量化对比')).toHaveCount(0);
  await expect(page.getByText('你的基线')).toHaveCount(0);
  await expect(page.getByText('参考：')).toHaveCount(0);

  await expect(page.getByTestId('reference-rules-叙事技法')).toContainText('用具体动作推进场景');
  expect(state.unexpected).toEqual([]);
});

test('D: 上传 .txt 读进文本框，并用文件名当档案名', async ({ page }) => {
  const state = await installFixture(page);

  await page.getByTestId('reference-file-input').setInputFiles({
    name: '断桥.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from(RAW, 'utf-8'),
  });

  await expect(page.getByTestId('reference-text-input')).toHaveValue(RAW);
  await expect(page.getByTestId('reference-file-notice')).toContainText('断桥.txt');
  await expect(page.getByTestId('reference-name-input')).toHaveValue('断桥');

  await page.getByTestId('reference-create-button').click();
  await expect(page.getByTestId('reference-item-rw-1')).toBeVisible();
  expect(state.createBodies[0].name).toBe('断桥');
  expect(state.unexpected).toEqual([]);
});

test('E: 改名 / 重新拆解 / 删除（带二次确认）', async ({ page }) => {
  const state = await installFixture(page, { items: [record()] });

  // 先打开报告，后续断言详情与列表保持同步
  await page.getByTestId('reference-view-rw-1').click();
  await expect(page.getByTestId('reference-report')).toContainText('《夜雨》拆解报告');

  // 改名
  await page.getByTestId('reference-rename-rw-1').click();
  await page.getByTestId('reference-rename-input').fill('夜雨（改）');
  await page.getByTestId('reference-rename-confirm').click();
  await expect(page.getByTestId('reference-item-rw-1')).toContainText('夜雨（改）');
  await expect(page.getByTestId('reference-report')).toContainText('《夜雨（改）》拆解报告');
  expect(state.patchBodies).toEqual([{ rid: 'rw-1', body: { name: '夜雨（改）' } }]);

  // 重新拆解
  await page.getByTestId('reference-reanalyze-rw-1').click();
  await expect(page.getByTestId('reference-item-rw-1')).toContainText('AI 已拆解');
  expect(state.reanalyzeCalls).toBe(1);
  await expect(page.getByTestId('reference-report')).toContainText('第二次拆解');

  // 删除：必须先确认，取消不删
  await page.getByTestId('reference-delete-rw-1').click();
  await expect(page.getByText('删除参考档案？')).toBeVisible();
  expect(state.deletes).toEqual([]);
  await page.getByTestId('reference-delete-confirm').click();
  await expect(page.getByTestId('reference-item-rw-1')).toHaveCount(0);
  await expect(page.getByTestId('reference-empty')).toBeVisible();
  expect(state.deletes).toEqual(['rw-1']);
  expect(state.unexpected).toEqual([]);
});

test('F: 列表不带原文，点「查看报告」才拉详情（原文可能几十万字）', async ({ page }) => {
  const state = await installFixture(page, { items: [record()] });

  await expect(page.getByTestId('reference-item-rw-1')).toContainText('18,000 字');
  expect(state.detailCalls).toEqual([]); // 列表阶段没有拉详情

  await page.getByTestId('reference-view-rw-1').click();
  expect(state.detailCalls).toEqual(['rw-1']);
  await expect(page.getByTestId('reference-toggle-raw')).toBeVisible();
  await expect(page.getByTestId('reference-raw-text')).toHaveCount(0); // 默认不展开原文
  await page.getByTestId('reference-toggle-raw').click();
  await expect(page.getByTestId('reference-raw-text')).toContainText('把刀放在膝上');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('G: 显式启用「创作参考」→ 显示正在参考与注入措辞；再取消即停用', async ({ page }) => {
  const state = await installFixture(page, { items: [record()] });

  // 默认不启用：没有徽章、也没有注入措辞
  await expect(page.getByTestId('reference-active-badge-rw-1')).toHaveCount(0);
  await expect(page.getByTestId('reference-activate-rw-1')).toContainText('设为创作参考');
  expect(state.activeBodies).toEqual([]);

  await page.getByTestId('reference-activate-rw-1').click();

  await expect(page.getByTestId('reference-active-badge-rw-1')).toContainText('正在参考');
  await expect(page.getByTestId('reference-activate-rw-1')).toContainText('取消参考');
  await expect(page.getByTestId('reference-inject-note-rw-1')).toContainText(
    '仅作表达参考，不覆盖本书既有风格与设定',
  );
  expect(state.activeBodies).toEqual([{ items: [{ referenceId: 'rw-1', weight: 6 }] }]);

  // 停用
  await page.getByTestId('reference-activate-rw-1').click();
  await expect(page.getByTestId('reference-active-badge-rw-1')).toHaveCount(0);
  expect(state.activeBodies).toEqual([
    { items: [{ referenceId: 'rw-1', weight: 6 }] },
    { items: [] },
  ]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('H: 页面明确写出「默认不启用、不改变文风画像」的边界', async ({ page }) => {
  await installFixture(page, { items: [record()] });

  const note = page.getByTestId('reference-scope-note');
  await expect(note).toContainText('不会改变你项目的文风画像');
  await expect(note).toContainText('显式');
  await expect(note).toContainText('默认不启用');
});

test('I: 个人素材库——只在「跨项目」的档案上标来源，本项目的档案不标', async ({ page }) => {
  // 列表返回该作者的全部档案：一条来自别的书、一条本项目在建
  const state = await installFixture(page, {
    items: [
      record({ id: 'rw-other', name: '别人的书里的档案', source_project_name: '另一本书' }),
      record({ id: 'rw-mine', name: '本书里收的档案', source_project_name: '参考档案回归项目' }),
    ],
  });

  const cross = page.getByTestId('reference-item-rw-other');
  await expect(page.getByTestId('reference-source-rw-other')).toContainText('来自：《另一本书》');
  await expect(page.getByTestId('reference-source-rw-mine')).toHaveCount(0);
  // 跨项目档案同样可以设为创作参考
  await cross.getByTestId('reference-activate-rw-other').click();
  await expect(page.getByTestId('reference-active-badge-rw-other')).toContainText('正在参考');
  expect(state.activeBodies).toEqual([{ items: [{ referenceId: 'rw-other', weight: 6 }] }]);

  // 空态也说清楚"按作者存、所有书共享"
  await expect(page.getByTestId('reference-status-rw-mine')).toBeVisible();
  expect(state.unexpected).toEqual([]);
});

test('J: 可同时启用多份参考并各自调强度；摘要按强度从高到低排', async ({ page }) => {
  const state = await installFixture(page, {
    items: [
      record({ id: 'rw-a', name: '甲书' }),
      record({ id: 'rw-b', name: '乙书' }),
    ],
  });

  await expect(page.getByTestId('reference-active-empty')).toBeVisible();

  // 先后启用两份 → 第二次请求带的是**完整集合**（后端是整体替换）
  await page.getByTestId('reference-activate-rw-a').click();
  await expect(page.getByTestId('reference-active-badge-rw-a')).toContainText('强度「中」');
  await page.getByTestId('reference-activate-rw-b').click();
  await expect(page.getByTestId('reference-active-entry-rw-b')).toContainText('乙书');

  expect(state.activeBodies).toEqual([
    { items: [{ referenceId: 'rw-a', weight: 6 }] },
    {
      items: [
        { referenceId: 'rw-a', weight: 6 },
        { referenceId: 'rw-b', weight: 6 },
      ],
    },
  ]);

  // 把「乙书」调成高（9）→ 摘要里应排在前面
  await page.getByTestId('reference-weight-rw-b').selectOption('9');
  await expect(page.getByTestId('reference-active-badge-rw-b')).toContainText('强度「高」');
  const entries = page.getByTestId('reference-active-summary').locator('li');
  await expect(entries).toHaveCount(2);
  await expect(entries.first()).toContainText('乙书');
  await expect(entries.first()).toContainText('高');

  // 摘要里写清楚了"强度=优先级"的真实含义
  await expect(page.getByTestId('reference-active-summary')).toContainText('先丢掉强度低的');

  // 取消其中一份 → 只送剩下的那份
  await page.getByTestId('reference-activate-rw-a').click();
  await expect(page.getByTestId('reference-active-badge-rw-a')).toHaveCount(0);
  expect(state.activeBodies.at(-1)).toEqual({ items: [{ referenceId: 'rw-b', weight: 9 }] });
  await expect(entries).toHaveCount(1);

  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('K: 素材库搜索与标签筛选（纯前端）+ 建档时可直接打标签', async ({ page }) => {
  const state = await installFixture(page, {
    items: [
      record({ id: 'rw-a', name: '夜雨', source_note: '', tags: ['对话流', '短句'] }),
      record({ id: 'rw-b', name: '长歌', source_note: '某短篇集第 3 篇', tags: ['悬疑'] }),
      record({ id: 'rw-c', name: '无名', source_note: '', tags: [] }),
    ],
  });

  const itemA = page.getByTestId('reference-item-rw-a');
  const itemB = page.getByTestId('reference-item-rw-b');
  await expect(page.getByTestId('reference-tags-rw-a')).toContainText('对话流');

  // 搜索：名字
  await page.getByTestId('reference-search-input').fill('长歌');
  await expect(itemB).toBeVisible();
  await expect(itemA).toHaveCount(0);
  await expect(page.getByTestId('reference-count')).toContainText('1/3');

  // 搜索：来源备注也能命中
  await page.getByTestId('reference-search-input').fill('短篇集');
  await expect(itemB).toBeVisible();
  await expect(itemA).toHaveCount(0);

  // 搜索：标签也能命中
  await page.getByTestId('reference-search-input').fill('对话');
  await expect(itemA).toBeVisible();
  await expect(itemB).toHaveCount(0);

  // 标签筛选：点 chip 收敛
  await page.getByTestId('reference-clear-filters').click();
  await expect(itemA).toBeVisible();
  await page.getByTestId('reference-tag-chip-悬疑').click();
  await expect(itemB).toBeVisible();
  await expect(itemA).toHaveCount(0);
  await expect(itemB.getByTestId('reference-activate-rw-b')).toBeVisible(); // 过滤不影响卡片功能

  // 清除筛选 → 全部回来
  await page.getByTestId('reference-clear-filters').click();
  await expect(itemA).toBeVisible();
  await expect(itemB).toBeVisible();
  await expect(page.getByTestId('reference-item-rw-c')).toBeVisible();

  // 没有命中时不报错，给一句可操作的提示
  await page.getByTestId('reference-search-input').fill('不存在的关键词');
  await expect(page.getByTestId('reference-no-match')).toContainText('清除筛选');
  await expect(page.getByTestId('reference-empty')).toHaveCount(0);

  // 编辑标签：顿号分隔 → 数组，且只 PATCH tags 字段
  await page.getByTestId('reference-clear-filters').click();
  await page.getByTestId('reference-edit-tags-rw-c').click();
  await page.getByTestId('reference-tags-edit-input').fill('动作戏、冷幽默');
  await page.getByTestId('reference-tags-confirm').click();
  await expect(page.getByTestId('reference-tags-rw-c')).toContainText('动作戏');
  expect(state.patchBodies).toEqual([{ rid: 'rw-c', body: { tags: ['动作戏', '冷幽默'] } }]);
  await expect(page.getByTestId('reference-tag-chip-动作戏')).toBeVisible();

  // 建档时带标签
  await page.getByTestId('reference-tags-input').fill('对话流、短句');
  await page.getByTestId('reference-name-input').fill('新收的');
  await page.getByTestId('reference-text-input').fill(RAW);
  await page.getByTestId('reference-create-button').click();
  await expect(page.getByTestId('reference-item-rw-1')).toBeVisible();
  expect(state.createBodies.at(-1)).toEqual({
    name: '新收的',
    text: RAW_STRIPPED,
    sourceNote: '',
    tags: ['对话流', '短句'],
  });

  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('L: 弹层打开时背景 inert、焦点被圈在弹层内，关闭后焦点还回触发按钮', async ({ page }) => {
  await installFixture(page, { items: [record({ id: 'rw-a', name: '甲书' })] });

  const trigger = page.getByTestId('reference-rename-rw-a');
  await trigger.click();
  await expect(page.getByTestId('reference-rename-input')).toBeVisible();

  // 初始聚焦：焦点必须已进入弹层，而不是留在背景页面
  expect(await page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null)).toBe(true);

  // 背景被标记为 inert：Tab 与读屏都不会跑到弹层后面
  expect(await page.evaluate(() => document.getElementById('root')?.hasAttribute('inert'))).toBe(true);

  // 焦点陷阱：连按多次 Tab，焦点必须始终留在弹层内部
  for (let index = 0; index < 12; index += 1) {
    await page.keyboard.press('Tab');
    expect(await page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null)).toBe(true);
  }

  // 关闭：inert 必须撤掉，焦点要回到当初打开它的按钮上
  await page.getByLabel('关闭').click();
  await expect(page.getByTestId('reference-rename-input')).toHaveCount(0);
  expect(await page.evaluate(() => document.getElementById('root')?.hasAttribute('inert'))).toBe(false);
  await expect(trigger).toBeFocused();
});
