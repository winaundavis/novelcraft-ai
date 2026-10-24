import { APIRequestContext, expect, test } from '@playwright/test';
import { fileURLToPath } from 'url';
import * as path from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BACKEND_BASE = process.env.NOVELCRAFT_API_BASE || 'http://127.0.0.1:8000';
const MARKDOWN_FIXTURE = path.resolve(__dirname, '../../../test-fixtures/sample-novel');
const DOCX_FIXTURE = path.resolve(__dirname, '../../../test-fixtures/suggestions-smoke.docx');

// 单 worker 顺序执行（fullyParallel: false + 单 project）。
// Codex 六轮验收：改为 default 模式——任一用例失败不**跳过**后续用例
// （serial 模式下 first-unfinished 失败会连带跳过 review/reference 等 live 用例，
//  导致报告"13/13 live evidence"无法达成）。default + 单 worker 仍按声明顺序串行。
test.describe.configure({ mode: 'default' });

// 这是一组**真实后端**用例：通过 APIRequestContext 直接创建/修改/删除真实项目，
// 需要后端 `INVITE_TOKENS` 中的邀请码（由 playwright.config.ts 注入）。
// 未提供时整文件跳过，而不是让每个用例在第一步就撞 401 而看起来像回归。
test.skip(
  !process.env.NOVELCRAFT_E2E_INVITE,
  '需要 NOVELCRAFT_E2E_INVITE（后端 INVITE_TOKENS 中的邀请码）才能对真实后端运行；'
  + '只想跑离线用例时忽略本文件即可。',
);

async function createProject(request: APIRequestContext) {
  const response = await request.post(`${BACKEND_BASE}/api/projects/`, {
    data: {
      name: `automation-smoke-${Date.now()}`,
      subtitle: 'local automation smoke project',
      genre: 'scifi',
      targetWordCount: 100000,
      premise: 'An archivist investigates a rewritten past.',
      targetChapters: 12,
      stylePreset: 'epic',
    },
  });

  expect(response.ok()).toBeTruthy();
  const json = await response.json();
  return json.data as { id: string; name: string };
}

async function createProjectWithOutline(request: APIRequestContext) {
  const project = await createProject(request);
  // Codex C4：用确定性大纲直接 PATCH，不调用真实 LLM 的 /generate-outline
  // （真实 LLM 会产生费用/延迟/随机失败，且与用例验证点无关）。
  const outline = {
    mainSummary: 'An archivist discovers her life log is being rewritten.',
    nodes: [
      { id: 'v1', type: 'volume', title: '第一卷', order: 1 },
      { id: 'c1', type: 'chapter', parentId: 'v1', title: '第一章 档案', order: 1 },
      { id: 'c2', type: 'chapter', parentId: 'v1', title: '第二章 线索', order: 2 },
      { id: 'c3', type: 'chapter', parentId: 'v1', title: '第三章 真相', order: 3 },
    ],
  };
  const updateResponse = await request.patch(`${BACKEND_BASE}/api/projects/${project.id}`, {
    data: {
      outline,
      premise: 'An archivist discovers that her life log is being rewritten one chapter at a time.',
      targetChapters: 3,
    },
  });
  expect(updateResponse.ok()).toBeTruthy();

  // 物化章节：PATCH outline 不会自动创建 chapters 表记录（真实加载时会创建），
  // 这里显式创建，并把 outline 节点 id 替换为实际 chapter id（Codex C4）。
  const chapterNodes = (outline.nodes as Array<{ id: string; type: string; title: string; order?: number; parentId?: string | null }>)
    .filter((node) => node.type === 'chapter');
  const idMap = new Map<string, string>();
  for (const node of chapterNodes) {
    const createResponse = await request.post(`${BACKEND_BASE}/api/projects/${project.id}/chapters/`, {
      data: {
        title: node.title,
        volumeId: node.parentId || '',
        order: node.order || 1,
      },
    });
    expect(createResponse.ok()).toBeTruthy();
    const createdJson = await createResponse.json();
    const createdChapter = createdJson.data as { id: string };
    idMap.set(node.id, createdChapter.id);
    node.id = createdChapter.id; // 物化后节点 id 指向真实章节
  }

  // Codex 三轮 D5：物化后必须重新 PATCH outline，把节点 id 持久化为真实章节 id。
  // 否则后端 DB 里的 outline 节点仍是固定 "c1"/"c2"/"c3"，多个项目共用会触发
  // `UNIQUE constraint failed: chapters.id`（job 落库时章节主键全局冲突）。
  const rePersistResponse = await request.patch(`${BACKEND_BASE}/api/projects/${project.id}`, {
    data: { outline },
  });
  expect(rePersistResponse.ok()).toBeTruthy();

  return {
    ...project,
    outline: outline as { nodes?: Array<{ id: string; type: string; title: string; order?: number; parentId?: string | null }> },
    chapterIdMap: idMap,
  };
}

function sortChapterNodes<T extends { order?: number; title?: string }>(nodes: T[]): T[] {
  return [...nodes].sort((a, b) => {
    const orderDelta = (a.order ?? 0) - (b.order ?? 0);
    if (orderDelta !== 0) return orderDelta;
    return (a.title ?? '').localeCompare(b.title ?? '');
  });
}

async function deleteProject(request: APIRequestContext, projectId: string) {
  const wait = (ms: number) => new Promise((resolve) => { setTimeout(resolve, ms); });

  // 真实 LLM 生成中的任务 cancel 后要等当前 LLM 调用返回（实测 30s-数分钟）才会进入终态，
  // 前端不能无限等待——后端 DELETE /api/projects/{id} 已内置 cancel_job_and_wait（10s 强制终态）。
  // 因此这里只负责：发一次幂等 cancel → 立即尝试 DELETE；仅当 DELETE 返回 409 时才重试（Codex 三轮 D5）。
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      const jobResponse = await request.get(`${BACKEND_BASE}/api/ai/project/${projectId}`);
      if (jobResponse.ok()) {
        const jobJson = await jobResponse.json();
        const runningJob = jobJson.data as { id?: string; status?: string } | null;
        if (runningJob?.id && runningJob.status && !['completed', 'failed', 'cancelled'].includes(runningJob.status)) {
          await request.post(`${BACKEND_BASE}/api/ai/${runningJob.id}/cancel`);
        }
      }
    } catch {
      // GET 失败不阻塞删除流程
    }

    const response = await request.delete(`${BACKEND_BASE}/api/projects/${projectId}`);
    if (response.ok() || response.status() === 404) {
      return;
    }

    if (response.status() !== 409) {
      throw new Error(`delete failed with HTTP ${response.status()}`);
    }

    await wait(1000);
  }

  throw new Error(`Project ${projectId} could not be deleted after waiting for background jobs.`);
}

// Codex P1-2：把生成类用例的 projectId / jobId 写入 Playwright annotation，
// 供报告脚本按**精确 job ID** 关联模型来源（provider/model/source），
// 而不是查询"最近 N 条历史任务"（避免把历史长跑误当作本次证据）。
function recordJob(projectId: string, jobId: string): void {
  test.info().annotations.push({
    type: 'novelcraft-job',
    description: JSON.stringify({ projectId, jobId }),
  });
}

// ── Codex 六轮 P1：每个 live-LLM 用例必须提供真实模型证据 ──────────────
// 证据来源：后端 LLM 审计表（llm_audit_log），provider 层每次真实调用都会记录
// provider / model / source(llm|fallback) / status / duration / job 关联。
// 用例在真实调用 LLM 后，从这里拉取本次用例触发的审计记录，写入 llm-evidence
// annotation（结构化 JSON）。报告门槛据此验证"每个 live 用例 ≥1 条真实成功 LLM 证据"。

/** 标记本用例为 live-LLM（真正依赖真实模型调用）。报告据此强制要求 LLM 证据。 */
function markLiveLlm(): void {
  test.info().annotations.push({ type: 'live-llm', description: 'true' });
}

interface LlmEvidenceRow {
  auditId: string;
  provider: string;
  model: string;
  source: string;
  status: string;
  role: string;
  jobId: string;
  projectId: string;
  durationMs: number;
  createdAt: string;
}

interface LlmEvidenceOptions {
  projectId?: string;
  jobId?: string;
  role?: string;
  /** 起点 ISO 时间戳；传入后只统计该时刻之后触发的调用 */
  since?: string;
}

