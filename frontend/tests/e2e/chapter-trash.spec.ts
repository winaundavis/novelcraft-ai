import { expect, test, type Page } from '@playwright/test';

// The browser renders the real app; every API request is intercepted, with no database or LLM access.
const PROJECT_ID = 'chapter-trash-browser-fixture';
const NOW = '2026-09-12T08:00:00Z';
const RESTORED = '# 原稿标题\n\n这是从旧大纲移除的完整正文。';
const SAVED = '当前章节已经保存的正文。';
const outline = { mainSummary: '回收站测试', structure: 'three-act', nodes: [
  { id: 'v1', type: 'volume', title: '第一卷', order: 1, summary: '第一卷故事', targetWordCount: 10000 },
  { id: 'a1', type: 'arc', parentId: 'v1', title: '开篇剧情弧', order: 1, summary: '开篇剧情', targetWordCount: 10000 },
  { id: 'c1', type: 'chapter', parentId: 'v1', arcId: 'a1', title: '当前第一章', order: 1, summary: '第一章故事', targetWordCount: 5000 },
  { id: 'c2', type: 'chapter', parentId: 'v1', title: '空白第二章', order: 2, summary: '第二章故事', targetWordCount: 5000 },
] };

function chapter(id: string, content: string) {
  return { id, projectId: PROJECT_ID, volumeId: 'v1', title: id === 'c1' ? '当前第一章' : '空白第二章',
    order: id === 'c1' ? 1 : 2, content, wordCount: content.length, status: 'draft', summary: '', notes: '', createdAt: NOW, updatedAt: NOW };
}

function record(id = 'old', content = RESTORED) {
  return { id, projectId: PROJECT_ID, sourceChapterId: 'removed-c1', title: '原稿标题',
    originalLocation: '旧卷 / 旧剧情弧 / 第三章', reason: 'outline_removed', removedAt: NOW,
    wordCount: content.length, contentHash: `hash-${id}`, content };
}

