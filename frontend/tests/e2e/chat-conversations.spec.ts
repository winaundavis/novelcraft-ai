import { expect, test, type Locator, type Page } from '@playwright/test';

// Every API request is intercepted. These cases never touch a database or LLM.
const PROJECT_ID = 'chat-browser-fixture';
const NOW = '2026-09-11T08:00:00Z';
type Message = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  status: 'completed' | 'pending' | 'failed' | 'interrupted';
  metadata: Record<string, unknown>;
};
type Conversation = {
  id: string;
  project_id: string;
  title: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  revision: number;
  messages: Message[];
};

function conversation(id: string, title: string, question: string, answer: string): Conversation {
  return {
    id, project_id: PROJECT_ID, title, archived: false,
    created_at: NOW, updated_at: NOW, revision: 1,
    messages: question ? [
      { id: `${id}-user`, role: 'user', content: question, timestamp: NOW, status: 'completed', metadata: {} },
      { id: `${id}-assistant`, role: 'assistant', content: answer, timestamp: NOW, status: 'completed', metadata: { reply: answer, mode: 'llm' } },
    ] : [],
  };
}

async function mockProject(page: Page, initial: Conversation[]) {
  const state = {
    records: structuredClone(initial),
    chatRequests: [] as Record<string, unknown>[],
    mutations: [] as string[],
    unexpected: [] as string[],
    detailReads: 0,
  };
  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only-access'));
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/\/$/, '');
    const method = request.method();
    const respond = (data: unknown, status = 200) => route.fulfill({ status, json: { success: status < 400, data } });
    const collection = `/api/projects/${PROJECT_ID}/chat-conversations`;
    if (path === '/api/auth/me') return respond({ id: 'mock-user', username: 'mock-user', isAdmin: false });
    if (path === collection && method === 'GET') {
      const archived = url.searchParams.get('archived') === 'true';
      return respond({ records: state.records.filter((item) => item.archived === archived).map(({ messages: _messages, ...item }) => item) });
    }
    if (path === collection && method === 'POST') {
      const item = conversation(`created-${state.records.length + 1}`, '新会话', '', '');
      state.records.unshift(item);
      state.mutations.push(`${method} ${path}`);
      return respond(item);
    }
    if (path.startsWith(`${collection}/`)) {
      const id = path.slice(collection.length + 1);
      const item = state.records.find((record) => record.id === id);
      if (!item) return respond(null, 404);
      if (method === 'GET') {
        state.detailReads += 1;
        return respond(item);
      }
      if (method === 'PATCH') {
        Object.assign(item, request.postDataJSON());
        item.revision += 1;
        state.mutations.push(`${method} ${path}`);
        return respond(item);
      }
      if (method === 'DELETE') {
        state.records = state.records.filter((record) => record.id !== id);
        state.mutations.push(`${method} ${path}`);
        return respond({ id });
      }
    }
    if (path === '/api/ai/chat' && method === 'POST') {
      const body = request.postDataJSON();
      state.chatRequests.push(body);
      const item = state.records.find((record) => record.id === body.conversationId);
      if (!item) return respond(null, 400);
      const question = body.messages.filter((message: Message) => message.role === 'user').at(-1)?.content || '';
      const reply = `已保存回复：${question}`;
      item.messages.push(
        { id: `${item.id}-u-${item.revision}`, role: 'user', content: question, timestamp: NOW, status: 'completed', metadata: {} },
        { id: `${item.id}-a-${item.revision}`, role: 'assistant', content: reply, timestamp: NOW, status: 'completed', metadata: { reply, mode: 'llm' } },
      );
      item.revision += 1;
      return respond({ reply, mode: 'llm', conversationId: item.id, requestId: body.requestId });
    }
    if (method !== 'GET') {
      state.unexpected.push(`${method} ${path}`);
      return respond(null, 405);
    }
    if (path === `/api/projects/${PROJECT_ID}`) return respond({
      id: PROJECT_ID, name: '会话浏览器测试', genre: 'scifi', status: 'draft',
      targetWordCount: 100000, currentWordCount: 0, targetChapters: 12,
      premise: '测试项目', subtitle: '', outline: null, createdAt: NOW, updatedAt: NOW,
    });
    if (path === '/api/projects') return respond([]);
    if (path.endsWith('/chapter-trash')) return respond({ records: [], targets: [], outlineHash: 'empty' });
    if (path.endsWith('/chapters') || path.endsWith('/outline-candidates')) return respond([]);
    if (path === `/api/ai/project/${PROJECT_ID}`) return respond(null);
      if (path.endsWith('/style-contract')) return respond({
        styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {},
      });
    if (path.endsWith('/story-state')) return respond({ projectId: PROJECT_ID, version: 0 });
    if (path.endsWith('/foreshadows')) return respond({ records: [], stats: {} });
    if (path.startsWith('/api/settings/')) return respond([]);
    // A closed mock boundary: unrecognized requests can never reach the server.
    state.unexpected.push(`${method} ${path}`);
    return respond(null, 404);
  });
  return state;
}