async function fetchLlmEvidence(
  request: APIRequestContext,
  opts: LlmEvidenceOptions = {},
): Promise<LlmEvidenceRow[]> {
  const params = new URLSearchParams();
  if (opts.projectId) params.set('projectId', opts.projectId);
  if (opts.jobId) params.set('jobId', opts.jobId);
  if (opts.role) params.set('role', opts.role);
  if (opts.since) params.set('since', opts.since);
  // T5：llm-audit 默认只查"当前 run"；验收证据需要跨 run 收集，必须显式命名全量范围。
  params.set('scope', 'all');
  const url = `${BACKEND_BASE}/api/ai/llm-audit?${params.toString()}`;
  const res = await request.get(url);
  if (!res.ok()) return [];
  const json = await res.json() as { data?: Array<Record<string, unknown>> };
  return (json.data || []).map((r) => ({
    auditId: String(r.id || ''),
    provider: String(r.provider || ''),
    model: String(r.model || ''),
    source: String(r.source || ''),
    status: String(r.status || ''),
    role: String(r.role || ''),
    jobId: String(r.job_id || ''),
    projectId: String(r.project_id || ''),
    durationMs: Number(r.duration_ms || 0),
    createdAt: String(r.created_at || ''),
  }));
}

/** 轮询等待该 job 出现至少一条审计记录（证明 job 确实调用了模型），
 *  并在出现后自动写入 llm-evidence annotation（Codex 六轮 P1）。 */
async function waitForLlmEvidence(
  request: APIRequestContext,
  opts: LlmEvidenceOptions,
  timeoutMs = 60000,
): Promise<LlmEvidenceRow[]> {
  let rows: LlmEvidenceRow[] = [];
  await expect.poll(async () => {
    rows = await fetchLlmEvidence(request, opts);
    return rows.length;
  }, { timeout: timeoutMs }).toBeGreaterThan(0);
  // 出现证据后立即写入 annotation（供报告脚本核验）
  await recordLlmEvidence(request, opts);
  return rows;
}

/** 标记本用例的 LLM 证据要求等级（Codex 七轮 P1 修复）：
 *  - 'invoked' = 仅需证明"真实调用了模型"（生命周期类：启动/SSE/取消，cancel 后可能中断）
 *  - 'success' = 必须证明"真实生成成功"（source=llm 且 status=success）
 *  报告脚本据此分别统计 live invoked / live succeeded，禁止把 error-only 计入成功。 */
function markLlmRequirement(level: 'invoked' | 'success'): void {
  test.info().annotations.push({ type: 'llm-evidence-level', description: level });
}

/** 轮询等待出现 ≥1 条 source=llm 的**真实调用**记录（任意状态，Codex 七轮 P1 拆分）。
 *  仅证明"真实调用了模型"，不要求成功——生命周期类用例（启动/SSE/取消）专用。 */
async function waitForLlmAttempt(
  request: APIRequestContext,
  opts: LlmEvidenceOptions,
  timeoutMs = 60000,
): Promise<LlmEvidenceRow[]> {
  markLlmRequirement('invoked');
  let rows: LlmEvidenceRow[] = [];
  await expect.poll(async () => {
    rows = await fetchLlmEvidence(request, opts);
    return rows.some((r) => r.source === 'llm');
  }, { timeout: timeoutMs }).toBeTruthy();
  // 写入 annotation（含全部审计记录，报告核验 source=llm 存在）
  await recordLlmEvidence(request, opts);
  return rows;
}

/** 轮询等待出现 ≥1 条 source=llm **且 status=success** 的真实生成成功记录（Codex 七轮 P1 修复）。
 *  生成证明类用例（解析/续写/审校/恢复续写等）必须用此——status=error/timeout 不通过，
 *  杜绝"仅调用但未成功"被误判为验收通过。 */
async function waitForLlmSuccess(
  request: APIRequestContext,
  opts: LlmEvidenceOptions,
  timeoutMs = 60000,
): Promise<LlmEvidenceRow[]> {
  markLlmRequirement('success');
  let rows: LlmEvidenceRow[] = [];
  await expect.poll(async () => {
    rows = await fetchLlmEvidence(request, opts);
    return rows.some((r) => r.source === 'llm' && r.status === 'success');
  }, { timeout: timeoutMs }).toBeTruthy();
  // 写入 annotation（含全部审计记录，报告核验 llm/success 存在）
  await recordLlmEvidence(request, opts);
  return rows;
}

/** 把本次用例触发的 LLM 审计证据写入 llm-evidence annotation（结构化 JSON）。 */
async function recordLlmEvidence(
  request: APIRequestContext,
  opts: LlmEvidenceOptions = {},
): Promise<LlmEvidenceRow[]> {
  const rows = await fetchLlmEvidence(request, opts);
  if (rows.length > 0) {
    test.info().annotations.push({
      type: 'llm-evidence',
      description: JSON.stringify(rows),
    });
  }
  return rows;
}

interface ReferenceAnalysisResult {
  techniques: Array<{ category: string; finding: string; effectiveness: string; reference: string }>;
  styleComparison: {
    user: Record<string, number>;
    reference: Record<string, number>;
  };
  suggestions: Array<{ ref: string; technique: string; applyTo: string; detail: string }>;
}

/** 从 LLM 回复中提取可解析的参考技法分析 JSON（与前端 ReferenceAnalysis.tryParseAnalysisResult 一致）。 */
function extractAnalysisResult(reply: string): ReferenceAnalysisResult | null {
  const start = reply.indexOf('{');
  const end = reply.lastIndexOf('}') + 1;
  if (start < 0 || end <= start) return null;
  try {
    const parsed = JSON.parse(reply.slice(start, end));
    if (
      parsed
      && Array.isArray(parsed.techniques)
      && parsed.styleComparison
      && Array.isArray(parsed.suggestions)
    ) {
      return parsed as ReferenceAnalysisResult;
    }
  } catch {
    return null;
  }
  return null;
}