async function mockProject(page: Page, options: { empty?: boolean; conflict?: boolean; holdRestoreRead?: boolean; holdSave?: boolean; holdRestore?: boolean; batch?: boolean; manyRecords?: number } = {}) {
  const state = {
    records: options.empty ? [] : options.manyRecords ? Array.from({ length: options.manyRecords }, (_, index) =>
      ({ ...record(`bulk-${index}`, `第 ${index + 1} 条历史原稿。`), sourceChapterId: 'removed-x', title: `历史原稿 ${index + 1}` })
    ) : options.batch ? [
      // 原章节 c2 当前为空 → 应还原成功
      { ...record('r-c2', '第二章的原稿正文。'), sourceChapterId: 'c2', title: '第二章原稿' },
      // 原章节已不在当前大纲 → 应跳过
      { ...record('r-gone'), sourceChapterId: 'removed-x', title: '大纲已移除的章' },
      // 原章节 c1 已有正文 → 应跳过（A 方案：不覆盖）
      { ...record('r-c1'), sourceChapterId: 'c1', title: '与当前章节同号的原稿' },
    ] : [record()], chapters: options.empty ? [] : [chapter('c1', SAVED)],
    mutations: [] as string[], restoreBodies: [] as Record<string, unknown>[], unexpected: [] as string[],
    pendingChapterReads: [] as Array<() => void>, pendingSaves: [] as Array<() => void>,
    pendingRestores: [] as Array<() => void>, completedRestores: 0,
  };
  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only-access'));
  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/\/$/, '');
    const method = req.method();
    const base = `/api/projects/${PROJECT_ID}`;
    const trash = `${base}/chapter-trash`;
    const respond = (data: unknown) => route.fulfill({ json: { success: true, data } });
    if (path === '/api/auth/me') return respond({ id: 'mock-user', username: 'mock-user', isAdmin: false });
    if (path === trash && method === 'GET') return respond({
      records: state.records.map(({ content: _content, ...item }) => item), outlineHash: 'outline-v1',
      targets: options.empty ? [] : ['c1', 'c2'].map((id) => ({ chapterId: id,
        title: id === 'c1' ? '当前第一章' : '空白第二章', location: `第一卷 / ${id === 'c1' ? '当前第一章' : '空白第二章'}`,
        hasContent: Boolean(state.chapters.find((item) => item.id === id)?.content),
        contentHash: state.chapters.find((item) => item.id === id)?.content ? 'target-saved-hash' : '',
      })),
    });
    // 批量端点必须排在下面的 `startsWith(trash + '/')` 之前：否则 /batch-restore 会被
    // 当成 trash_id 吃掉，永远匹配不到。这里的跳过规则按后端 service 复刻，
    // 用来验证 UI 如何呈现「成功几条 / 跳过几条及原因」。
    if (path === `${trash}/batch-restore` && method === 'POST') {
      const body = req.postDataJSON() as { ids: string[]; outlineHash: string };
      const restored: Array<Record<string, string>> = [];
      const skipped: Array<Record<string, string>> = [];
      const claimed = new Set<string>();
      for (const trashId of body.ids) {
        const entry = state.records.find((item) => item.id === trashId);
        if (!entry) {
          skipped.push({ trashId, title: '', reason: 'missing_record', message: '回收记录已不存在（可能已被还原或永久删除）。' });
          continue;
        }
        if (!['c1', 'c2'].includes(entry.sourceChapterId)) {
          skipped.push({ trashId, title: entry.title, reason: 'source_not_in_outline', message: '原章节已不在当前大纲中，无法自动还原；请单独选择目标章节还原。' });
          continue;
        }
        if (claimed.has(entry.sourceChapterId)) {
          skipped.push({ trashId, title: entry.title, reason: 'duplicate_source', message: '同一原章节在本批中已被另一条记录占用，请单独处理这条。' });
          continue;
        }
        const current = state.chapters.find((item) => item.id === entry.sourceChapterId);
        if (current?.content?.trim()) {
          skipped.push({ trashId, title: entry.title, reason: 'target_has_content', message: '原章节已有正文，批量还原不会覆盖正文；请单独确认后再还原。' });
          continue;
        }
        state.records = state.records.filter((item) => item.id !== trashId);
        state.chapters = [...state.chapters.filter((item) => item.id !== entry.sourceChapterId), chapter(entry.sourceChapterId, entry.content)];
        claimed.add(entry.sourceChapterId);
        restored.push({ trashId, chapterId: entry.sourceChapterId, title: entry.title });
      }
      state.mutations.push(`${method} ${path}`);
      return respond({ restored, skipped, outlineHash: 'outline-v1' });
    }
    if (path === `${trash}/batch-delete` && method === 'POST') {
      const body = req.postDataJSON() as { ids: string[] };
      const deleted: string[] = [];
      const missing: string[] = [];
      for (const trashId of body.ids) {
        if (state.records.some((item) => item.id === trashId)) {
          state.records = state.records.filter((item) => item.id !== trashId);
          deleted.push(trashId);
        } else {
          missing.push(trashId);
        }
      }
      state.mutations.push(`${method} ${path}`);
      return respond({ deleted, missing });
    }
    if (path.startsWith(`${trash}/`)) {
      const id = path.slice(trash.length + 1).split('/')[0];
      const item = state.records.find((entry) => entry.id === id);
      if (path.endsWith('/restore') && method === 'POST' && item) {
        const body = req.postDataJSON();
        state.restoreBodies.push(body);
        if (options.conflict) return route.fulfill({ status: 409, json: { detail: '大纲已更新，请刷新回收站后重试。' } });
        const old = state.chapters.find((entry) => entry.id === body.targetChapterId);
        if (old?.content && !body.confirmReplace) return route.fulfill({ status: 409, json: { detail: '请确认替换已有正文。' } });
        state.records = state.records.filter((entry) => entry.id !== id);
        if (old?.content) state.records.push({ ...record('displaced', old.content), title: old.title, reason: 'restore_replaced' });
        state.chapters = [...state.chapters.filter((entry) => entry.id !== body.targetChapterId), chapter(body.targetChapterId, item.content)];
        state.mutations.push(`${method} ${path}`);
        if (options.holdRestore) await new Promise<void>((resolve) => state.pendingRestores.push(resolve));
        state.completedRestores += 1;
        return respond({ chapterId: body.targetChapterId, displacedTrashId: old?.content ? 'displaced' : null, projectionResults: [] });
      }
      if (method === 'GET' && item) return respond(item);
      if (method === 'DELETE' && item && url.searchParams.get('confirm') === 'true') {
        state.records = state.records.filter((entry) => entry.id !== id);
        state.mutations.push(`${method} ${path}`);
        return respond(null);
      }
    }
    if (path === `${base}/chapters` && method === 'GET') return respond(state.chapters);
    if (path.startsWith(`${base}/chapters/`)) {
      const id = path.slice(`${base}/chapters/`.length);
      if (method === 'GET' && id.endsWith('/projections')) return respond([]);
      if (method === 'GET') {
        if (options.holdRestoreRead && state.restoreBodies.length > 0) await new Promise<void>((resolve) => state.pendingChapterReads.push(resolve));
        return respond(state.chapters.find((item) => item.id === id) || chapter(id, ''));
      }
      if (method === 'DELETE') {
        const old = state.chapters.find((item) => item.id === id);
        if (old) state.records.push({ ...record('recycled', old.content), title: old.title, reason: 'manual_delete' });
        state.chapters = state.chapters.filter((item) => item.id !== id);
        state.mutations.push(`${method} ${path}`);
        return respond(null);
      }
      if (method === 'PUT') {
        const body = req.postDataJSON();
        state.chapters = [...state.chapters.filter((item) => item.id !== id), chapter(id, body.content)];
        if (options.holdSave) await new Promise<void>((resolve) => state.pendingSaves.push(resolve));
        return respond(chapter(id, body.content));
      }
    }
    if (method === 'GET') {
      if (path === base) return respond({ id: PROJECT_ID, name: '章节回收站测试', genre: 'scifi', status: 'drafting',
        targetWordCount: 100000, currentWordCount: state.chapters.reduce((total, item) => total + item.wordCount, 0),
        targetChapters: 2, premise: '测试项目', subtitle: '', outline: options.empty ? null : outline, createdAt: NOW, updatedAt: NOW });
      if (path === '/api/projects' || path.endsWith('/outline-candidates') || path.endsWith('/recoverable') || path.startsWith('/api/settings/')) return respond([]);
      if (path === `/api/ai/project/${PROJECT_ID}`) return respond(null);
      if (path.endsWith('/chat-conversations')) return respond({ records: [] });
      if (path.endsWith('/style-contract')) return respond({
        styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {},
      });
      if (path.endsWith('/story-state')) return respond({ projectId: PROJECT_ID, version: 0 });
      if (path.endsWith('/foreshadows')) return respond({ records: [], stats: {} });
    }
    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unrecognized mock request' } });
  });
  return state;
}

