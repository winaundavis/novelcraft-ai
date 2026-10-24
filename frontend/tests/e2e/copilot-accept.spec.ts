import { expect, test, type Page } from '@playwright/test';

// Closed API boundary: real React UI, no backend writes or model calls.
const PID = 'copilot-accept-fixture';
const ORIGINAL = '旧稿：雨停后，她把信放回抽屉。';
const FIRST = '第一次修改：雨停后，她把信交给门外的人。';
const SECOND = '第二次修改：她握紧信封，终于推开门。';
const NOW = '2026-09-13T09:00:00Z';

async function fixture(page: Page, holdAccept = false, proposedOutlineCandidate: Record<string, unknown> | null = null) {
  const chapter = (id: string, content: string) => ({ id, projectId: PID, volumeId: 'v1',
    title: id === 'c1' ? '第一章' : '第二章', order: id === 'c1' ? 1 : 2,
    content, wordCount: content.length, status: 'draft', createdAt: NOW, updatedAt: NOW });
  const outline = { mainSummary: '离线测试', structure: 'three-act', nodes: [
    { id: 'v1', type: 'volume', title: '第一卷', summary: '送出那封信。', order: 1 },
    { id: 'a1', type: 'arc', parentId: 'v1', title: '送信', summary: '决定送信。', order: 1 },
    { id: 'c1', type: 'chapter', parentId: 'v1', arcId: 'a1', title: '第一章', summary: '决定送信', order: 1 },
    { id: 'c2', type: 'chapter', parentId: 'v1', arcId: 'a1', title: '第二章', summary: '门外', order: 2 },
  ] };
  const state = { chapters: [chapter('c1', ORIGINAL), chapter('c2', '第二章独有正文。')],
    requests: [] as Record<string, any>[], chatRequests: [] as Record<string, any>[], messages: [] as Record<string, any>[],
    candidates: [] as Record<string, any>[], unexpected: [] as string[], errors: [] as string[],
    acceptCount: 0, releaseAccept: undefined as (() => void) | undefined, revision: 1 };
  page.on('pageerror', (error) => state.errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') state.errors.push(message.text()); });
  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));
  await page.route('**/api/**', async (route) => {
    const req = route.request(), url = new URL(req.url()), method = req.method();
    const path = url.pathname.replace(/\/$/, ''), base = `/api/projects/${PID}`;
    const respond = (data: unknown) => route.fulfill({ json: { success: true, data } });
    if (path === '/api/auth/me') return respond({ id: 'mock', username: 'mock', isAdmin: false });
    if (path === `${base}/chat-conversations`) return respond({ records: url.searchParams.get('archived') === 'true' ? [] : [
      { id: 'conv', project_id: PID, title: '正文修改', archived: false, revision: state.revision, created_at: NOW, updated_at: NOW },
    ] });
    if (path === `${base}/chat-conversations/conv`) return respond({ id: 'conv', project_id: PID,
      title: '正文修改', archived: false, revision: state.revision, created_at: NOW, updated_at: NOW, messages: state.messages });
    if (path === '/api/ai/chat' && method === 'POST') {
      // 普通对话回复（用于验证"漏判后可一键转为正文修改"的兼底入口）
      const body = req.postDataJSON();
      state.chatRequests.push(body);
      const question = body.messages?.[body.messages.length - 1]?.content || '';
      state.messages.push(
        { id: `cu-${state.chatRequests.length}`, role: 'user', content: question, timestamp: NOW, status: 'completed', metadata: {} },
        { id: `ca-${state.chatRequests.length}`, role: 'assistant', content: '这一章的主角叫陈九。', timestamp: NOW, status: 'completed',
          metadata: { mode: 'llm', intent: 'consultation' } },
      );
      state.revision++;
      return respond({ reply: '这一章的主角叫陈九。', mode: 'llm', intent: 'consultation' });
    }
    if (path === '/api/copilot/candidates' && method === 'POST') {
      const body = req.postDataJSON(); state.requests.push(body);
      const original = body.selectionText || body.currentText;
      const replacement = state.requests.length === 1 ? FIRST : SECOND;
      const candidate = { id: `candidate-${state.requests.length}`, project_id: PID, chapter_id: body.chapterId,
        rewrite_type: 'rewrite', status: 'proposed', original, rewritten: replacement, candidate: replacement,
        diff: [{ type: 'replace', original, candidate: replacement }] };
      state.candidates.push(candidate);
      state.messages.push(
        { id: `u-${state.requests.length}`, role: 'user', content: body.instruction, timestamp: NOW, status: 'completed', metadata: {} },
        { id: `a-${state.requests.length}`, role: 'assistant', content: '已生成候选，请确认。', timestamp: NOW,
          status: 'completed', metadata: { kind: 'copilot', mode: 'llm', operation: body, candidate } },
      );
      state.revision++;
      return respond(candidate);
    }
    if (path.endsWith('/accept') && method === 'POST') {
      state.acceptCount++;
      const candidate = state.candidates.find((item) => path.includes(item.id))!;
      const target = state.chapters.find((item) => item.id === candidate.chapter_id)!;
      if (!target.content.includes(candidate.original)) return route.fulfill({ status: 409, json: { detail: 'candidate no longer matches current chapter' } });
      target.content = target.content.replace(candidate.original, candidate.candidate);
      target.wordCount = target.content.length;
      candidate.status = 'applied'; state.revision++;
      if (holdAccept) await new Promise<void>((resolve) => { state.releaseAccept = resolve; });
      return respond({ ...candidate, applied_content: target.content });
    }
    if (path === `${base}/chapters` && method === 'GET') return respond(state.chapters);
    if (path.startsWith(`${base}/chapters/`)) {
      const cid = path.slice(`${base}/chapters/`.length);
      if (cid.endsWith('/projections')) return respond([]);
      if (method === 'GET') return respond(state.chapters.find((item) => item.id === cid));
      if (method === 'PUT') {
        const target = state.chapters.find((item) => item.id === cid)!;
        target.content = req.postDataJSON().content; return respond(target);
      }
    }
    if (method === 'GET') {
      if (path === base) return respond({ id: PID, name: '候选同步测试', genre: 'mystery', status: 'drafting',
        premise: '送出那封信。', subtitle: '', currentWordCount: 100, targetWordCount: 10000, targetChapters: 2, outline, createdAt: NOW, updatedAt: NOW });
      if (path.endsWith('/outline-candidates')) return respond(proposedOutlineCandidate ? [proposedOutlineCandidate] : []);
      if (path === '/api/projects' || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) return respond([]);
      if (path.endsWith('/chapter-trash')) return respond({ records: [], targets: [], outlineHash: 'outline-v1' });
      if (path === `/api/ai/project/${PID}`) return respond(null);
      if (path.endsWith('/style-contract')) return respond({
        // 真实后端总是同时给出 styleTone 与顶层成对的计划 token；
        // 空的 id + 0 表示「明确未启用计划」，与「读取失败」是两回事。
        styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {},
      });
      if (path.endsWith('/story-state')) return respond({ projectId: PID, version: 1 });
      if (path.endsWith('/foreshadows')) return respond({ records: [], stats: {} });
    }
    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });
  await page.goto(`/project/${PID}`);
  await expect(page.getByRole('heading', { name: '候选同步测试', exact: true })).toBeVisible();
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(ORIGINAL);
  return state;
}