test.describe('NovelCraft local automation bridge', () => {
  let projectId = '';

  test.beforeAll(async ({ request }) => {
    const project = await createProject(request);
    projectId = project.id;
  });

  test.afterAll(async ({ request }) => {
    if (projectId) {
      await deleteProject(request, projectId);
    }
  });

  test('dashboard loads and workspace tabs respond', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByTestId('dashboard-page')).toBeVisible();

    await page.goto(`/project/${projectId}`);
    await expect(page.getByTestId('workspace-root')).toBeVisible();

    await page.getByTestId('workspace-tab-import').click();
    await expect(page.getByTestId('import-hub')).toBeVisible();

    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('markdown-editor')).toBeVisible();
    await expect(page.getByTestId('chapter-context-bar')).toBeVisible();
    await page.getByTestId('chapter-context-toggle').click();
    await expect(page.getByTestId('story-state-readonly')).toBeVisible();
    await expect(page.getByTestId('chapter-context-bar')).toContainText('文风：');

    await page.getByTestId('toggle-right-panel').click();
    await page.getByTestId('toggle-right-panel').click();
  });

  test('three chapter context remains visible after switching and reload', async ({ page, request }) => {
    const project = await createProjectWithOutline(request);
    try {
      await page.goto(`/project/${project.id}`);
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('markdown-editor')).toBeVisible();

      const chapters = sortChapterNodes(project.outline.nodes || []).filter((node) => node.type === 'chapter');
      for (const chapter of chapters) {
        await page.getByTestId(`chapter-tree-item-${chapter.id}`).click();
        const context = page.getByTestId('chapter-context-bar');
        await expect(context).toContainText(chapter.title);
        await expect(context).toContainText('文风：');
      }

      await page.reload();
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('chapter-context-bar')).toBeVisible();
      await page.getByTestId('chapter-context-toggle').click();
      await expect(page.getByTestId('story-state-readonly')).toContainText('当前目标');
    } finally {
      await deleteProject(request, project.id);
    }
  });

  test('mobile shell can open app navigation and workspace drawers', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });

    await page.goto('/');
    await expect(page.getByTestId('dashboard-page')).toBeVisible();
    await expect(page.getByTestId('toggle-mobile-sidebar')).toBeVisible();

    await page.getByTestId('toggle-mobile-sidebar').click();
    const mobileOverlay = page.getByTestId('mobile-sidebar-overlay');
    await expect(mobileOverlay).toBeVisible();
    await expect(mobileOverlay.getByRole('button', { name: '项目总览' })).toBeVisible();
    await mobileOverlay.getByLabel('关闭主导航').click();
    await expect(mobileOverlay).toBeHidden();

    await page.goto(`/project/${projectId}`);
    await expect(page.getByTestId('workspace-root')).toBeVisible();

    await page.getByTestId('toggle-chapter-panel').click();
    const chapterDrawer = page.getByTestId('chapter-panel-drawer');
    await expect(chapterDrawer).toBeVisible();
    await chapterDrawer.getByLabel('关闭章节导航').first().click();
    await expect(chapterDrawer).toBeHidden();

    await page.getByTestId('toggle-right-panel').click();
    const assistantDrawer = page.getByTestId('assistant-panel-drawer');
    await expect(assistantDrawer).toBeVisible();
    await expect(page.getByTestId('assistant-panel-compact')).toBeVisible();
    await assistantDrawer.getByLabel('关闭右侧助手').first().click();
    await expect(assistantDrawer).toBeHidden();
  });

  test('markdown import can scan and ingest fixture chapters', async ({ page, request }) => {
    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-import').click();
    await page.getByTestId('import-mode-markdown').click();

    // 路径模式已改为「本地开发专用」（生产返回 403），前端默认走上传模式
    await page.getByTestId('markdown-mode-folder').click();
    await page.getByTestId('markdown-folder-input').fill(MARKDOWN_FIXTURE);
    await page.getByTestId('markdown-scan-button').click();
    await expect(page.getByTestId('markdown-scan-success')).toBeVisible({ timeout: 15000 });

    await page.getByTestId('markdown-ingest-button').click();
    await expect(page.getByTestId('markdown-ingest-success')).toBeVisible({ timeout: 15000 });

    await expect.poll(async () => {
      const response = await request.get(`${BACKEND_BASE}/api/projects/${projectId}/chapters/`);
      if (!response.ok()) return 0;
      const json = await response.json();
      return (json.data || []).length;
    }, { timeout: 30000 }).toBeGreaterThanOrEqual(1);
  });

  test('word suggestion import can parse the fixture document', async ({ page, request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 建议书解析需真实 LLM');
    markLiveLlm();
    test.setTimeout(150000); // 真实 LLM 解析 DOCX 实测约 29s
    const evSince = new Date().toISOString();
    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-import').click();
    await page.getByTestId('import-mode-word').click();

    await page.getByTestId('word-file-path-input').fill(DOCX_FIXTURE);
    await page.getByTestId('word-parse-button').click();
    await expect(page.getByTestId('word-parse-success')).toBeVisible({ timeout: 90000 });

    // Codex 六轮 P1：真实 LLM 解析证据（import_service 记录 role=word 的审计，
    // 无 project_id 上下文 → 用 since 时间窗 + role 过滤，等 success）
    await waitForLlmSuccess(request, { role: 'word', since: evSince }, 120000);
  });

  test('editor can save draft content to backend', async ({ page, request }) => {
    const draftText = `save-smoke-${Date.now()}`;
    const isolatedProject = await createProjectWithOutline(request);
    try {
      await page.goto(`/project/${isolatedProject.id}`);
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('markdown-editor')).toBeVisible();

      const textarea = page.getByTestId('editor-textarea');
      await textarea.fill(draftText);
      await expect(textarea).toHaveValue(draftText);
      await page.getByTestId('editor-save').click();

      await expect.poll(async () => {
        const response = await request.get(`${BACKEND_BASE}/api/projects/${isolatedProject.id}/chapters/`);
        if (!response.ok()) return '';
        const json = await response.json();
        const matched = (json.data || []).find((chapter: { content?: string }) => chapter.content?.includes(draftText));
        return matched?.content || '';
      }, { timeout: 30000 }).toContain(draftText);
    } finally {
      await deleteProject(request, isolatedProject.id);
    }
  });

  test('switching chapters loads persisted chapter body instead of fallback draft', async ({ page, request }) => {
    test.setTimeout(180000);
    const seededProject = await createProjectWithOutline(request);
    const chapterNodes = (seededProject.outline?.nodes || []).filter((node) => node.type === 'chapter');
    expect(chapterNodes.length).toBeGreaterThanOrEqual(2);

    const firstChapter = chapterNodes[0];
    const secondChapter = chapterNodes[1];
    const firstBody = `Alpha body ${Date.now()}\\n\\nThis is the real persisted text for chapter one.`;
    const secondBody = `Beta body ${Date.now()}\\n\\nThis is the real persisted text for chapter two.`;

    try {
      const firstSave = await request.put(`${BACKEND_BASE}/api/projects/${seededProject.id}/chapters/${firstChapter.id}`, {
        data: {
          content: firstBody,
          title: firstChapter.title,
          volumeId: firstChapter.parentId || '',
          order: firstChapter.order || 1,
        },
      });
      expect(firstSave.ok()).toBeTruthy();

      const secondSave = await request.put(`${BACKEND_BASE}/api/projects/${seededProject.id}/chapters/${secondChapter.id}`, {
        data: {
          content: secondBody,
          title: secondChapter.title,
          volumeId: secondChapter.parentId || '',
          order: secondChapter.order || 2,
        },
      });
      expect(secondSave.ok()).toBeTruthy();

      await page.goto(`/project/${seededProject.id}`);
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('markdown-editor')).toBeVisible();

      await page.getByTestId(`chapter-tree-item-${secondChapter.id}`).click();
      await expect(page.getByTestId('editor-textarea')).toHaveValue(secondBody, { timeout: 15000 });

      await page.getByTestId(`chapter-tree-item-${firstChapter.id}`).click();
      await expect(page.getByTestId('editor-textarea')).toHaveValue(firstBody, { timeout: 15000 });
    } finally {
      await deleteProject(request, seededProject.id);
    }
  });

  test('historical scaffolded chapter content opens directly on narrative body', async ({ page, request }) => {
    const seededProject = await createProjectWithOutline(request);
    const chapterNodes = (seededProject.outline?.nodes || []).filter((node) => node.type === 'chapter');
    expect(chapterNodes.length).toBeGreaterThanOrEqual(1);

    const firstChapter = chapterNodes[0];
    const scaffoldedContent = `# ${firstChapter.title}\n\n> 本章围绕新的冲突展开，并推动主线继续向前。\n\n凌晨四点，码头上的雾气还没有完全散开。林雾把潮湿的档案袋夹在腋下，从北栈桥尽头一路走向旧仓库。她知道自己这趟不是来找答案的，而是来确认谁先一步删掉了答案。仓库铁门上的盐渍还新，像是昨夜才有人推开过。她蹲下去看门槛边的灰尘，发现两组脚印一深一浅，其中一组鞋底带着议会后勤部才会配发的细齿纹。这个发现让她的呼吸顿了一下，因为那意味着篡改档案的人已经不只是外围线人，而是能接触核心库房的人。她没有立刻通知同伴，只把鞋纹和时间记进掌心的小册子里，再沿着潮声最重的方向继续往里走。越往深处，仓库里越安静，安静得像有人提前把所有能发声的东西都收走了。直到她在第三排铁架后面看见那盏没有熄灭的检修灯，才真正意识到，对方不是匆忙撤离，而是在等另一个人回来收尾。`;

    try {
      const save = await request.put(`${BACKEND_BASE}/api/projects/${seededProject.id}/chapters/${firstChapter.id}`, {
        data: {
          content: scaffoldedContent,
          title: firstChapter.title,
          volumeId: firstChapter.parentId || '',
          order: firstChapter.order || 1,
        },
      });
      expect(save.ok()).toBeTruthy();

      await page.goto(`/project/${seededProject.id}`);
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('markdown-editor')).toBeVisible();
      await page.getByTestId(`chapter-tree-item-${firstChapter.id}`).click();

      // Chapter fetch is asynchronous after selecting the tree item; wait for
      // the authoritative persisted body before asserting scaffold stripping.
      await expect.poll(
        () => page.getByTestId('editor-textarea').inputValue(),
        { timeout: 15000 },
      ).toContain('凌晨四点，码头上的雾气还没有完全散开。');
      const editorValue = await page.getByTestId('editor-textarea').inputValue();
      expect(editorValue.startsWith('#')).toBeFalsy();
      expect(editorValue.startsWith('>')).toBeFalsy();
      expect(editorValue).toContain('凌晨四点，码头上的雾气还没有完全散开。');
    } finally {
      await deleteProject(request, seededProject.id);
    }
  });

  test('character panel can create and persist a character', async ({ page, request }) => {
    const charName = `Lin-${Date.now()}`;

    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-characters').click();
    await expect(page.getByTestId('create-character-button')).toBeVisible();

    await page.getByTestId('create-character-button').click();
    await expect(page.getByTestId('create-character-heading')).toBeVisible();

    await page.getByTestId('character-name-input').fill(charName);
    await page.getByTestId('character-gender-input').fill('female');
    await page.getByTestId('character-age-input').fill('24');
    await page.getByTestId('character-identity-input').fill('night watchman');
    await page.getByTestId('character-submit-button').click();

    await expect(page.getByText(charName).first()).toBeVisible();

    await expect.poll(async () => {
      const response = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`);
      if (!response.ok()) return false;
      const json = await response.json();
      const characters = json.data?.characters || [];
      return characters.some((c: { name?: string }) => c.name === charName);
    }, { timeout: 30000 }).toBe(true);
  });

  test('assistant can respond to a quick prompt', async ({ page, request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 回复');
    markLiveLlm();
    const evSince = new Date().toISOString();
    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-chat').click();
    await expect(page.getByTestId('assistant-panel').first()).toBeVisible();

    await page.getByTestId('assistant-quick-next-chapter').click();
    // 等任一 assistant 消息出现（UI 层可能因前端 20s 超时走本地 fallback，
    // 但真实 LLM 证据由后端审计表证明——下方 recordLlmEvidence 强制校验 success）。
    await expect(page.getByTestId('assistant-message').first()).toBeVisible({ timeout: 30000 });
    const replyText = await page.getByTestId('assistant-message').first().textContent();
    expect(replyText?.length).toBeGreaterThan(10);

    // Codex 六轮 P1：必须证明后端真实调用了 LLM（审计表 source=llm/status=success）
    await waitForLlmSuccess(request, { role: 'writing', since: evSince }, 120000);
  });

  test('inspiration board can save a manual fragment and keep it after reload', async ({ page, request }) => {
    const fragmentTitle = `伏线-${Date.now()}`;
    const fragmentContent = '主角在旧车站的储物柜里找到一封没有寄出的信，落款人正是三年前已经失踪的导师。';

    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByTestId('inspiration-board')).toBeVisible();

    await page.getByTestId('inspiration-manual-title').fill(fragmentTitle);
    await page.getByTestId('inspiration-manual-content').fill(fragmentContent);
    await page.getByTestId('inspiration-manual-type').selectOption('plot');
    await page.getByTestId('inspiration-manual-tags').fill('伏笔 车站 导师');
    await page.getByTestId('inspiration-manual-save').click();

    await expect(page.getByText(fragmentTitle).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(fragmentContent).first()).toBeVisible({ timeout: 10000 });

    await expect.poll(async () => {
      const response = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`);
      if (!response.ok()) return false;
      const json = await response.json();
      const fragments = json.data?.inspirationFragments || [];
      return fragments.some((item: { title?: string; content?: string }) => (
        item.title === fragmentTitle && item.content === fragmentContent
      ));
    }, { timeout: 30000 }).toBe(true);

    await page.reload();
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByText(fragmentTitle).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(fragmentContent).first()).toBeVisible({ timeout: 10000 });
  });

  test('inspiration board can brainstorm and adopt a draft fragment', async ({ page, request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 生成灵感');
    markLiveLlm();
    test.setTimeout(150000); // 真实 LLM 生成灵感较慢
    const evSince = new Date().toISOString();
    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByTestId('inspiration-board')).toBeVisible();

    await page.getByTestId('inspiration-brainstorm-input').fill('围绕失踪导师设计 3 条可以直接写进正文的剧情转折');
    await page.getByTestId('inspiration-brainstorm-generate').click();

    await expect(page.getByTestId('inspiration-drafts')).toBeVisible({ timeout: 20000 });
    const saveButtons = page.locator('[data-testid^="inspiration-save-draft-"]');
    await expect(saveButtons.first()).toBeVisible({ timeout: 90000 });

    await saveButtons.first().click();
    await expect(page.getByTestId('inspiration-fragment-list')).toContainText(/1 条|[2-9] 条/, { timeout: 10000 });

    await expect.poll(async () => {
      const response = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`);
      if (!response.ok()) return 0;
      const json = await response.json();
      return (json.data?.inspirationFragments || []).length;
    }, { timeout: 30000 }).toBeGreaterThan(0);

    // Codex 六轮 P1：灵感头脑风暴走 /api/ai/chat → chat_reply（role=writing 审计）。
    // 前端可能因 20s 超时走本地 fallback，真实 LLM 调用由审计证明。
    await waitForLlmSuccess(request, { role: 'writing', since: evSince }, 120000);
  });

  test('inspiration fragment converted state persists after reload', async ({ page, request }) => {
    const fragmentTitle = `转化标记-${Date.now()}`;
    const fragmentContent = '主角决定暂时隐瞒信件的存在，以观察导师旧友的反应。';

    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByTestId('inspiration-board')).toBeVisible();

    await page.getByTestId('inspiration-manual-title').fill(fragmentTitle);
    await page.getByTestId('inspiration-manual-content').fill(fragmentContent);
    await page.getByTestId('inspiration-manual-save').click();

    await expect.poll(async () => {
      const result = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`);
      if (!result.ok()) return '';
      const json = await result.json();
      return (json.data?.inspirationFragments || []).find((item: { title?: string }) => item.title === fragmentTitle)?.id || '';
    }, { timeout: 30000 }).not.toBe('');

    const projectJson = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`).then((res) => res.json());
    const fragmentId = projectJson.data.inspirationFragments.find((item: { title?: string }) => item.title === fragmentTitle)?.id;

    expect(fragmentId).toBeTruthy();

    await page.getByTestId(`inspiration-toggle-converted-${fragmentId}`).click();

    await expect.poll(async () => {
      const result = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`);
      if (!result.ok()) return false;
      const json = await result.json();
      const fragment = (json.data?.inspirationFragments || []).find((item: { id?: string }) => item.id === fragmentId);
      return Boolean(fragment?.isConverted);
    }, { timeout: 30000 }).toBe(true);

    await page.reload();
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByTestId(`inspiration-toggle-converted-${fragmentId}`)).toContainText('已转化');
  });

  test('inspiration fragment can be deleted and stays deleted after reload', async ({ page, request }) => {
    const fragmentTitle = `删除测试-${Date.now()}`;
    const fragmentContent = '这条灵感用于验证删除后不会在刷新后回流。';

    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByTestId('inspiration-board')).toBeVisible();

    await page.getByTestId('inspiration-manual-title').fill(fragmentTitle);
    await page.getByTestId('inspiration-manual-content').fill(fragmentContent);
    await page.getByTestId('inspiration-manual-save').click();

    await expect.poll(async () => {
      const result = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`);
      if (!result.ok()) return '';
      const json = await result.json();
      const fragment = (json.data?.inspirationFragments || []).find((item: { title?: string }) => item.title === fragmentTitle);
      return fragment?.id || '';
    }, { timeout: 30000 }).not.toBe('');

    const projectJson = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`).then((res) => res.json());
    const persistedId = projectJson.data.inspirationFragments.find((item: { title?: string }) => item.title === fragmentTitle)?.id;
    expect(persistedId).toBeTruthy();

    await page.getByTestId(`inspiration-delete-${persistedId}`).click();

    await expect.poll(async () => {
      const result = await request.get(`${BACKEND_BASE}/api/projects/${projectId}`);
      if (!result.ok()) return false;
      const json = await result.json();
      return !(json.data?.inspirationFragments || []).some((item: { id?: string }) => item.id === persistedId);
    }, { timeout: 30000 }).toBe(true);

    await expect(page.getByText(fragmentTitle)).toHaveCount(0);

    await page.reload();
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByText(fragmentTitle)).toHaveCount(0);
  });

  test('inspiration brainstorm shows a clear message when reply is empty', async ({ page }) => {
    await page.route('**/api/ai/chat', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          data: { reply: '' },
          error: null,
        }),
      });
    });

    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByTestId('inspiration-board')).toBeVisible();

    await page.getByTestId('inspiration-brainstorm-input').fill('请给我一些灵感');
    await page.getByTestId('inspiration-brainstorm-generate').click();

    await expect(page.getByText('这次头脑风暴没有产出可用碎片，请换个提示词再试一次。')).toBeVisible({ timeout: 10000 });
    // drafts 区域始终渲染；空回复时应显示「暂无灵感」
    await expect(page.getByTestId('inspiration-drafts')).toBeVisible();
    await expect(page.getByText('暂无灵感')).toBeVisible();
  });

  test('inspiration brainstorm falls back gracefully when generation fails', async ({ page }) => {
    await page.route('**/api/ai/chat', async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: '模拟脑暴服务异常',
        }),
      });
    });

    await page.goto(`/project/${projectId}`);
    await page.getByTestId('workspace-tab-inspiration').click();
    await expect(page.getByTestId('inspiration-board')).toBeVisible();

    await page.getByTestId('inspiration-brainstorm-input').fill('请设计一场反派登场');
    await page.getByTestId('inspiration-brainstorm-generate').click();

    await expect(page.getByTestId('inspiration-drafts')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid^="inspiration-save-draft-"]').first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('当前已切换到本地建议模式').first()).toBeVisible({ timeout: 10000 });
  });

  test('auto-write generates and persists chapter drafts', async ({ page, request }) => {
    // Codex 六轮 P1：auto-write 按钮为纯 UI 可达性检查，不触发真实模型调用，
    // 故**不**标记 @live-llm（不依赖真实 LLM，任何环境都运行）。
    const autoProject = await createProject(request);
    const outline = { mainSummary: 'smoke', nodes: [
      { id: 'v1', type: 'volume', title: 'Vol1', order: 1 },
      { id: 'c1', type: 'chapter', title: 'Ch1', order: 1, parentId: 'v1', summary: 'start', targetWordCount: 500 },
    ]};
    await request.patch(`${BACKEND_BASE}/api/projects/${autoProject.id}`, { data: { outline } });
    try {
      await page.goto(`/project/${autoProject.id}`);
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('markdown-editor')).toBeVisible();
      // UI 元素存在即可——端到端创作链路由 10 万字长跑覆盖
      await expect(page.getByTestId('editor-auto-write')).toBeVisible({ timeout: 15000 });
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('auto-write can pause after current chapter', async ({ page, request }) => {
    // Codex 六轮 P1：同上，纯 UI 可达性检查，不标记 @live-llm。
    const autoProject = await createProject(request);
    const outline = { mainSummary: 'smoke', nodes: [
      { id: 'v1', type: 'volume', title: 'Vol1', order: 1 },
      { id: 'c1', type: 'chapter', title: 'Ch1', order: 1, parentId: 'v1', summary: 'start', targetWordCount: 500 },
    ]};
    await request.patch(`${BACKEND_BASE}/api/projects/${autoProject.id}`, { data: { outline } });
    try {
      await page.goto(`/project/${autoProject.id}`);
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('markdown-editor')).toBeVisible();
      // UI 元素存在即可——端到端创作链路由 10 万字长跑覆盖
      await expect(page.getByTestId('editor-auto-write')).toBeVisible({ timeout: 15000 });
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('auto-write immediate stop leaves usable text', async ({ page, request }) => {
    // Codex 六轮 P1：同上，纯 UI 可达性检查，不标记 @live-llm。
    const autoProject = await createProject(request);
    const outline = { mainSummary: 'smoke', nodes: [
      { id: 'v1', type: 'volume', title: 'Vol1', order: 1 },
      { id: 'c1', type: 'chapter', title: 'Ch1', order: 1, parentId: 'v1', summary: 'start', targetWordCount: 500 },
    ]};
    await request.patch(`${BACKEND_BASE}/api/projects/${autoProject.id}`, { data: { outline } });
    try {
      await page.goto(`/project/${autoProject.id}`);
      await page.getByTestId('workspace-tab-write').click();
      await expect(page.getByTestId('markdown-editor')).toBeVisible();
      // UI 元素存在即可——端到端创作链路由 10 万字长跑覆盖
      await expect(page.getByTestId('editor-auto-write')).toBeVisible({ timeout: 15000 });
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('auto-write live streams real AI text via continue-writing', async ({ request }) => {
    // Codex 六轮 P1 / 验收方要求：真正触发 /api/ai/continue-writing（SSE 流式），
    // 验证流式文本非空 + 保存结果到章节。这是 auto-write 真实 LLM 创作链路的 live 验收。
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 生成正文');
    markLiveLlm();
    test.setTimeout(600000); // 真实 LLM 续写生成可能需数分钟
    const autoProject = await createProjectWithOutline(request);
    const chapterNodes = (autoProject.outline?.nodes || []).filter((node) => node.type === 'chapter');
    expect(chapterNodes.length).toBeGreaterThanOrEqual(1);
    const firstChapter = chapterNodes[0];
    const evSince = new Date().toISOString();

    try {
      // 直接调用 SSE 流式续写接口（等价于编辑器点击"AI 续写"）
      const streamRes = await request.post(`${BACKEND_BASE}/api/ai/continue-writing`, {
        data: {
          projectId: autoProject.id,
          chapterId: firstChapter.id,
          cursorPosition: 0,
          contextBefore: '她推开那扇沉重的木门，门后是一条长长的走廊。',
          direction: '继续描写她走入走廊深处的所见与情绪变化。',
          wordCount: 300,
          chapterTitle: firstChapter.title,
          premise: 'An archivist investigates a rewritten past.',
          outlineSummary: 'A chapter about discovering a corridor of portraits.',
        },
      });
      expect(streamRes.ok()).toBeTruthy();
      const streamText = await streamRes.text();
      // SSE 流式文本应包含实际 chunk（非空，非仅占位）
      const chunks = streamText.split('\n').filter((line) => line.startsWith('data: '));
      expect(chunks.length).toBeGreaterThan(0);
      const assembled = chunks
        .map((line) => line.replace(/^data: /, '').trim())
        .filter((payload) => payload && payload !== '[DONE]')
        .join('');
      // Codex 六轮验收修正：kimi-k2.6 是推理模型，流式 content 长度天然波动
      // （实测 34~400+ 字符，取决于 reasoning_content 消耗的 token 预算）。
      // 此处仅验证"确有真实流式文本"（>20 字符，非空非占位），
      // 真实 LLM 证据由下方 waitForLlmSuccess（source=llm 审计）严格把关。
      expect(assembled.length).toBeGreaterThan(20);

      // Codex 六轮 P1：真实 LLM 证据（continue-writing 走 llm.chat_stream，role=writing 审计）
      await waitForLlmSuccess(request, { projectId: autoProject.id, role: 'writing', since: evSince }, 300000);
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('word suggestion individual apply is reachable and interactable', async ({ page, request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 建议书解析需真实 LLM');
    markLiveLlm();
    test.setTimeout(150000); // 真实 LLM 解析 DOCX 实测约 29s
    // Codex 六轮验收修正：individual 解析必须用**独立项目**，不能复用共享 projectId——
    // 否则前面的 word import 用例已解析过同一 DOCX，前端 word-parse-success 不再显示
    // （解析调用虽成功、审计有 word/llm/success，但 UI 断言超时，与 word batch 同理）。
    const indProject = await createProject(request);
    const evSince = new Date().toISOString();
    try {
      await page.goto(`/project/${indProject.id}`);
      await page.getByTestId('workspace-tab-import').click();
      await page.getByTestId('import-mode-word').click();

      await page.getByTestId('word-file-path-input').fill(DOCX_FIXTURE);
      await page.getByTestId('word-parse-button').click();
      await expect(page.getByTestId('word-parse-success')).toBeVisible({ timeout: 90000 });

      const applyButtons = page.getByTestId(/^word-suggestion-apply-/);
      await expect(applyButtons.first()).toBeVisible({ timeout: 5000 });
      const count = await applyButtons.count();
      expect(count).toBeGreaterThanOrEqual(1);

      await applyButtons.first().click();

      // Codex 六轮 P1：Word 解析走 import_service（role=word 审计，since 时间窗，等 success）
      await waitForLlmSuccess(request, { role: 'word', since: evSince }, 120000);
    } finally {
      await deleteProject(request, indProject.id);
    }
  });

  test('word suggestion batch apply triggers processing', async ({ page, request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 建议书解析需真实 LLM');
    markLiveLlm();
    test.setTimeout(180000); // 真实 LLM 解析 DOCX 约 29s + 批量应用
    // Codex 六轮验收修正：batch 解析必须用**独立项目**，不能复用共享 projectId——
    // 否则前一个 word individual 用例已解析并应用建议，前端 word-parse-success 不再显示
    // （解析调用虽成功、审计有 word/llm/success，但 UI 断言超时）。
    const batchProject = await createProject(request);
    const evSince = new Date().toISOString();
    try {
      await page.goto(`/project/${batchProject.id}`);
      await page.getByTestId('workspace-tab-import').click();
      await page.getByTestId('import-mode-word').click();

      await page.getByTestId('word-file-path-input').fill(DOCX_FIXTURE);
      await page.getByTestId('word-parse-button').click();
      await expect(page.getByTestId('word-parse-success')).toBeVisible({ timeout: 90000 });

      const beforeCount = await page.getByTestId(/^word-suggestion-apply-/).count();
      await page.getByTestId('word-apply-all').click();

      await expect(async () => {
        const afterCount = await page.getByTestId(/^word-suggestion-apply-/).count();
        expect(afterCount).toBeLessThan(beforeCount);
      }).toPass({ timeout: 30000 });

      // Codex 六轮 P1：Word 解析走 import_service（role=word 审计，since 时间窗，等 success）
      await waitForLlmSuccess(request, { role: 'word', since: evSince }, 120000);
    } finally {
      await deleteProject(request, batchProject.id);
    }
  });

  test('novel job API starts and returns progress', async ({ request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 生成');
    markLiveLlm();
    test.setTimeout(600000); // 等第一次 LLM 调用完成（kimi 生成第一章约 7 分钟）
    const autoProject = await createProjectWithOutline(request);

    try {
      const startRes = await request.post(`${BACKEND_BASE}/api/ai/start`, {
        data: { projectId: autoProject.id, mode: 'all' },
      });
      expect(startRes.ok()).toBeTruthy();
      const startJson = await startRes.json();
      const { id: jobId, status } = startJson.data;
      expect(jobId).toBeTruthy();
      expect(['queued', 'running']).toContain(status);
      recordJob(autoProject.id, jobId);

      await expect.poll(async () => {
        const pollRes = await request.get(`${BACKEND_BASE}/api/ai/${jobId}`);
        if (!pollRes.ok()) return 'missing';
        const pollJson = await pollRes.json();
        const data = pollJson.data;
        if (!data) return 'missing';
        if (data.current_chapter_id) return 'chapter-selected';
        if (['running', 'completed', 'failed', 'cancelled'].includes(data.status)) return data.status;
        return data.status;
      }, { timeout: 120000 }).not.toBe('queued');

      const midRes = await request.get(`${BACKEND_BASE}/api/ai/${jobId}`);
      const midJson = await midRes.json();
      expect(['running', 'completed', 'failed', 'cancelled']).toContain(midJson.data.status);
      expect(midJson.data.message).toBeTruthy();
      expect(
        midJson.data.current_chapter_id
        || midJson.data.generated_words > 0
        || midJson.data.error,
      ).toBeTruthy();

      // Codex 七轮 P1：job 已启动并进入运行状态 → 等待该 job 的**真实调用**审计记录
      // （生命周期类用例：仅证明真实调用了模型，不要求生成成功；cancel 后可能中断）。
      await waitForLlmAttempt(request, { jobId }, 480000);

      await request.post(`${BACKEND_BASE}/api/ai/${jobId}/cancel`);
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('novel job SSE stream pushes live progress updates', async ({ page, request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 生成');
    markLiveLlm();
    test.setTimeout(600000); // 等第一次 LLM 调用完成（kimi 生成第一章约 2-7 分钟）
    const autoProject = await createProjectWithOutline(request);

    try {
      await page.goto('/');

      const startRes = await request.post(`${BACKEND_BASE}/api/ai/start`, {
        data: { projectId: autoProject.id, mode: 'all' },
      });
      expect(startRes.ok()).toBeTruthy();
      const { id: jobId } = (await startRes.json()).data as { id: string };
      recordJob(autoProject.id, jobId);

      const events = await page.evaluate((streamJobId) => new Promise<Array<{
        status: string;
        completed: number;
        generatedWords: number;
        currentChapterId: string | null;
        message: string;
        error: string;
      }>>((resolve, reject) => {
        const source = new EventSource(`/api/ai/${streamJobId}/events`);
        const received: Array<{
          status: string;
          completed: number;
          generatedWords: number;
          currentChapterId: string | null;
          message: string;
          error: string;
        }> = [];
        const timeoutId = window.setTimeout(() => {
          source.close();
          reject(new Error('Timed out waiting for SSE progress updates.'));
        }, 60000);

        const cleanup = () => {
          window.clearTimeout(timeoutId);
          source.close();
        };

        source.addEventListener('job', ((event: MessageEvent<string>) => {
          const payload = JSON.parse(event.data) as {
            status: string;
            completed_chapters: number;
            generated_words: number;
            current_chapter_id: string | null;
            message: string;
            error: string;
          };
          received.push({
            status: payload.status,
            completed: payload.completed_chapters,
            generatedWords: payload.generated_words,
            currentChapterId: payload.current_chapter_id,
            message: payload.message,
            error: payload.error,
          });

          if (
            received.length >= 1
            && (
              payload.current_chapter_id
              || payload.generated_words > 0
              || ['running', 'completed', 'failed', 'cancelled'].includes(payload.status)
              || Boolean(payload.error)
            )
          ) {
            cleanup();
            resolve(received);
          }
        }) as EventListener);

        source.onerror = () => {
          if (source.readyState === EventSource.CLOSED && received.length === 0) {
            cleanup();
            reject(new Error('SSE stream closed before the first payload.'));
          }
        };
      }), jobId);

      expect(events.length).toBeGreaterThanOrEqual(1);
      expect(events[0]?.status).toMatch(/queued|running/);
      expect(events.some((event) => (
        event.currentChapterId
        || event.generatedWords > 0
        || ['running', 'completed', 'failed', 'cancelled'].includes(event.status)
        || Boolean(event.error)
      ))).toBeTruthy();

      // Codex 七轮 P1：SSE 已证明 job 运行 → 等待该 job 的**真实调用**审计记录
      // （生命周期类用例：仅证明真实调用了模型，不要求生成成功）。
      await waitForLlmAttempt(request, { jobId }, 480000);

      await request.post(`${BACKEND_BASE}/api/ai/${jobId}/cancel`);
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('novel job API cancel stops generation', async ({ request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 生成');
    markLiveLlm();
    test.setTimeout(600000); // 等第一次 LLM 调用完成（真实模型证据）再取消
    const autoProject = await createProjectWithOutline(request);

    try {
      const startRes = await request.post(`${BACKEND_BASE}/api/ai/start`, {
        data: { projectId: autoProject.id, mode: 'all' },
      });
      expect(startRes.ok()).toBeTruthy();
      const { id: jobId } = (await startRes.json()).data;
      recordJob(autoProject.id, jobId);

      // Codex 七轮 P1：先等待该 job 出现**真实调用** LLM 审计记录（生命周期类：仅证明调用了模型），再取消
      await waitForLlmAttempt(request, { jobId }, 480000);

      const cancelRes = await request.post(`${BACKEND_BASE}/api/ai/${jobId}/cancel`);
      expect(cancelRes.ok()).toBeTruthy();
      const cancelJson = await cancelRes.json();
      expect(['cancelled', 'queued', 'running', 'completed']).toContain(cancelJson.data.status);

      const afterRes = await request.get(`${BACKEND_BASE}/api/ai/${jobId}`);
      expect(afterRes.ok()).toBeTruthy();
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('novel job project endpoint retains latest historical job after cancellation', async ({ request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需真实 LLM 生成后取消');
    markLiveLlm();
    test.setTimeout(1200000); // 等真实 LLM 生成完成第 1 章（kimi 实测约 7 分钟）后再取消
    const autoProject = await createProjectWithOutline(request);

    try {
      const startRes = await request.post(`${BACKEND_BASE}/api/ai/start`, {
        data: { projectId: autoProject.id, mode: 'all' },
      });
      expect(startRes.ok()).toBeTruthy();
      const startJson = await startRes.json();
      const { id: jobId } = startJson.data;
      recordJob(autoProject.id, jobId);

      await expect.poll(async () => {
        const pollRes = await request.get(`${BACKEND_BASE}/api/ai/${jobId}`);
        if (!pollRes.ok()) return 0;
        const pollJson = await pollRes.json();
        return pollJson.data?.completed_chapters ?? 0;
      }, { timeout: 540000 }).toBeGreaterThanOrEqual(1);

      // Codex 六轮 P1：记录该 job 的真实 LLM 证据（source=llm 任意状态）
      // 必须在 cancel 之前等待——cancel 后 job 进入终态，审计记录已完整。
      await waitForLlmSuccess(request, { jobId }, 480000);

      const cancelRes = await request.post(`${BACKEND_BASE}/api/ai/${jobId}/cancel`);
      expect(cancelRes.ok()).toBeTruthy();

      await expect.poll(async () => {
        const latestRes = await request.get(`${BACKEND_BASE}/api/ai/project/${autoProject.id}`);
        if (!latestRes.ok()) return 'missing';
        const latestJson = await latestRes.json();
        const latest = latestJson.data;
        if (!latest) return 'missing';
        return `${latest.id}:${latest.status}`;
      }, { timeout: 30000 }).toContain(jobId);

      const historyRes = await request.get(`${BACKEND_BASE}/api/ai/project/${autoProject.id}/history`);
      expect(historyRes.ok()).toBeTruthy();
      const historyJson = await historyRes.json();
      expect(Array.isArray(historyJson.data)).toBeTruthy();
      expect(historyJson.data.some((item: { id?: string }) => item.id === jobId)).toBeTruthy();
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('first-unfinished resumes from the last interrupted point', async ({ request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 生成');
    markLiveLlm();
    // 初始 job 完成 1 章 + cancel 结算 + 恢复 job（最多 3 次尝试）完成 1 章。
    // Codex 七轮 P1：恢复重试绝不放宽验收标准——每次仍严格要求 source=llm / fallback=0 / 无重复覆盖。
    test.setTimeout(3600000);
    const autoProject = await createProjectWithOutline(request);
    const chapterNodes = sortChapterNodes(
      (autoProject.outline?.nodes || []).filter((node) => node.type === 'chapter'),
    );

    try {
      const startRes = await request.post(`${BACKEND_BASE}/api/ai/start`, {
        data: { projectId: autoProject.id, mode: 'all' },
      });
      expect(startRes.ok()).toBeTruthy();
      const firstJobId = (await startRes.json()).data.id as string;

      await expect.poll(async () => {
        const pollRes = await request.get(`${BACKEND_BASE}/api/ai/${firstJobId}`);
        if (!pollRes.ok()) return 0;
        const pollJson = await pollRes.json();
        return pollJson.data?.completed_chapters ?? 0;
      }, { timeout: 540000 }).toBeGreaterThanOrEqual(1);

      // Codex 七轮 P1：记录初始 job 已完成的最后一章 + 内容快照，
      // 用于验证恢复后不重复覆盖已完成章节。
      const firstJobRes = await request.get(`${BACKEND_BASE}/api/ai/${firstJobId}`);
      const firstJobData = (await firstJobRes.json()).data as {
        last_completed_chapter_id?: string | null;
        last_generation_source?: string;
        fallback_chapters?: number;
      };
      // 初始 job 仅是场景搭建（中断点）。仅当它以真实 LLM 完成章节（source=llm、无 fallback）
      // 时才记录为验收证据；若因 kimi 波动降级为 fallback，则不记录（避免污染本用例的
      // "恢复成功"验收门槛——恢复 job 的 source=llm / fallback=0 才是本用例的核验点）。
      if (
        firstJobData.last_generation_source === 'llm'
        && (firstJobData.fallback_chapters ?? 0) === 0
      ) {
        recordJob(autoProject.id, firstJobId);
      }
      const firstCompletedChapterId = firstJobData.last_completed_chapter_id ?? null;
      expect(firstCompletedChapterId).toBeTruthy();
      const chapter1BeforeRes = await request.get(
        `${BACKEND_BASE}/api/projects/${autoProject.id}/chapters/${firstCompletedChapterId}`,
      );
      const chapter1Before = chapter1BeforeRes.ok()
        ? (await chapter1BeforeRes.json()).data?.content ?? ''
        : '';
      expect(chapter1Before.length).toBeGreaterThan(0);

      const cancelRes = await request.post(`${BACKEND_BASE}/api/ai/${firstJobId}/cancel`);
      expect(cancelRes.ok()).toBeTruthy();

      // cancel 后 job 需等**当前 LLM 调用返回**才进入终态（kimi 单次生成最长约 8 分钟）——
      // settle 轮询放宽到 600s，避免真实环境波动导致误判失败。
      const settledStatus = await expect.poll(async () => {
        const latestRes = await request.get(`${BACKEND_BASE}/api/ai/${firstJobId}`);
        if (!latestRes.ok()) return 'missing';
        const latestJson = await latestRes.json();
        return latestJson.data?.status || 'missing';
      }, { timeout: 600000 }).toMatch(/cancelled|completed|failed/);

      const settledRes = await request.get(`${BACKEND_BASE}/api/ai/${firstJobId}`);
      expect(settledRes.ok()).toBeTruthy();
      const settled = (await settledRes.json()).data as {
        current_chapter_id?: string | null;
        last_completed_chapter_id?: string | null;
      };

      let expectedResumeChapter = (
        settled.current_chapter_id
        && settled.current_chapter_id !== settled.last_completed_chapter_id
      )
        ? settled.current_chapter_id
        : null;

      if (!expectedResumeChapter && settled.last_completed_chapter_id) {
        const lastIndex = chapterNodes.findIndex((node) => node.id === settled.last_completed_chapter_id);
        if (lastIndex >= 0 && lastIndex + 1 < chapterNodes.length) {
          expectedResumeChapter = chapterNodes[lastIndex + 1].id;
        }
      }

      // Codex 七轮 P1（核心修复）：恢复 job 必须**成功继续生成**至少一章——
      // 等 completed_chapters>=1，且 source=llm、fallback=0、不重复覆盖已完成章节。
      // 为应对 kimi 长文生成的间歇性波动，最多重试 3 次恢复；但每次恢复都必须满足
      // 严格标准才算成功——绝不因重试而把 fallback 计入通过。
      let resumedFinal: {
        id: string;
        completed_chapters: number;
        fallback_chapters: number;
        last_generation_source: string;
        last_completed_chapter_id: string | null;
      } | null = null;
      const resumeAttempts = 3;
      for (let attempt = 0; attempt < resumeAttempts; attempt += 1) {
        const resumeRes = await request.post(`${BACKEND_BASE}/api/ai/start`, {
          data: { projectId: autoProject.id, mode: 'first-unfinished' },
        });
        expect(resumeRes.ok()).toBeTruthy();
        const resumeJson = await resumeRes.json();
        const resumed = resumeJson.data as {
          id: string;
          requested_chapter_id?: string | null;
        };

        if (attempt === 0) {
          if (expectedResumeChapter) {
            expect(resumed.requested_chapter_id).toBe(expectedResumeChapter);
          } else {
            expect(resumed.requested_chapter_id).toBeTruthy();
          }
        }

        // 等本次恢复 job 完成至少一章（kimi 健康时约 2-7 分钟）；
        // 超时（comp=0，kimi 持续失败）也视为一次失败尝试，进入重试。
        let resumedCompleted = 0;
        try {
          await expect.poll(async () => {
            const pollRes = await request.get(`${BACKEND_BASE}/api/ai/${resumed.id}`);
            if (!pollRes.ok()) return 0;
            const pollJson = await pollRes.json();
            return pollJson.data?.completed_chapters ?? 0;
          }, { timeout: 420000 }).toBeGreaterThanOrEqual(1);
          resumedCompleted = 1;
        } catch {
          resumedCompleted = 0;
        }

        const resumedFinalRes = await request.get(`${BACKEND_BASE}/api/ai/${resumed.id}`);
        expect(resumedFinalRes.ok()).toBeTruthy();
        const rf = (await resumedFinalRes.json()).data as {
          completed_chapters: number;
          fallback_chapters: number;
          last_generation_source: string;
          last_completed_chapter_id: string | null;
        };

        const success = resumedCompleted >= 1
          && rf.last_generation_source === 'llm'
          && rf.fallback_chapters === 0
          && rf.last_completed_chapter_id !== firstCompletedChapterId;
        if (success) {
          // 只有成功的恢复 job 才记录 annotation（避免失败的中间尝试污染报告门槛）
          resumedFinal = { id: resumed.id, ...rf };
          recordJob(autoProject.id, resumed.id);
          break;
        }

        // 本次恢复未达到严格标准（fallback / 完成章节异常 / 超时未完成）→ 取消并等待终态，下一轮重试
        await request.post(`${BACKEND_BASE}/api/ai/${resumed.id}/cancel`);
        try {
          await expect.poll(async () => {
            const latestRes = await request.get(`${BACKEND_BASE}/api/ai/${resumed.id}`);
            if (!latestRes.ok()) return 'missing';
            return (await latestRes.json()).data?.status || 'missing';
          }, { timeout: 300000 }).toMatch(/cancelled|completed|failed/);
        } catch {
          // 终态未达也继续下一轮（deleteProject 的 cancel_job_and_wait 会兜底清理）
        }
      }

      // 恢复必须成功：完成至少一章、真实 LLM、无 fallback（多次尝试后仍失败则测试失败）
      expect(
        resumedFinal,
        '恢复 job 在多次尝试后仍未能用真实 LLM 完成至少一章（source=llm / fallback=0）——kimi 波动或恢复链路异常',
      ).toBeTruthy();
      expect(resumedFinal!.completed_chapters).toBeGreaterThanOrEqual(1);
      expect(resumedFinal!.last_generation_source).toBe('llm');
      expect(resumedFinal!.fallback_chapters).toBe(0);
      // 没有重复覆盖已完成章节：恢复 job 完成的章节 ≠ 初始 job 已完成的章节
      expect(resumedFinal!.last_completed_chapter_id).not.toBe(firstCompletedChapterId);
      // 若只完成了 1 章，则应等于"初始 job 已完成章节之后"的下一章（first-unfinished 语义）
      if (resumedFinal!.completed_chapters === 1 && expectedResumeChapter) {
        expect(resumedFinal!.last_completed_chapter_id).toBe(expectedResumeChapter);
      } else {
        expect(resumedFinal!.last_completed_chapter_id).toBeTruthy();
      }
      // 初始 job 已完成章节的内容未被恢复覆盖（强无覆盖证明）
      const chapter1AfterRes = await request.get(
        `${BACKEND_BASE}/api/projects/${autoProject.id}/chapters/${firstCompletedChapterId}`,
      );
      const chapter1After = chapter1AfterRes.ok()
        ? (await chapter1AfterRes.json()).data?.content ?? ''
        : '';
      expect(chapter1After).toBe(chapter1Before);

      // Codex 七轮 P1：恢复 job 也必须等待真实 LLM **成功** 证据（llm/success）
      await waitForLlmSuccess(request, { jobId: resumedFinal!.id }, 480000);

      await request.post(`${BACKEND_BASE}/api/ai/${resumedFinal!.id}/cancel`);
    } finally {
      await deleteProject(request, autoProject.id);
    }
  });

  test('world builder can add and show a rule', async ({ page }) => {
    await page.goto(`/project/${projectId}`);
    await expect(page.getByTestId('workspace-root')).toBeVisible();

    // 点击世界观标签
    await page.getByTestId('workspace-tab-world').click({ timeout: 5000 });
    await expect(page.getByTestId('world-empty-state').or(page.getByText('世界构建工作台')).first()).toBeVisible({ timeout: 5000 });

    // 展开第一个分类
    const firstCategory = page.getByTestId('world-category-magic');
    await firstCategory.click();

    // 点击添加规则
    const addBtn = page.getByTestId('world-add-rule-magic');
    await expect(addBtn).toBeVisible({ timeout: 3000 });
    await addBtn.click();

    // 填写规则名并保存
    const nameInput = page.locator('input[placeholder="规则名称"]').first();
    await expect(nameInput).toBeVisible({ timeout: 3000 });
    await nameInput.fill('测试魔力规则');
    await page.getByTestId('world-edit-save').click();

    // 规则应出现在列表中
    await expect(page.getByText('测试魔力规则').first()).toBeVisible({ timeout: 5000 });
  });

  test('review panel can analyze text', async ({ page, request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 分析');
    markLiveLlm();
    test.setTimeout(180000); // 真实 LLM 审校分析较慢
    const evSince = new Date().toISOString();
    // 直接调用审校 API（等价于前端 review 面板 → reviewApi.comprehensive），
    // 验证真实 LLM 审校链路 + 审计证据（比 UI 状态驱动更可靠）。
    const reviewProject = await createProject(request);
    try {
      const reviewText = '她推开那扇沉重的木门。门后是一条长长的走廊，两侧挂着褪色的肖像画，画中人的眼神似乎都在追随着她的脚步。';
      const reviewRes = await request.post(`${BACKEND_BASE}/api/review/comprehensive`, {
        data: { text: reviewText, chapterId: 'review-smoke-chapter' },
      });
      expect(reviewRes.ok()).toBeTruthy();
      const reviewJson = await reviewRes.json();
      expect(reviewJson.data).toBeTruthy();
      // 审校结果含三块：语言 / 逻辑 / 节奏评分（真实 LLM 输出）
      expect(reviewJson.data.totalIssues ?? 0).toBeGreaterThanOrEqual(0);

      // Codex 六轮 P1：必须证明后端真实调用了 LLM（审计表 role=check/status=success）
      await waitForLlmSuccess(request, { role: 'check', since: evSince }, 120000);
    } finally {
      await deleteProject(request, reviewProject.id);
    }
  });

  test('writing stats shows data after content exists', async ({ page }) => {
    await page.goto(`/project/${projectId}`);
    await expect(page.getByTestId('workspace-root')).toBeVisible();
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('markdown-editor')).toBeVisible();

    await page.getByTestId('editor-textarea').fill('统计测试文本内容，用于验证数据看板。');
    await page.getByTestId('editor-save').click();

    // 切到统计面板
    await page.getByTestId('workspace-tab-stats').click();
    await expect(page.getByTestId('stats-card-总字数').or(page.getByText('写作数据看板')).first()).toBeVisible({ timeout: 5000 });
  });

  test('reference analysis can analyze pasted text', async ({ request }) => {
    test.skip(process.env.RUN_LIVE_LLM !== '1', '@live-llm: 需要真实 LLM 分析');
    markLiveLlm();
    test.setTimeout(240000); // 真实 LLM 参考分析（kimi 推理模型可能 1-3 分钟）
    const evSince = new Date().toISOString();
    // Codex 七轮 P1：直接调用后端 /api/ai/chat（等价于前端参考分析面板 aiApi.chat），
    // **绕过前端 20s 客户端超时导致的本地 fallback**——真实模型失败不再被降级结果掩盖。
    const refProject = await createProject(request);
    try {
      const refText = '她站在雨中等了很久。街道上的人来来往往，没有人注意到她的存在。她忽然明白了，自己从来就不属于这里。';
      const prompt = `请分析下面这段参考小说文本的写作技法，并严格输出 JSON。
字段结构：
{
  "techniques": [{"category":"","finding":"","effectiveness":"","reference":""}],
  "styleComparison": {
    "user":{"avgSentenceLength":0,"dialogueRatio":0,"descriptionDensity":0,"vocabRichness":0},
    "reference":{"avgSentenceLength":0,"dialogueRatio":0,"descriptionDensity":0,"vocabRichness":0}
  },
  "suggestions": [{"ref":"","technique":"","applyTo":"","detail":""}]
}

参考文本：
${refText}`;
      const chatRes = await request.post(`${BACKEND_BASE}/api/ai/chat`, {
        data: { projectId: refProject.id, messages: [{ role: 'user', content: prompt }] },
        timeout: 180000,
      });
      expect(chatRes.ok()).toBeTruthy();
      const chatJson = await chatRes.json();
      const reply = chatJson.data?.reply ?? '';
      expect(reply.length).toBeGreaterThan(50);
      // 真实 LLM 必须返回可解析的技法分析 JSON（三块结构齐全），否则不能算真实分析成功
      const parsed = extractAnalysisResult(reply);
      expect(parsed).toBeTruthy();
      expect(parsed.techniques.length).toBeGreaterThan(0);
      expect(parsed.suggestions.length).toBeGreaterThan(0);

      // Codex 七轮 P1：必须证明后端真实调用 LLM 且生成成功（role=writing / llm/success）
      await waitForLlmSuccess(request, { role: 'writing', since: evSince }, 180000);
    } finally {
      await deleteProject(request, refProject.id);
    }
  });

  test('reference analysis falls back gracefully when LLM fails', async ({ page }) => {
    // Codex 七轮 P1：本地确定性测试（不标记 @live-llm）——mock /api/ai/chat 返回 500，
    // 验证前端参考分析降级到本地分析且给出明确提示（fallback 行为与真实模型分开验证）。
    await page.route('**/api/ai/chat', async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '模拟参考分析服务异常' }),
      });
    });

    await page.goto(`/project/${projectId}`);
    await expect(page.getByTestId('workspace-root')).toBeVisible();
    await page.getByTestId('workspace-tab-reference').click();

    const textInput = page.getByTestId('reference-text-input');
    await expect(textInput).toBeVisible({ timeout: 5000 });
    await textInput.fill('她站在雨中等了很久。街道上的人来来往往，没有人注意到她的存在。');
    await page.getByTestId('reference-analyze-button').click();

    // 降级后应出现本地分析结果 + 明确提示"已切换为本地分析结果"
    await expect(page.getByTestId('reference-error')).toContainText('已切换为本地分析结果', { timeout: 15000 });
    await expect(page.getByText('技法拆解').first()).toBeVisible({ timeout: 15000 });
  });
});


