import { APIRequestContext, expect, test } from '@playwright/test';
import { fileURLToPath } from 'url';
import * as path from 'path';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BACKEND_BASE = process.env.NOVELCRAFT_API_BASE || 'http://127.0.0.1:8000';
const DB_PATH = process.env.NOVELCRAFT_DB_PATH
  || path.resolve(__dirname, '../../../backend/data/novelcraft.db');

// P1 记忆投影失败产品闭环 —— 前端浏览器级测试（Codex 复测验收第 5 项）：
// 1) 面板渲染 5 路投影状态（failed 可见 + 原因 detail）
// 2) failed 项重试按钮点击 → 调用 replay API → 状态更新
// 3) 重复点击幂等（重放成功后按钮消失，不产生重复状态）
// 数据注入：测试通过 node:sqlite 直接写 memory_projections 制造 failed 状态
// （后端无"置 failed"接口，真实失败路径由后端单测覆盖；此处验证前端展示与交互闭环）。

test.describe.configure({ mode: 'default' });

// 这是一组**真实后端 + 真实 SQLite** 用例：既调后端 API，也用 node:sqlite 直接写
// memory_projections 表。需要后端 `INVITE_TOKENS` 中的邀请码（由 playwright.config.ts 注入）。
// 未提供时整文件跳过，避免看起来像回归的假失败。
test.skip(
  !process.env.NOVELCRAFT_E2E_INVITE,
  '需要 NOVELCRAFT_E2E_INVITE（后端 INVITE_TOKENS 中的邀请码）才能对真实后端运行；'
  + '只想跑离线用例时忽略本文件即可。',
);

// Codex P2 复测 P2：完整 E2E 运行时后端也在写同一 DB（live-LLM 用例触发），
// 直接 db.exec 会偶发 `database is locked`。设置 busy_timeout 让 SQLite 等待
// 锁释放（最多 5s），消除与后端并发写库的锁冲突。
function withBusyTimeout(db: DatabaseSync) {
  db.exec('PRAGMA busy_timeout = 5000');
}

function writeProjection(db: DatabaseSync, projectId: string, chapterId: string, type: string, status: string, detail = '') {
  withBusyTimeout(db);
  db.exec(`
    INSERT INTO memory_projections (id, project_id, chapter_id, projection_type, status, detail, created_at, updated_at)
    VALUES ('${projectId}-${type}', '${projectId}', '${chapterId}', '${type}', '${status}', '${detail}', '2026-08-18T00:00:00', '2026-08-18T00:00:00')
    ON CONFLICT(project_id, chapter_id, projection_type) DO UPDATE SET
      status = excluded.status, detail = excluded.detail, updated_at = excluded.updated_at
  `);
}

function clearProjections(db: DatabaseSync, projectId: string) {
  withBusyTimeout(db);
  db.exec(`DELETE FROM memory_projections WHERE project_id = '${projectId}'`);
}

async function createProjectWithChapter(request: APIRequestContext) {
  const projectResp = await request.post(`${BACKEND_BASE}/api/projects/`, {
    data: { name: `projection-e2e-${Date.now()}`, genre: 'xuanhuan', targetWordCount: 100000 },
  });
  expect(projectResp.ok()).toBeTruthy();
  const project = (await projectResp.json()).data as { id: string };

  const outline = {
    mainSummary: '投影闭环测试。',
    nodes: [
      { id: 'v1', type: 'volume', title: '第一卷', order: 1 },
      { id: 'c1', type: 'chapter', parentId: 'v1', title: '第一章', order: 1 },
    ],
  };
  await request.patch(`${BACKEND_BASE}/api/projects/${project.id}`, { data: { outline, targetChapters: 1 } });

  const chapterResp = await request.post(`${BACKEND_BASE}/api/projects/${project.id}/chapters/`, {
    data: { title: '第一章', volumeId: 'v1', order: 1 },
  });
  expect(chapterResp.ok()).toBeTruthy();
  const chapter = (await chapterResp.json()).data as { id: string };

  // 写正文（有内容的章节，投影状态才有业务意义）
  await request.put(`${BACKEND_BASE}/api/projects/${project.id}/chapters/${chapter.id}`, {
    data: { content: '云破霄踏入宗门的第一天，雪正落。', title: '第一章', volumeId: 'v1', order: 1 },
  });

  return { projectId: project.id, chapterId: chapter.id };
}