async function send(page: Page, instruction = '调整本章正文') {
  const input = page.getByTestId('assistant-panel-compact').getByTestId('chat-input');
  await expect(input).toBeEnabled();
  await input.fill(instruction);
  await input.press('Enter');
  await expect(page.getByTestId('copilot-accept')).toBeVisible();
}

test('accept immediately updates visible prose and the next edit uses that prose', async ({ page }) => {
  const state = await fixture(page);
  await send(page);
  await page.getByTestId('copilot-accept').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(FIRST);
  await send(page, '继续调整本章正文');
  expect(state.requests[1].currentText).toBe(FIRST);
  expect(state.requests[1].selectionText).toBe('');
  await page.getByTestId('copilot-accept').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(SECOND);
  await page.getByTestId('editor-save').click();
  expect(state.chapters[0].content).toBe(SECOND);
  await page.reload();
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(SECOND);
  await expect(page).toHaveURL(new RegExp(`/project/${PID}$`));
  await expect(page).toHaveTitle(/NovelCraft/i);
  await expect(page.locator('vite-error-overlay')).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('accepted-prose.png') });
  expect(state.errors).toEqual([]); expect(state.unexpected).toEqual([]);
});

test('regenerate an accepted reply uses current prose instead of historical operation snapshot', async ({ page }) => {
  const state = await fixture(page);
  await send(page);
  await page.getByTestId('copilot-accept').click();
  await expect(page.getByTestId('copilot-accept')).toHaveCount(0);
  await page.getByRole('button', { name: '重新生成回复', exact: true }).click();
  await expect.poll(() => state.requests.length).toBe(2);
  expect(state.requests[1].currentText).toBe(FIRST);
  expect(state.requests[1].selectionText).toBe('');
});

test('unsaved manual edits block accepting an older candidate without losing local prose', async ({ page }) => {
  const state = await fixture(page);
  await send(page);
  await page.getByTestId('editor-textarea').fill('作者刚刚输入、尚未保存的新正文。');
  await page.getByTestId('copilot-accept').click();
  await expect(page.getByRole('alert')).toContainText('保存');
  expect(state.acceptCount).toBe(0);
  await expect(page.getByTestId('editor-textarea')).toHaveValue('作者刚刚输入、尚未保存的新正文。');
});

