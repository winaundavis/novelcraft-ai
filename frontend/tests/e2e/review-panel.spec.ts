import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 审校面板回归（全离线，拦截所有 /api/**）。
 *
 * 覆盖：
 * A. 超时/诊断：慢响应不再被误判"AI 服务降级"；429/502 说明真实原因；
 * B. 结果保留：切到正文/其他页签再回到审校，上次审校分析仍在（不重复请求）；
 * C. 一键修改：按建议改写并写入正文；已应用标记；可撤销；
 * D. 过期保护：正文改动后结果标记过期并禁用一键修改。
 *
 * 【已知未修】E. 整页重新加载（浏览器丢弃闲置标签页 / 开发服务器重连都会这样）后，
 *    审校结果会丢、页签也会回到默认值 —— 作者报告过。本次只完成了取证，未落地修复，
 *    原因与方案见 CHANGELOG 的「仍未修」段，所以这里不写断言。
 */

const PID = 'review-fixture';
const NOW = '2026-09-15T00:00:00Z';
const SNIPPET = '她把信放回抽屉';
const CONTENT = `雨停后，${SNIPPET}，然后离开了房间。她知道这件事不会就此结束。`;
const REWRITTEN = '她把信在手里攥了很久，才重新折好收进抽屉';
const AFTER_APPLY = CONTENT.replace(SNIPPET, REWRITTEN);

type ReviewMode =
  | { kind: 'ok' }
  | { kind: 'ok-batch' }
  | { kind: 'slow'; delayMs: number }
  | { kind: 'status'; status: number; detail: string };

/** 第二条建议的原文：在 CONTENT 里只出现一次，且第一条改完后仍存在。 */
const SECOND_SNIPPET = '她知道这件事不会就此结束';

function reviewPayload(batch = false) {
  const issues = [
    {
      severity: 'important',
      type: '动作笼统',
      location: '第 1 段',
      original: SNIPPET,
      suggestion: '用更具体的动作替换"放回抽屉"，体现犹豫。',
      reason: '笼统动作削弱人物状态。',
    },
  ];
  if (batch) {
    issues.push({
      severity: 'suggestion',
      type: '收尾偏弱',
      location: '末句',
      original: SECOND_SNIPPET,
      suggestion: '末句可以更含蓄，不要直接点题。',
      reason: '点破会削弱悬念。',
    });
  }
  return {
    totalIssues: issues.length,
    fatalCount: 0,
    importantCount: 1,
    suggestionCount: batch ? 1 : 0,
    summary: `综合审校完成：致命 0，重要 1，建议 ${batch ? 1 : 0}。`,
    languageScore: 88,
    logicScore: 80,
    rhythmScore: 85,
    issues,
  };
}

function chapterRecord(content: string) {
  return {
    id: 'c1', projectId: PID, volumeId: 'v1', title: '第一章', order: 1,
    content, wordCount: content.length, status: 'completed', createdAt: NOW, updatedAt: NOW,
  };
}