async function openAssistant(page: Page) {
  await page.goto(`/project/${PROJECT_ID}`);
  await expect(page.getByTestId('workspace-root')).toBeVisible();
  await page.getByTestId('workspace-tab-chat').click();
  const panel = page.getByTestId('assistant-panel');
  await expect(panel).toBeVisible();
  return panel;
}

async function history(panel: Locator, archived = false) {
  const tab = panel.getByTestId(archived ? 'chat-history-archived' : 'chat-history-active');
  if (!await tab.isVisible()) await panel.getByTestId('chat-history-toggle').click();
  await tab.click();
}

test.describe('Persistent project conversations — mocked APIs only', () => {
  test('pending candidates appear after their own replies', async ({ page }) => {
    const record = conversation('inline', '修改正文', '请按新大纲修改正文', '已生成正文候选');
    record.messages[1].metadata = { kind: 'copilot', candidate: {
      id: 'candidate-one', status: 'proposed', chapter_id: 'c1', candidate: '候选文字', diff: [],
    } };
    record.messages.push(
      { id: 'later-user', role: 'user', content: '再调整大纲', timestamp: NOW, status: 'completed', metadata: {} },
      { id: 'later-assistant', role: 'assistant', content: '已生成大纲候选', timestamp: NOW, status: 'completed', metadata: {
        intent: 'outline_edit', candidate: { id: 'outline-one', status: 'proposed', diff: {} },
      } },
    );
    await mockProject(page, [record]);
    const panel = await openAssistant(page);
    const proseReply = panel.getByTestId('assistant-message').filter({ hasText: '已生成正文候选' });
    const outlineReply = panel.getByTestId('assistant-message').filter({ hasText: '已生成大纲候选' });
    await expect(proseReply.locator('..').getByTestId('copilot-candidate')).toBeVisible();
    await expect(outlineReply.locator('..').getByTestId('outline-candidate')).toBeVisible();
    const order = await panel.locator('[data-testid="assistant-message"], [data-testid="copilot-candidate"], [data-testid="outline-candidate"]').evaluateAll(
      (elements) => elements.map((element) => element.getAttribute('data-testid')),
    );
    expect(order).toEqual(['assistant-message', 'copilot-candidate', 'assistant-message', 'outline-candidate']);
  });
  test('conversation menu fits desktop and mobile without runtime errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
    const state = await mockProject(page, [conversation('visual', '第一卷剧情与人物关系讨论', '如何让人物动机更连贯？', '可以先从人物当前的目标与阻碍开始讨论。')]);
    const panel = await openAssistant(page);
    await expect(page).toHaveURL(new RegExp(`/project/${PROJECT_ID}$`));
    await expect(page).toHaveTitle(/NovelCraft/i);
    await history(panel);
    await expect(panel.getByTestId('chat-conversation-visual')).toBeVisible();
    await expect(page.locator('vite-error-overlay')).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath('desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(panel.getByTestId('chat-conversation-visual')).toBeVisible();
    const menu = await panel.getByTestId('chat-conversation-visual').boundingBox();
    expect(menu!.x).toBeGreaterThanOrEqual(0);
    expect(menu!.x + menu!.width).toBeLessThanOrEqual(390);
    await page.screenshot({ path: test.info().outputPath('mobile.png') });
    expect(errors).toEqual([]);
    expect(state.chatRequests).toHaveLength(0);
    expect(state.unexpected).toEqual([]);
  });

  test('refresh restores the server transcript without sending an AI request', async ({ page }) => {
    const state = await mockProject(page, [conversation('saved', '已保存的会话', '如何安排第一幕？', '第一幕先呈现人物目标。')]);
    const panel = await openAssistant(page);
    await expect(panel).toContainText('第一幕先呈现人物目标。');
    await page.reload();
    await page.getByTestId('workspace-tab-chat').click();
    await expect(panel).toContainText('如何安排第一幕？');
    await expect(panel).toContainText('第一幕先呈现人物目标。');
    expect(state.chatRequests).toHaveLength(0);
    expect(state.unexpected).toEqual([]);
  });

  test('new and switched conversations keep independent transcripts', async ({ page }) => {
    const state = await mockProject(page, [conversation('first', '第一条会话', '旧问题', '旧会话独有回复')]);
    const panel = await openAssistant(page);
    await expect(panel).toContainText('旧会话独有回复');
    await panel.getByTestId('chat-new').click();
    await expect(panel).not.toContainText('旧会话独有回复');
    await panel.getByTestId('chat-input').fill('这是第二条会话的问题');
    await panel.getByTestId('chat-input').press('Enter');
    await expect(panel).toContainText('已保存回复：这是第二条会话的问题');
    expect(state.chatRequests).toHaveLength(1);
    expect(state.chatRequests[0].conversationId).toBe('created-2');
    expect(state.chatRequests[0].requestId).toEqual(expect.any(String));
    await history(panel);
    await panel.getByTestId('chat-conversation-first').click();
    await expect(panel).toContainText('旧会话独有回复');
    await expect(panel).not.toContainText('已保存回复：这是第二条会话的问题');
    await history(panel);
    await panel.getByTestId('chat-conversation-created-2').click();
    await expect(panel).toContainText('已保存回复：这是第二条会话的问题');
    await expect(panel).not.toContainText('旧会话独有回复');
    expect(state.unexpected).toEqual([]);
  });

  test('archive is recoverable and deletion requires confirmation', async ({ page }) => {
    const state = await mockProject(page, [conversation('keep', '需要管理的会话', '管理问题', '管理回复')]);
    const panel = await openAssistant(page);
    await history(panel);
    await panel.getByTestId('chat-archive-keep').click();
    await expect(panel.getByTestId('chat-conversation-keep')).toHaveCount(0);
    await history(panel, true);
    await expect(panel.getByTestId('chat-conversation-keep')).toBeVisible();
    await panel.getByTestId('chat-restore-keep').click();
    await history(panel);
    await expect(panel.getByTestId('chat-conversation-keep')).toBeVisible();
    await panel.getByTestId('chat-delete-keep').click();
    await expect(page.getByTestId('chat-delete-confirm')).toBeVisible();
    expect(state.records.some((item) => item.id === 'keep')).toBe(true);
    expect(state.mutations.filter((item) => item.startsWith('DELETE'))).toHaveLength(0);
    await page.getByTestId('chat-delete-confirm').click();
    await expect(panel.getByTestId('chat-conversation-keep')).toHaveCount(0);
    expect(state.records.some((item) => item.id === 'keep')).toBe(false);
    expect(state.mutations.filter((item) => item.startsWith('DELETE'))).toEqual([
      `DELETE /api/projects/${PROJECT_ID}/chat-conversations/keep`,
    ]);
    expect(state.unexpected).toEqual([]);
  });

  test('full and compact assistants share the selected conversation', async ({ page }) => {
    const state = await mockProject(page, [
      conversation('first', '第一会话', '第一问题', '第一会话的内容'),
      conversation('second', '第二会话', '第二问题', '第二会话的内容'),
    ]);
    const panel = await openAssistant(page);
    await history(panel);
    await panel.getByTestId('chat-conversation-second').click();
    await expect(panel).toContainText('第二会话的内容');
    await page.getByTestId('workspace-tab-write').click();
    const compact = page.getByTestId('assistant-panel-compact');
    await expect(compact).toBeVisible();
    await expect(compact).toContainText('第二会话的内容');
    await expect(compact).not.toContainText('第一会话的内容');
    await history(compact);
    await compact.getByTestId('chat-conversation-first').click();
    await page.getByTestId('workspace-tab-chat').click();
    await expect(panel).toContainText('第一会话的内容');
    await expect(panel).not.toContainText('第二会话的内容');
    expect(state.chatRequests).toHaveLength(0);
    expect(state.unexpected).toEqual([]);
  });

  test('refresh during a pending reply polls completion without a duplicate AI POST', async ({ page }) => {
    const pending = conversation('pending', '生成中的会话', '正在处理的唯一问题', '');
    pending.messages[1].status = 'pending';
    pending.messages[1].metadata = { request_id: 'existing-request' };
    const state = await mockProject(page, [pending]);
    const panel = await openAssistant(page);
    await expect(panel).toContainText('正在处理的唯一问题');
    await page.reload();
    await page.getByTestId('workspace-tab-chat').click();
    await expect(panel).toContainText('正在处理的唯一问题');
    const readsAfterRefresh = state.detailReads;
    const message = state.records[0].messages[1];
    message.status = 'completed';
    message.content = '刷新后收到后台已完成的回复';
    message.metadata = { reply: message.content, mode: 'llm' };
    state.records[0].revision += 1;
    await expect(panel).toContainText('刷新后收到后台已完成的回复', { timeout: 15000 });
    expect(state.detailReads).toBeGreaterThan(readsAfterRefresh);
    expect(state.chatRequests).toHaveLength(0);
    expect(state.unexpected).toEqual([]);
  });
});