test.describe('ProjectionStatusPanel 前端闭环', () => {
  let projectId = '';
  let chapterId = '';

  test.beforeAll(async ({ request }) => {
    const created = await createProjectWithChapter(request);
    projectId = created.projectId;
    chapterId = created.chapterId;

    // 注入投影状态：facts=failed（带原因），其余 done
  const db = new DatabaseSync(DB_PATH);
  try {
      // The projection table is project-scoped; seed the chapter row first so
      // SQLite foreign-key enforcement cannot make the browser fixture order-dependent.
      db.exec(`
        INSERT OR IGNORE INTO chapters
          (id, project_id, volume_id, title, chapter_order, content, word_count, status, summary, notes, created_at, updated_at)
        VALUES ('${chapterId}', '${projectId}', 'v1', '第一章', 1, '云破霄踏入宗门的第一天，雪正落。', 18, 'completed', '', '', '2026-08-18T00:00:00', '2026-08-18T00:00:00')
      `);
      const contentHash = createHash('sha256')
        .update('云破霄踏入宗门的第一天，雪正落。', 'utf8')
        .digest('hex')
        .slice(0, 32);
      db.exec(`
        INSERT OR IGNORE INTO chapter_commit_history
          (id, project_id, chapter_id, version, parent_commit_id, content_hash, context_hash, audit_id, provider, model, created_at)
        VALUES ('${projectId}-commit', '${projectId}', '${chapterId}', 1, '', '${contentHash}', 'fixture-context', '', 'mock', 'deterministic', '2026-08-18T00:00:00')
      `);
      clearProjections(db, projectId);
      writeProjection(db, projectId, chapterId, 'chapter_summary', 'done');
      writeProjection(db, projectId, chapterId, 'facts', 'failed', '模拟事实抽取失败：LLM 超时');
      writeProjection(db, projectId, chapterId, 'entities', 'done');
      writeProjection(db, projectId, chapterId, 'volume', 'done');
      writeProjection(db, projectId, chapterId, 'fts', 'done');
    } finally {
      db.close();
    }
  });

  test.afterAll(async ({ request }) => {
    const db = new DatabaseSync(DB_PATH);
    try {
      clearProjections(db, projectId);
    } finally {
      db.close();
    }
    await request.delete(`${BACKEND_BASE}/api/projects/${projectId}`);
  });

  test('面板渲染 5 路状态：failed 可见且带原因 + 重试按钮', async ({ page }) => {
    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('markdown-editor')).toBeVisible();

    // Codex P2 复测 P2：等待章节树加载完成（chapterNodes 就绪后编辑器自动选中
    // 第一章，currentChapterId 非空投影面板才渲染）——消除完整运行时
    // chapterNodes 加载慢导致的时序竞态（投影面板偶发不渲染）。
    const treeItem = page.getByTestId(`chapter-tree-item-${chapterId}`);
    await expect(treeItem).toBeVisible({ timeout: 15000 });

    // 竞态根治：ChapterTree 用「后端章节 id」（随机 id）设置 currentChapterId，
    // MarkdownEditor 用「outline 节点 id」（c1）设置 currentChapterId，两者竞争。
    // 完整运行时 MarkdownEditor 可能先设置 c1，导致投影查询用 c1 查不到
    // 测试写入的（projectId, 随机id）数据。显式点击章节树中的目标章节，
    // 确保 currentChapterId 稳定指向测试写入投影的章节 id（随机 id）。
    await treeItem.click();

    // 编辑器选中目标章节 → 面板默认折叠，点击标题展开明细
    await expect(page.getByTestId('projection-panel-content')).toHaveCount(0);
    await page.getByTestId('projection-panel-toggle').click();
    const panel = page.getByTestId('projection-list');
    await expect(panel).toBeVisible();

    // failed 计数徽标
    await expect(page.getByTestId('projection-failed-count')).toContainText('有 1 项待处理');

    // 5 路投影状态齐全
    await expect(panel).toContainText('章节摘要');
    await expect(panel).toContainText('事实抽取');
    await expect(panel).toContainText('实体抽取');
    await expect(panel).toContainText('卷摘要');
    await expect(panel).toContainText('FTS 索引');

    // facts 行：显示失败原因 detail + 重试按钮（有 detail 时不显示"同步失败"标签）
    await expect(panel).toContainText('模拟事实抽取失败');
    await expect(panel).toContainText('重试');
    const replayBtn = page.getByTestId('projection-replay-facts');
    await expect(replayBtn).toBeVisible();
    await expect(replayBtn).toContainText('重试');

    // 其余 done 项显示"已同步"（无重试按钮）
    await expect(page.getByTestId('projection-replay-facts')).toHaveCount(1);
    await expect(panel.getByText('已同步')).toHaveCount(4);
  });

  test('点击重试：调用 replay API，重放成功 → 状态更新为已同步', async ({ page, request }) => {
    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('markdown-editor')).toBeVisible();

    // Codex P2 复测 P2：等待章节树加载完成（chapterNodes 就绪后编辑器自动选中
    // 第一章，currentChapterId 非空投影面板才渲染）——消除完整运行时时序竞态。
    const treeItem = page.getByTestId(`chapter-tree-item-${chapterId}`);
    await expect(treeItem).toBeVisible({ timeout: 15000 });
    // 竞态根治：显式点击目标章节，确保 currentChapterId 稳定指向测试写入投影的章节 id。
    await treeItem.click();

    await page.getByTestId('projection-panel-toggle').click();
    const replayBtn = page.getByTestId('projection-replay-facts');
    await expect(replayBtn).toBeVisible();
    await replayBtn.click();

    // 重放成功 → facts 变已同步 → 按钮消失、failed 计数徽标消失
    await expect(page.getByTestId('projection-replay-facts')).toHaveCount(0, { timeout: 60000 });
    await expect(page.getByTestId('projection-failed-count')).toHaveCount(0);

    // 后端状态确认：facts=done（通过 REST 查询投影接口验证真实链路）
    await expect.poll(async () => {
      const resp = await request.get(
        `${BACKEND_BASE}/api/projects/${projectId}/chapters/${chapterId}/projections`,
      );
      if (!resp.ok()) return '';
      const json = (await resp.json()) as {
        data?: Array<{ projection_type: string; status: string }>;
      };
      const facts = (json.data || []).find((p) => p.projection_type === 'facts');
      return facts?.status || '';
    }, { timeout: 60000 }).toBe('done');

    // 幂等说明：重复点击/重复重放的幂等（只重跑该项、不重复）已由后端单测
    // `test_replay_failed_projection_only` 覆盖（重复重放 facts 只调 facts 一次）。
    // 此处 REST 再次注入 failed 并 reload，验证"重启/刷新后失败状态仍可见、可再次重试"：
    const db = new DatabaseSync(DB_PATH);
    try {
      writeProjection(db, projectId, chapterId, 'facts', 'failed', '第二次失败');
    } finally {
      db.close();
    }

    // reload 后重新走完整流程：写tab → 编辑器 → 投影面板（稳定 testid）。
    // reload 后当前章可能不是 c1，显式点击章节树中的目标章节确保选中。
    await page.reload();
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('markdown-editor')).toBeVisible({ timeout: 60000 });
    const reloadedTreeItem = page.getByTestId(`chapter-tree-item-${chapterId}`);
    await expect(reloadedTreeItem).toBeVisible({ timeout: 60000 });
    await reloadedTreeItem.click();
    // 面板挂载（chapterId 有值时渲染）；reload 后需等待投影数据异步加载完成
    await expect(page.getByTestId('projection-panel')).toBeVisible({ timeout: 60000 });
    await page.getByTestId('projection-panel-toggle').click();
    await expect(page.getByTestId('projection-replay-facts')).toBeVisible({ timeout: 60000 });

    // 再次点击重试 → 再次收敛到 done（重复重放安全）
    await page.getByTestId('projection-replay-facts').click();
    await expect(page.getByTestId('projection-replay-facts')).toHaveCount(0, { timeout: 60000 });
    await expect(page.getByTestId('projection-failed-count')).toHaveCount(0);

    // 后端状态确认：facts=done（单条记录，无重复）
    const after = await request.get(
      `${BACKEND_BASE}/api/projects/${projectId}/chapters/${chapterId}/projections`,
    );
    const json = (await after.json()) as {
      data?: Array<{ projection_type: string; status: string }>;
    };
    const factsRows = (json.data || []).filter((p) => p.projection_type === 'facts');
    expect(factsRows).toHaveLength(1);
    expect(factsRows[0].status).toBe('done');
  });
});