async function installFixture(page: Page, mode: ReviewMode) {
  const base = `/api/projects/${PID}`;
  const state = {
    chapters: [chapterRecord(CONTENT)],
    reviewCalls: 0,
    candidates: [] as Record<string, any>[],
    accepts: [] as string[],
    saves: [] as string[],
    pendingCandidate: null as { id: string; original: string; rewritten: string } | null,
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

    if (path === '/api/review/comprehensive' && method === 'POST') {
      state.reviewCalls += 1;
      if (mode.kind === 'status') return route.fulfill({ status: mode.status, json: { detail: mode.detail } });
      if (mode.kind === 'slow') await new Promise((resolve) => setTimeout(resolve, mode.delayMs));
      return ok(route, reviewPayload(mode.kind === 'ok-batch'));
    }

    if (path === '/api/copilot/candidates' && method === 'POST') {
      const body = req.postDataJSON();
      state.candidates.push(body);
      const original = body.selectionText || body.currentText;
      // 批量用例：逐条把命中的原文加上标记，便于断言顺序与结果；
      // 单条用例保持原样（把选中的句子换成重写后的句子）。
      const rewritten = body.selectionText
        ? (mode.kind === 'ok-batch' ? `${body.selectionText}（已按建议改）` : REWRITTEN)
        : `${CONTENT}\n（已按建议改写）`;
      state.pendingCandidate = { id: `cand-${state.candidates.length}`, original, rewritten };
      return ok(route, {
        id: state.pendingCandidate.id, project_id: PID, chapter_id: 'c1', status: 'proposed',
        original, rewritten, candidate: rewritten, diff: [{ type: 'replace', original, candidate: rewritten }],
      });
    }
    if (path.startsWith('/api/copilot/candidates/') && path.endsWith('/accept') && method === 'POST') {
      const candidate = state.pendingCandidate!;
      state.accepts.push(path);
      const target = state.chapters[0];
      target.content = candidate.original
        ? target.content.replace(candidate.original, candidate.rewritten)
        : candidate.rewritten;
      target.wordCount = target.content.length;
      return ok(route, { id: candidate.id, status: 'applied', applied_content: target.content });
    }

    if (path === `${base}/chat-conversations` && method === 'GET') return ok(route, { records: [] });
    if (path === `${base}/chapters` && method === 'GET') return ok(route, state.chapters);
    if (path.startsWith(`${base}/chapters/`)) {
      const chapterId = path.slice(`${base}/chapters/`.length);
      if (chapterId.endsWith('/projections')) return ok(route, []);
      const target = state.chapters.find((item) => item.id === chapterId) || state.chapters[0];
      if (method === 'GET') return ok(route, target);
      if (method === 'PUT') {
        const content = String(req.postDataJSON().content ?? '');
        state.saves.push(content);
        target.content = content;
        target.wordCount = content.length;
        return ok(route, target);
      }
    }

    if (method === 'GET') {
      if (path === base) {
        return ok(route, { id: PID, name: '审校回归项目', subtitle: '', genre: 'mystery', status: 'drafting',
          premise: '她把信放回抽屉。', currentWordCount: CONTENT.length, targetWordCount: 10000, targetChapters: 3,
          outline: { mainSummary: '送信', structure: 'three-act', nodes: [
            { id: 'v1', type: 'volume', title: '第一卷', summary: '送出那封信。', order: 1 },
            { id: 'c1', type: 'chapter', parentId: 'v1', title: '第一章', summary: '决定送信', order: 1 },
          ] }, createdAt: NOW, updatedAt: NOW });
      }
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) return ok(route, {
        styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {},
      });
      if (path.endsWith('/story-state')) return ok(route, { projectId: PID, version: 1 });
      if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
      if (path === `/api/ai/project/${PID}`) return ok(route, null);
      if (path === '/api/projects' || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) return ok(route, []);
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${PID}`);
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(CONTENT);
  await page.getByTestId('workspace-tab-review').click();
  await expect(page.getByTestId('review-start-button')).toBeEnabled();
  return state;
}

/** 跑一次审校并等待结果出现（等第一条建议，而不是等写死的汇总文案）。 */
async function runReview(page: Page) {
  await page.getByTestId('review-start-button').click();
  await expect(page.getByTestId('review-issue-select-0')).toBeVisible({ timeout: 30_000 });
}

// ── A. 超时与诊断 ─────────────────────────────────────────

test('a review slower than the old 20s deadline still succeeds instead of degrading', async ({ page }) => {
  test.setTimeout(120_000);
  const state = await installFixture(page, { kind: 'slow', delayMs: 22_000 });

  await page.getByTestId('review-start-button').click();
  await expect(page.getByTestId('review-progress-hint')).toBeVisible();

  await expect(page.getByTestId('review-degraded-reason')).toHaveCount(0);
  await expect(page.getByText('综合审校完成：致命 0，重要 1，建议 0。')).toBeVisible({ timeout: 30_000 });
  expect(state.reviewCalls).toBe(1);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('a rate-limited review explains the quota instead of blaming the model', async ({ page }) => {
  const state = await installFixture(page, { kind: 'status', status: 429, detail: '请求过于频繁，请稍后再试。' });

  await page.getByTestId('review-start-button').click();

  const banner = page.getByTestId('review-degraded-reason');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText('请求过于频繁');
  await expect(banner).toContainText('与模型无关');
  expect(state.reviewCalls).toBe(1);
  expect(state.unexpected).toEqual([]);
});

test('an upstream provider failure reports the provider error, not a generic degradation', async ({ page }) => {
  const state = await installFixture(page, { kind: 'status', status: 502, detail: 'LLM 上游返回错误。' });

  await page.getByTestId('review-start-button').click();

  const banner = page.getByTestId('review-degraded-reason');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText('模型服务返回错误');
  await expect(banner).toContainText('LLM 上游返回错误');
  expect(state.unexpected).toEqual([]);
});

test('a successful review renders scores and issues without any degradation banner', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });

  await runReview(page);

  await expect(page.getByTestId('review-degraded-reason')).toHaveCount(0);
  await expect(page.getByTestId('review-error')).toHaveCount(0);
  // 每条建议下面是一个可勾选的「修改」开关，统一由上方的一键修改执行
  await expect(page.getByTestId('review-issue-select-0')).toHaveText(/修改/);
  await expect(page.getByTestId('review-apply-selected')).toBeDisabled();
  expect(state.reviewCalls).toBe(1);
  expect(state.unexpected).toEqual([]);
});

// ── B. 结果保留 ───────────────────────────────────────────

test('the review result survives switching tabs and coming back', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });
  await runReview(page);

  // 去看正文
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(CONTENT);
  // 再看别的功能
  await page.getByTestId('workspace-tab-characters').click();
  // 切回审校
  await page.getByTestId('workspace-tab-review').click();

  // 上次审校分析仍在，且没有重复请求模型
  await expect(page.getByText('综合审校完成：致命 0，重要 1，建议 0。')).toBeVisible();
  await expect(page.getByText('动作笼统')).toBeVisible();
  await expect(page.getByTestId('review-issue-select-0')).toBeVisible();
  expect(state.reviewCalls).toBe(1);
  expect(state.unexpected).toEqual([]);
});

// ── C. 勾选 + 一键修改（可以只选一条，也可以全选一次改） ──────

test('selected suggestions are applied together in list order and undone as one batch', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok-batch' });
  await runReview(page);

  // 默认没勾任何一条：一键修改是禁用的，不会出现「点了却没反应」
  await expect(page.getByTestId('review-selection-hint')).toContainText('已选 0');
  await expect(page.getByTestId('review-apply-selected')).toBeDisabled();

  // 全选两条，检查按钮上的条数与提示
  await page.getByTestId('review-select-all').click();
  await expect(page.getByTestId('review-selection-hint')).toContainText('已选 2');
  await expect(page.getByTestId('review-issue-select-0')).toHaveText(/已选/);
  await expect(page.getByTestId('review-apply-selected')).toContainText('一键修改（2）');

  await page.getByTestId('review-apply-selected').click();

  await expect(page.getByTestId('review-apply-message')).toContainText('已按 2 条审校建议依次修改正文');
  await expect(page.getByTestId('review-issue-applied')).toHaveCount(2);
  // 逐条按列表顺序执行，每条都基于上一条改完后的正文重新定位原文
  expect(state.candidates).toHaveLength(2);
  expect(state.candidates[0].selectionText).toBe(SNIPPET);
  expect(state.candidates[1].selectionText).toBe(SECOND_SNIPPET);
  expect(state.accepts).toHaveLength(2);

  // 正文里两条都落地了
  await page.getByTestId('workspace-tab-write').click();
  const textarea = page.getByTestId('editor-textarea');
  await expect(textarea).toHaveValue(/（已按建议改）/);
  await expect(textarea).not.toHaveValue(CONTENT);

  // 撤销以「整批」为单位：一次回到本批开始前的正文
  await page.getByTestId('workspace-tab-review').click();
  await page.getByTestId('review-undo-apply').click();
  await expect(page.getByTestId('review-apply-message')).toContainText('已撤销');
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(CONTENT);
  expect(state.saves).toEqual([CONTENT]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('a single selected suggestion still works and can be undone', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });
  await runReview(page);

  await page.getByTestId('review-issue-select-0').click();
  await expect(page.getByTestId('review-apply-selected')).toContainText('一键修改（1）');
  await page.getByTestId('review-apply-selected').click();

  await expect(page.getByTestId('review-apply-message')).toContainText('已按审校建议修改正文');
  expect(state.candidates).toHaveLength(1);
  expect(state.candidates[0].selectionText).toBe(SNIPPET);
  expect(state.candidates[0].instruction).toContain('用更具体的动作替换');
  expect(state.accepts).toHaveLength(1);
  await expect(page.getByTestId('review-issue-applied')).toBeVisible();

  // 正文确实被改了
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(AFTER_APPLY);

  // 撤销：写回修改前正文
  await page.getByTestId('workspace-tab-review').click();
  await page.getByTestId('review-undo-apply').click();
  await expect(page.getByTestId('review-apply-message')).toContainText('已撤销');
  expect(state.saves).toEqual([CONTENT]);
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(CONTENT);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

// ── D. 过期保护 ───────────────────────────────────────────

test('editing the body after a review marks the result stale and blocks one-click apply', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });
  await runReview(page);

  // 回正文改一句
  await page.getByTestId('workspace-tab-write').click();
  const textarea = page.getByTestId('editor-textarea');
  await expect(textarea).toHaveValue(CONTENT);
  await textarea.fill(`${CONTENT}\n新增一句。`);

  await page.getByTestId('workspace-tab-review').click();

  // 结果仍在（不清空），但标记过期：单条「修改」与「一键修改」都禁用
  await expect(page.getByTestId('review-stale-notice')).toBeVisible();
  await expect(page.getByText('综合审校完成：致命 0，重要 1，建议 0。')).toBeVisible();
  await expect(page.getByTestId('review-issue-select-0')).toBeDisabled();
  await expect(page.getByTestId('review-apply-selected')).toBeDisabled();
  expect(state.candidates).toHaveLength(0);
  expect(state.reviewCalls).toBe(1);
});

// ── E. 整页重新加载不会丢审校结果（作者报告的「回来结果就没了」） ──
//
// 触发这条路的不一定是我们自己的代码：浏览器把闲置标签页丢弃后恢复、开发服务器
// HMR 重连、随手按 F5，都会让页面重新加载一次，内存里的状态全部清空。
// 原因之一是审校快照不落盘（现已改为 `nc-review`）。
// 【未落地】页签本身仍不会回到离开时那一页，所以下面刷新后显式点一下审校
// —— 那是另一条待修项，详见 CHANGELOG「仍未修」。

test('a full page reload keeps the review result', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });
  await runReview(page);

  await page.reload();
  await page.getByTestId('workspace-tab-review').click();

  // 结果从落盘的快照里恢复，而不是又跑了一遍模型
  await expect(page.getByText('综合审校完成：致命 0，重要 1，建议 0。')).toBeVisible();
  await expect(page.getByText('动作笼统')).toBeVisible();
  expect(state.reviewCalls).toBe(1);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('after a reload the restored result still knows whether the body changed', async ({ page }) => {
  const state = await installFixture(page, { kind: 'ok' });
  await runReview(page);

  // 审校之后改了正文，然后重新加载：恢复出来的结果必须**如实**标记过期，
  // 而不是把旧结论当成当前结论（这是当初「不持久化」想防的事，现在由指纹判断兜住）
  await page.getByTestId('workspace-tab-write').click();
  const textarea = page.getByTestId('editor-textarea');
  await expect(textarea).toHaveValue(CONTENT);
  await textarea.fill(`${CONTENT}\n她改了主意。`);
  await page.getByTestId('workspace-tab-review').click();
  await expect(page.getByTestId('review-stale-notice')).toBeVisible();

  await page.reload();
  await page.getByTestId('workspace-tab-review').click();

  await expect(page.getByTestId('review-stale-notice')).toBeVisible();
  await expect(page.getByTestId('review-issue-select-0')).toBeDisabled();
  expect(state.reviewCalls).toBe(1);
  expect(state.unexpected).toEqual([]);
});

// ── 已知未修：页签不会回到离开时那一页 ─────────────────────
//
// 刷新（或浏览器丢弃闲置标签页后恢复）后，页签仍会回到默认值。修复（按项目把页签写
// sessionStorage 再读回来）已写好并自测通过，但它会与一个**既有竞态**打架：
// 「初始页签的决定」会把作者在加载期间手动点的页签覆盖回去
// （最小复现：同一页面里连续 goto、每次先点「正文」→ `copilot-accept.spec.ts` 稳定失败；
// 关掉记忆即 11 passed）。因此本次只落地了审校结果持久化。