test('unsaved prose remains protected when accepting from the full Assistant tab', async ({ page }) => {
  const state = await fixture(page);
  await send(page);
  const draft = '作者保留的未保存正文，切换助手后也不能丢失。';
  await page.getByTestId('editor-textarea').fill(draft);
  await page.getByTestId('workspace-tab-chat').click();
  await expect(page.getByTestId('editor-textarea')).toHaveCount(0);
  const assistant = page.getByTestId('assistant-panel');
  await assistant.getByTestId('copilot-accept').click();
  await expect.soft(assistant.getByRole('alert')).toContainText('保存', { timeout: 3000 });
  expect.soft(state.acceptCount).toBe(0);
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(draft);
  expect(state.errors).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('accept response arriving after chapter switch never overwrites the selected chapter', async ({ page }) => {
  const state = await fixture(page, true);
  await send(page);
  await page.getByTestId('copilot-accept').click();
  await expect.poll(() => Boolean(state.releaseAccept)).toBe(true);
  await page.getByTestId('chapter-tree-arc-a1').click();
  await page.getByTestId('chapter-tree-item-c2').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章独有正文。');
  state.releaseAccept!();
  await expect(page.getByTestId('copilot-accept')).toHaveCount(0);
  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章独有正文。');
  expect(state.unexpected).toEqual([]);
});

test('a proposed outline candidate is restored after a refresh', async ({ page }) => {
  // 对话消息里可能不再带着候选；刷新后必须从后端恢复，用户才仍能应用/拒绝。
  const proposed = {
    id: 'oc-restored', projectId: PID, baseOutlineHash: 'h1', baseOutlineVersion: 1,
    candidateOutline: { mainSummary: '恢复后的主线', structure: 'three-act', nodes: [] },
    diff: { added: [{ type: 'arc', title: '新增支线' }], removed: [], edited: [], moved: [] },
    instruction: '调整大纲', status: 'proposed',
  };

  const state = await fixture(page, false, proposed);

  // 首次进入即恢复
  await expect(page.getByTestId('restored-outline-candidate')).toBeVisible();
  await expect(page.getByTestId('restored-outline-candidate').getByTestId('outline-candidate')).toBeVisible();

  // 整页刷新后仍然恢复（消息列表里没有该候选）
  await page.reload();
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('restored-outline-candidate')).toBeVisible();
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

// 用户报告：口语化的正文调整指令有时不进入正文候选流程，而是当成普通对话回复。
// 下面两条分别覆盖"缺范围词"和"缺修改动词"两种漏判路径。
test('a casually worded prose edit request still produces a body candidate', async ({ page }) => {
  const state = await fixture(page);
  await send(page, '把这段改一下');
  expect(state.requests).toHaveLength(1);
  expect(state.requests[0].chapterId).toBe('c1');
  expect(state.unexpected).toEqual([]);
});

test('a prose edit request without a generic edit verb still produces a body candidate', async ({ page }) => {
  const state = await fixture(page);
  await send(page, '精简一下本章');
  expect(state.requests).toHaveLength(1);
  expect(state.requests[0].chapterId).toBe('c1');
  expect(state.unexpected).toEqual([]);
});

test('a pure question stays a conversation and does not touch the body', async ({ page }) => {
  // 反向保护：提问不得被误判为正文修改。
  const state = await fixture(page);
  const input = page.getByTestId('assistant-panel-compact').getByTestId('chat-input');
  await input.fill('这一章的主角叫什么名字？');
  await input.press('Enter');
  await expect.poll(() => state.chatRequests.length).toEqual(1);
  expect(state.requests).toHaveLength(0);
  expect(state.unexpected).toEqual([]);
});

test('common prose edit phrasings all reach the body candidate flow', async ({ page }) => {
  for (const phrasing of ['帮我润色一下', '把开头改一下', '本章节奏太快，调整一下']) {
    const state = await fixture(page);
    await send(page, phrasing);
    expect(state.requests, phrasing).toHaveLength(1);
    expect(state.requests[0].chapterId, phrasing).toBe('c1');
    expect(state.unexpected, phrasing).toEqual([]);
  }
});

test('a chat reply can be converted into a body edit with one click', async ({ page }) => {
  // 漏判兼底：普通回复仍可一键按上一条指令生成正文候选，避免"以为改了、实际没改"。
  const state = await fixture(page);
  const input = page.getByTestId('assistant-panel-compact').getByTestId('chat-input');
  await input.fill('这一章的主角叫什么名字？');
  await input.press('Enter');
  await expect(page.getByTestId('apply-instruction-to-prose')).toBeVisible();
  expect(state.requests).toHaveLength(0);

  await page.getByTestId('apply-instruction-to-prose').click();
  await expect(page.getByTestId('copilot-accept')).toBeVisible();
  expect(state.requests).toHaveLength(1);
  expect(state.requests[0].instruction).toContain('叫什么名字');
  expect(state.requests[0].chapterId).toBe('c1');
  expect(state.unexpected).toEqual([]);
});