async function openTrash(page: Page) {
  await page.getByTestId('chapter-trash-open').filter({ visible: true }).click();
  await expect(page.getByTestId('chapter-trash-dialog')).toBeVisible();
}

test.describe('Chapter recycle bin — mocked APIs only', () => {
  test('empty project still exposes the recycle bin', async ({ page }) => {
    const state = await mockProject(page, { empty: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await openTrash(page);
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('回收站为空');
    expect(state.unexpected).toEqual([]);
  });

  test('previews original metadata and confirms replacement before reloading the selected editor', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
    const state = await mockProject(page);
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(SAVED);
    await expect(page.getByTestId('chapter-tree-arc-a1')).toBeVisible();
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-old').click();
    const dialog = page.getByTestId('chapter-trash-dialog');
    await expect(dialog).toContainText(RESTORED);
    await expect(dialog).toContainText('旧卷 / 旧剧情弧 / 第三章');
    await expect(dialog).toContainText('大纲');
    await expect(dialog).toContainText('2026');
    await page.getByTestId('chapter-trash-target').selectOption('c1');
    await page.getByTestId('chapter-trash-restore').click();
    await expect(page.getByTestId('chapter-trash-replace-confirm')).toBeVisible();
    expect(state.restoreBodies).toHaveLength(0);
    await page.screenshot({ path: test.info().outputPath('desktop-replace.png') });
    await page.getByTestId('chapter-trash-replace-confirm').click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(RESTORED);
    expect(state.restoreBodies).toEqual([{ targetChapterId: 'c1', outlineHash: 'outline-v1', targetContentHash: 'target-saved-hash', confirmReplace: true }]);
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-displaced').click();
    await expect(dialog).toContainText(SAVED);
    expect(errors).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });

  test('mobile restores into an empty current-outline target and selects its editor', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const state = await mockProject(page);
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('toggle-chapter-panel').click();
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-old').click();
    await page.getByTestId('chapter-trash-target').selectOption('c2');
    const box = await page.getByTestId('chapter-trash-dialog').boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    await expect(page).toHaveTitle(/NovelCraft/i);
    await expect(page.locator('vite-error-overlay')).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath('mobile-preview.png') });
    await page.getByTestId('chapter-trash-restore').click();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(RESTORED);
    await expect(page.getByTestId('chapter-panel-drawer')).not.toBeVisible();
    expect(state.restoreBodies[0]).toEqual({ targetChapterId: 'c2', outlineHash: 'outline-v1', targetContentHash: '', confirmReplace: false });
    expect(state.unexpected).toEqual([]);
  });

  test('batch restore sends every record back to its own chapter and reports skips', async ({ page }) => {
    const state = await mockProject(page, { batch: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await openTrash(page);
    const dialog = page.getByTestId('chapter-trash-dialog');
    const count = page.getByTestId('chapter-trash-selected-count');
    await expect(count).toHaveText('已选 0 条');

    // 多选：勾一个不影响单条预览
    await page.getByTestId('chapter-trash-check-r-c2').check();
    await expect(count).toHaveText('已选 1 条');
    await page.getByTestId('chapter-trash-record-r-gone').click();
    await expect(dialog).toContainText('大纲已移除的章');
    await expect(count).toHaveText('已选 1 条');

    // 全选
    await page.getByTestId('chapter-trash-select-all').click();
    await expect(count).toHaveText('已选 3 条');
    await page.getByTestId('chapter-trash-select-all').click();
    await expect(count).toHaveText('已选 0 条');
    await page.getByTestId('chapter-trash-select-all').click();

    await page.getByTestId('chapter-trash-batch-restore').click();
    const result = page.getByTestId('chapter-trash-batch-result');
    await expect(result).toContainText('已还原 1 条');
    await expect(result).toContainText('跳过 2 条');
    await expect(result).toContainText('原章节已不在当前大纲中');
    await expect(result).toContainText('批量还原不会覆盖正文');

    // 还原成功的清出选择，被跳过的留在选中态供作者逐条处理
    await expect(count).toHaveText('已选 2 条');
    expect(state.records.map((item) => item.id).sort()).toEqual(['r-c1', 'r-gone']);
    expect(state.chapters.find((item) => item.id === 'c2')?.content).toBe('第二章的原稿正文。');
    // 关键：被跳过那条的原章节正文没有被覆盖
    expect(state.chapters.find((item) => item.id === 'c1')?.content).toBe(SAVED);
    expect(state.unexpected).toEqual([]);
  });

  test('batch delete requires confirmation and cancellation deletes nothing', async ({ page }) => {
    const state = await mockProject(page, { batch: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await openTrash(page);
    await page.getByTestId('chapter-trash-select-all').click();
    await page.getByTestId('chapter-trash-batch-delete').click();
    await expect(page.getByTestId('chapter-trash-batch-delete-confirm')).toContainText('确认永久删除 3 条');
    expect(state.mutations).toEqual([]);

    await page.getByTestId('chapter-trash-batch-delete-cancel').click();
    await expect(page.getByTestId('chapter-trash-batch-delete-confirm')).toHaveCount(0);
    expect(state.mutations).toEqual([]);
    await expect(page.getByTestId('chapter-trash-selected-count')).toHaveText('已选 3 条');

    await page.getByTestId('chapter-trash-batch-delete').click();
    await page.getByTestId('chapter-trash-batch-delete-confirm').click();
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('回收站为空');
    expect(state.records).toEqual([]);
    expect(state.mutations).toEqual([`POST /api/projects/${PROJECT_ID}/chapter-trash/batch-delete`]);
    expect(state.unexpected).toEqual([]);
  });

  test('batch delete stays within the per-request id cap by chunking the selection', async ({ page }) => {
    // 回收站会持续增长（实测某个 Beta 项目已有 484 条），而后端把单请求 id 数限制在 500。
    // 若把「全选」一次性发出去，超过上限会被整个 422 拒绝——这里钉住分片汇总的行为。
    const state = await mockProject(page, { manyRecords: 460 });
    const batches: number[] = [];
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/chapter-trash/batch-delete')) {
        batches.push((request.postDataJSON() as { ids: string[] }).ids.length);
      }
    });
    await page.goto(`/project/${PROJECT_ID}`);
    await openTrash(page);
    await page.getByTestId('chapter-trash-select-all').click();
    await expect(page.getByTestId('chapter-trash-selected-count')).toHaveText('已选 460 条');

    await page.getByTestId('chapter-trash-batch-delete').click();
    await page.getByTestId('chapter-trash-batch-delete-confirm').click();
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('回收站为空');

    // 每片都不超过上限，且覆盖了全部 460 条
    expect(batches.length).toBeGreaterThan(1);
    expect(Math.max(...batches)).toBeLessThanOrEqual(200);
    expect(batches.reduce((total, size) => total + size, 0)).toBe(460);
    expect(state.records).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });

  test('batch actions stay disabled until something is selected', async ({ page }) => {
    const state = await mockProject(page, { batch: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await openTrash(page);
    await expect(page.getByTestId('chapter-trash-batch-restore')).toBeDisabled();
    await expect(page.getByTestId('chapter-trash-batch-delete')).toBeDisabled();
    await page.getByTestId('chapter-trash-check-r-c2').check();
    await expect(page.getByTestId('chapter-trash-batch-restore')).toBeEnabled();
    await expect(page.getByTestId('chapter-trash-batch-delete')).toBeEnabled();
    expect(state.mutations).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });

  test('unsaved prose blocks batch restore and batch delete', async ({ page }) => {
    const state = await mockProject(page, { batch: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('workspace-tab-write').click();
    const editor = page.getByTestId('editor-textarea');
    await expect(editor).toHaveValue(SAVED);
    await editor.fill('尚未保存，必须保留的修改');
    await openTrash(page);
    await page.getByTestId('chapter-trash-select-all').click();
    await page.getByTestId('chapter-trash-batch-restore').click();
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('请先保存');
    await page.getByTestId('chapter-trash-batch-delete').click();
    await expect(page.getByTestId('chapter-trash-batch-delete-confirm')).toHaveCount(0);
    expect(state.mutations).toEqual([]);
    expect(state.records).toHaveLength(3);
    await expect(editor).toHaveValue('尚未保存，必须保留的修改');
    expect(state.unexpected).toEqual([]);
  });

  test('permanent deletion requires confirmation and cancellation preserves the record', async ({ page }) => {
    const state = await mockProject(page);
    await page.goto(`/project/${PROJECT_ID}`);
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-old').click();
    await page.getByTestId('chapter-trash-delete').click();
    await expect(page.getByTestId('chapter-trash-delete-confirm')).toBeVisible();
    expect(state.mutations).toEqual([]);
    await page.getByTestId('chapter-trash-confirm-cancel').click();
    await expect(page.getByTestId('chapter-trash-record-old')).toBeVisible();
    await page.getByTestId('chapter-trash-delete').click();
    await page.getByTestId('chapter-trash-delete-confirm').click();
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('回收站为空');
    expect(state.mutations).toEqual([`DELETE /api/projects/${PROJECT_ID}/chapter-trash/old`]);
    expect(state.unexpected).toEqual([]);
  });

  test('unsaved prose blocks restore, permanent deletion, and direct recycle until saved', async ({ page }) => {
    const state = await mockProject(page);
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('workspace-tab-write').click();
    const editor = page.getByTestId('editor-textarea');
    await expect(editor).toHaveValue(SAVED);
    await editor.fill('尚未保存，必须保留的修改');
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-old').click();
    await page.getByTestId('chapter-trash-target').selectOption('c2');
    await page.getByTestId('chapter-trash-restore').click();
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('请先保存');
    await page.getByTestId('chapter-trash-delete').click();
    await expect(page.getByTestId('chapter-trash-delete-confirm')).not.toBeVisible();
    await page.getByRole('dialog', { name: '章节回收站', exact: true }).getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByTestId('chapter-tree-arc-a1').click();
    await page.getByTestId('chapter-recycle-c1').click();
    await expect(page.getByText('请先保存当前章节的未保存修改。', { exact: true })).toBeVisible();
    expect(state.mutations).toEqual([]);
    await expect(editor).toHaveValue('尚未保存，必须保留的修改');
    await page.getByTestId('editor-save').click();
    await expect(page.getByTestId('markdown-editor')).toContainText('已保存');
    await page.getByTestId('chapter-recycle-c1').click();
    await expect(page.getByTestId('chapter-recycle-confirm')).toBeVisible();
    expect(state.mutations).toEqual([]);
    await page.getByTestId('chapter-recycle-confirm').click();
    await expect(editor).toHaveValue('');
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-recycled').click();
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('尚未保存，必须保留的修改');
    expect(state.unexpected).toEqual([]);
  });

  test('a stale restore shows the conflict and retains the preview and editor', async ({ page }) => {
    const state = await mockProject(page, { conflict: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(SAVED);
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-old').click();
    await page.getByTestId('chapter-trash-target').selectOption('c2');
    await page.getByTestId('chapter-trash-restore').click();
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText('大纲已更新');
    await expect(page.getByTestId('chapter-trash-dialog')).toContainText(RESTORED);
    await expect(page.getByTestId('editor-textarea')).toHaveValue(SAVED);
    expect(state.mutations).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });

  test('same-ID restore immediately replaces old prose even when the refresh GET is delayed', async ({ page }) => {
    const state = await mockProject(page, { holdRestoreRead: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(SAVED);
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-old').click();
    await page.getByTestId('chapter-trash-target').selectOption('c1');
    await page.getByTestId('chapter-trash-restore').click();
    await page.getByTestId('chapter-trash-replace-confirm').click();
    await expect(page.getByTestId('chapter-trash-dialog')).not.toBeVisible();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(RESTORED);
    await page.getByTestId('editor-textarea').fill(`${RESTORED}\n恢复后继续修改`);
    state.pendingChapterReads.splice(0).forEach((release) => release());
    await expect(page.getByTestId('editor-textarea')).toHaveValue(`${RESTORED}\n恢复后继续修改`);
    expect(state.unexpected).toEqual([]);
  });

  test('typing during a pending save remains dirty and blocks recycling', async ({ page }) => {
    const state = await mockProject(page, { holdSave: true });
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('workspace-tab-write').click();
    const editor = page.getByTestId('editor-textarea');
    await expect(editor).toHaveValue(SAVED);
    await page.getByTestId('chapter-tree-arc-a1').click();
    await editor.fill('已提交保存的版本');
    await page.getByTestId('editor-save').click();
    await expect.poll(() => state.pendingSaves.length).toBe(1);
    await editor.fill('保存尚未返回时继续输入的版本');
    state.pendingSaves.splice(0).forEach((release) => release());
    await expect(page.getByTestId('editor-save')).toBeEnabled();
    await page.getByTestId('chapter-recycle-c1').click();
    await expect(page.getByText('请先保存当前章节的未保存修改。', { exact: true })).toBeVisible();
    await expect(page.getByTestId('chapter-recycle-confirm')).not.toBeVisible();
    await expect(editor).toHaveValue('保存尚未返回时继续输入的版本');
    expect(state.mutations).toEqual([]);
  });

  test('a newly saved chapter immediately exposes its recycle control', async ({ page }) => {
    await mockProject(page);
    await page.goto(`/project/${PROJECT_ID}`);
    await page.getByTestId('chapter-tree-arc-a1').click();
    await page.getByTestId('chapter-tree-item-c2').click();
    await expect(page.getByTestId('editor-textarea')).toHaveValue('');
    await page.getByTestId('editor-textarea').fill('首次保存的正文');
    await page.getByTestId('editor-save').click();
    await expect(page.getByTestId('chapter-recycle-c2')).toBeVisible();
  });

  test('restoration completing after the mobile drawer unmounts preserves new editor input', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const state = await mockProject(page, { holdRestore: true });
    await page.goto(`/project/${PROJECT_ID}`);
    // 手机端 12 个 tab 已收进菜单（横滑条只在 lg+ 可见），所以这里走菜单切到「正文」
    await page.getByTestId('open-tab-menu').click();
    await page.getByTestId('tab-menu-item-write').click();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(SAVED);
    await page.getByTestId('toggle-chapter-panel').click();
    await openTrash(page);
    await page.getByTestId('chapter-trash-record-old').click();
    await page.getByTestId('chapter-trash-target').selectOption('c2');
    await page.getByTestId('chapter-trash-restore').click();
    await expect.poll(() => state.pendingRestores.length).toBe(1);
    await page.setViewportSize({ width: 1440, height: 960 });
    await expect(page.getByTestId('chapter-trash-dialog')).not.toBeVisible();
    await page.getByTestId('editor-textarea').fill('窗口切换后继续写的新内容');
    state.pendingRestores.splice(0).forEach((release) => release());
    await expect.poll(() => state.completedRestores).toBe(1);
    await expect(page.getByTestId('editor-textarea')).toHaveValue('窗口切换后继续写的新内容');
  });
});
