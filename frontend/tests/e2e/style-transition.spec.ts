import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 文风渐进过渡 v2 前端集成回归（全离线，拦截所有 /api/**）。
 *
 * 约束（来自 handoff）：
 * - 不改数据库 / Story State / Memory / 生成算法 / provider / 预算；
 * - 不通过 Arts/DeepSeek 接口调用真实模型。RUN_LIVE_LLM=0。
 *
 * 覆盖的契约点：
 * A. PUT 单次设置：单一目标预设 + 文字补充 + 自定义周期（37 章）；
 * B. 快/中/慢默认周期 9/60/200；
 * C. targetChapters 边界：1/200 有效，0/负数/201/小数被拒；
 * D. 刷新后从后端恢复计划快照与进度；不凭 localStorage 推算；
 * E. v1 旧计划只读展示：不报错、不虚构版本号与完成数；
 * F. 无画像 / 非法维度不得白屏；
 * G. 续写前重取最新计划版本；SSE started 的 token 绑定到该草稿；保存回传原始 token；
 * H. 生成与保存之间计划变化 → 409 不覆盖正文、不插入本地兜底、不再次调用模型；
 * I. 候选卡展示计划版本；接受后刷新；陈旧候选不被自动接受；
 * J. 计划冲突任务展示保留的候选且不自动恢复；
 * K. 完成文案只说「计划已完成」，不声称文风已达标。
 */

const PANEL_PID = 'style-v2-panel';
const EDITOR_PID = 'style-v2-editor';
/** 第二个项目：用于验证「同页切换项目」时旧项目的在途响应不会污染新项目。 */
const SIBLING_PID = 'style-v2-sibling';
const NOW = '2026-09-28T00:00:00Z';

/** 与后端 VALID_PACES / PACE_CONFIG 对齐。 */
const VALID_PACES = new Set(['fast', 'medium', 'slow', 'custom']);
const PACE_DEFAULTS: Record<string, number> = { fast: 9, medium: 60, slow: 200 };
/** 与后端 VALID_PRESETS 对齐：key 必须原样回传。 */
const VALID_PRESETS = new Set([
  'concise', 'poetic', 'humorous', 'intense', 'tender', 'epic', 'lightNovel', 'western', 'emotional_resonance',
]);

const CHAPTERS = [
  { id: 'c1', fileName: '第01章.md', relativePath: '样书/第01章.md', title: '第一章', content: '雨下了一整夜。', wordCount: 7, order: 1 },
  { id: 'c2', fileName: '第02章.md', relativePath: '样书/第02章.md', title: '第二章', content: '档案被人动过。', wordCount: 7, order: 2 },
];

const PICKED = [
  { name: '第01章.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第一章\n\n雨下了一整夜。') },
  { name: '第02章.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第二章\n\n档案被人动过。') },
];

function projectPayload(pid: string, name: string) {
  return {
    id: pid, name, subtitle: '', genre: 'mystery', status: 'drafting', premise: '雨夜里的旧档案。',
    currentWordCount: 0, targetWordCount: 10000, targetChapters: 2,
    characters: [], locations: [], worldRules: [],
    outline: { mainSummary: '', structure: 'three-act', nodes: [] },
    createdAt: NOW, updatedAt: NOW,
  };
}

function styleProfilePayload(pid: string, dimensions?: Record<string, unknown>) {
  return {
    id: 'profile-1', projectId: pid, status: 'completed',
    narrativeRules: [{ rule: '通过行动展现人物选择。', key: 'action', source: 'llm' }],
    characterRules: [], languageRules: [], rhythmRules: [], antiAiRules: [],
    strengths: ['叙事清晰。'], weaknesses: ['可以增加节奏变化。'],
    dimensions: dimensions ?? {
      avgSentenceLength: 18, dialogueRatio: 0.3, descriptionDensity: 0.2,
      paragraphLength: 'short', tone: '平稳克制',
    },
    metrics: {}, sampleCount: CHAPTERS.length, sampleChars: 4800, report: '采样段落。',
  };
}

/**
 * v2 计划响应，字段与后端 `_present` + `row_to_transition` 一致。
 * `plan` 里的账本（baseline / excluded / completed）由后端持有，前端不得推算。
 */
function v2Transition(options: {
  pid: string; pace: string; targetChapters: number; completedCount: number;
  planId?: string; revision?: number; targetPresetKey?: string; targetPrompt?: string;
}) {
  const { pid, pace, targetChapters, completedCount } = options;
  const planId = options.planId ?? 'plan-1';
  const revision = options.revision ?? 1;
  const totalSteps = pace === 'fast' ? 3 : pace === 'medium' ? 10 : pace === 'slow' ? 20 : Math.max(targetChapters, 1);
  const intervalChapters = pace === 'fast' ? 3 : pace === 'medium' ? 6 : pace === 'slow' ? 10 : 1;
  return {
    id: 'transition-1', projectId: pid, profileId: 'profile-1', pace,
    progress: Math.min(completedCount / targetChapters, 1),
    intervalChapters, totalSteps,
    plan: {
      schemaVersion: 2, planId, revision,
      targetChapters,
      baselineChapterIds: ['c1', 'c2'], excludedChapterIds: [],
      completedChapters: Array.from({ length: completedCount }, (_, i) => ({
        chapterId: `done-${i}`, weight: (i + 1) / targetChapters, commitId: '',
      })),
      sourceSnapshot: {
        profileId: 'profile-1', profileVersion: 1, name: '雨夜原稿画像', status: 'completed',
        dimensions: {}, rules: {}, identity: {}, mechanics: {}, boundaries: {},
      },
      targetSnapshot: {
        presetKey: options.targetPresetKey ?? '',
        preset: {},
        customPrompt: options.targetPrompt ?? '',
      },
    },
    startedAt: NOW, updatedAt: NOW,
    schemaVersion: 2, legacyReadOnly: false, planId, revision,
    targetChapters, completedCount,
    sourceSnapshot: {
      profileId: 'profile-1', profileVersion: 1, name: '雨夜原稿画像', status: 'completed',
      dimensions: {}, rules: {}, identity: {}, mechanics: {}, boundaries: {},
    },
    targetSnapshot: {
      presetKey: options.targetPresetKey ?? '',
      preset: {},
      customPrompt: options.targetPrompt ?? '',
    },
  };
}

/** v1 旧计划：后端明确标记只读，且**不**补 planId / revision / completedCount。 */
function v1Transition(pid: string) {
  return {
    id: 'transition-legacy', projectId: pid, profileId: 'profile-1', pace: 'medium',
    progress: 0.4, intervalChapters: 6, totalSteps: 10,
    plan: { step: 4, weight: 0.4, start_chapter_count: 10, steps: [] },
    startedAt: NOW, updatedAt: NOW,
    schemaVersion: 1, legacyReadOnly: true,
  };
}

/** 面板 fixture：走 Markdown 导入流程，导入成功后文风面板才可用。 */
async function installPanelFixture(
  page: Page,
  options: { profile?: 'ok' | 'none' | 'illegal'; transition?: Record<string, unknown> | null } = {},
) {
  const state = {
    transitionBodies: [] as Record<string, unknown>[],
    transitionResponses: [] as number[],
    profileGets: 0,
    chain: Promise.resolve() as Promise<unknown>,
    unexpected: [] as string[],
    errors: [] as string[],
  };
  let profile: Record<string, unknown> | null = null;
  let transition: Record<string, unknown> | null = options.transition ?? null;

  page.on('pageerror', (error) => state.errors.push(error.message));

  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });
  const base = `/api/projects/${PANEL_PID}`;

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/\/$/, '');
    const method = request.method();

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    if (path === '/api/import/markdown-files' && method === 'POST') {
      return ok(route, {
        success: true, folderName: '样书', totalFiles: CHAPTERS.length, totalWords: 14, chapters: CHAPTERS,
        analysis: { characters: [], locations: [], timeline: [], foreshadowing: [], summary: '已分析 2 章。' },
      });
    }
    if (path === '/api/import/markdown-files/ingest' && method === 'POST') {
      return ok(route, {
        project: projectPayload(PANEL_PID, '文风过渡面板'), chapters: CHAPTERS,
        importedChapters: CHAPTERS.length, totalWords: 14, analysis: { summary: '已分析 2 章。' },
      });
    }

    if (path === `${base}/style-analysis` && method === 'POST') {
      profile = styleProfilePayload(PANEL_PID);
      return ok(route, profile);
    }

    // 单次设置计划：这里按后端校验顺序复刻，确保前端发出的载荷真的能被后端接受。
    if (path === `${base}/style-transition` && method === 'PUT') {
      const body = request.postDataJSON() as {
        pace?: string; targetChapters?: unknown; targetStyle?: { presetKey?: string; customPrompt?: string };
      };
      state.transitionBodies.push(body);
      const reject = (status: number, detail: string) => {
        state.transitionResponses.push(status);
        return route.fulfill({ status, json: { detail } });
      };
      if (typeof body.pace !== 'string' || !VALID_PACES.has(body.pace)) {
        return reject(400, `不支持的过渡档位：${String(body.pace)}`);
      }
      // StrictInt：字符串/小数/布尔都不是整数 → 422
      const provided = body.targetChapters;
      if (provided !== undefined && provided !== null) {
        if (typeof provided !== 'number' || !Number.isInteger(provided)) {
          return reject(422, 'Input should be a valid integer');
        }
        if (provided < 1 || provided > 200) {
          return reject(422, 'Input should be greater than or equal to 1');
        }
      }
      if (body.pace === 'custom' && (provided === undefined || provided === null)) {
        return reject(400, '自定义过渡需要 targetChapters');
      }
      if (body.targetStyle?.presetKey && !VALID_PRESETS.has(body.targetStyle.presetKey)) {
        return reject(400, '请选择有效的目标文风预设');
      }
      if (typeof body.targetStyle?.customPrompt === 'string' && body.targetStyle.customPrompt.length > 2000) {
        return reject(400, '文字补充不能超过 2000 字');
      }
      const targetChapters = (provided as number | undefined) ?? PACE_DEFAULTS[body.pace] ?? 60;
      transition = v2Transition({
        pid: PANEL_PID, pace: body.pace, targetChapters, completedCount: 0,
        targetPresetKey: body.targetStyle?.presetKey ?? '', targetPrompt: body.targetStyle?.customPrompt ?? '',
      });
      state.transitionResponses.push(200);
      return ok(route, transition);
    }

    if (method === 'GET') {
      if (path === base) return ok(route, projectPayload(PANEL_PID, '文风过渡面板'));
      if (path === '/api/projects') return ok(route, []);
      if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
      if (path.endsWith('/chapters')) return ok(route, CHAPTERS);
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'h1' });
      if (path.endsWith('/style-contract')) {
        return ok(route, { styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {} });
      }
      if (path.endsWith('/style-profile')) {
        state.profileGets++;
        if (options.profile === 'none') return route.fulfill({ status: 404, json: { detail: '项目尚未进行深度文风分析。' } });
        if (options.profile === 'illegal') {
          profile = styleProfilePayload(PANEL_PID, {
            // 非法维度：对象/数字/超长字符串，必须被当成「未知」而不是渲染成 React 子节点
            paragraphLength: { nested: true }, tone: 12345,
            avgSentenceLength: 'not-a-number', dialogueRatio: null,
          } as unknown as Record<string, unknown>);
          return ok(route, profile);
        }
        profile = profile ?? styleProfilePayload(PANEL_PID);
        return ok(route, profile);
      }
      if (path.endsWith('/style-transition')) {
        if (!transition) return route.fulfill({ status: 404, json: { detail: '项目尚未设置风格过渡。' } });
        return ok(route, transition);
      }
      if (path.endsWith('/story-state')) return ok(route, { projectId: PANEL_PID, version: 1 });
      if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
      if (path === `/api/ai/project/${PANEL_PID}`) return ok(route, null);
      if (path === `/api/ai/project/${PANEL_PID}/recoverable`) return ok(route, []);
      if (path.startsWith('/api/settings/')) return ok(route, []);
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${PANEL_PID}`);
  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('import-mode-markdown').click();
  await expect(page.getByTestId('markdown-import-panel')).toBeVisible();

  const setter = {
    async ingest() {
      await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
      await page.getByTestId('markdown-upload-scan-button').click();
      await expect(page.getByTestId('markdown-scan-success')).toBeVisible();
      await page.getByTestId('markdown-ingest-button').click();
      await expect(page.getByTestId('markdown-ingest-success')).toBeVisible();
      await expect(page.getByTestId('style-analysis-panel')).toBeVisible();
    },
    setTransition(value: Record<string, unknown> | null) { transition = value; },
  };
  return { state, setter };
}

/** 编辑器 fixture：直接进入工作区，用于续写 token 与保存回传的回归。 */
async function installEditorFixture(
  page: Page,
  options: { planId?: string; revision?: number; conflictOnSave?: boolean; candidateMissingToken?: boolean; missingStartedToken?: boolean; startedRevision?: number; deferContinueResponse?: boolean; deferSaveResponse?: boolean; sibling?: boolean } = {},
) {
  const chapter = { id: 'c1', projectId: EDITOR_PID, volumeId: 'v1', title: '第一章', order: 1, content: '原稿：雨停后，她把信放回抽屉。' };
  const secondChapter = { id: 'c2', projectId: EDITOR_PID, volumeId: 'v1', title: '第二章', order: 2, content: '第二章原稿。' };
  const state = {
    continueRequests: [] as Record<string, unknown>[],
    continueCount: 0,
    autoStartRequests: [] as Record<string, unknown>[],
    saveRequests: [] as Record<string, unknown>[],
    acceptCount: 0,
    planId: options.planId ?? 'plan-1',
    planRevision: options.revision ?? 1,
    conversationRevision: 1,
    conflictOnSave: options.conflictOnSave ?? false,
    candidateRequests: [] as Record<string, unknown>[],
    candidates: [] as Record<string, unknown>[],
    messages: [] as Record<string, unknown>[],
    latestJob: null as Record<string, unknown> | null,
    contractCalls: 0,
    holdContract: false,
    contractPaused: false,
    releaseContract: null as (() => void) | null,
    failContract: false,
    unexpected: [] as string[],
    errors: [] as string[],
    releaseContinue: null as (() => void) | null,
    releaseSave: null as (() => void) | null,
    missingStartedToken: options.missingStartedToken ?? false,
  };

  page.on('pageerror', (error) => state.errors.push(error.message));
  await page.addInitScript(() => sessionStorage.setItem('novelcraft-beta-invite', 'mock-only'));

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });
  const base = `/api/projects/${EDITOR_PID}`;
  const siblingBase = `/api/projects/${SIBLING_PID}`;
  /** 第二个项目的最小数据：只需能渲染出写作页与一章正文。 */
  const siblingChapter = {
    id: 'b1', projectId: SIBLING_PID, volumeId: 'bv1', title: '第一章', order: 1,
    content: '另一部作品的原稿：他在站台等天亮。',
  };
  const siblingOutline = {
    mainSummary: '另一条线', structure: 'three-act', nodes: [
      { id: 'bv1', type: 'volume', title: '第一卷', summary: '等天亮。', order: 1 },
      { id: 'ba1', type: 'arc', parentId: 'bv1', title: '站台', summary: '等天亮。', order: 1 },
      { id: 'b1', type: 'chapter', parentId: 'bv1', arcId: 'ba1', title: '第一章', summary: '站台', order: 1 },
    ],
  };
  const outline = {
    mainSummary: '离线测试', structure: 'three-act', nodes: [
      { id: 'v1', type: 'volume', title: '第一卷', summary: '送信。', order: 1 },
      { id: 'a1', type: 'arc', parentId: 'v1', title: '送信', summary: '决定送信。', order: 1 },
      { id: 'c1', type: 'chapter', parentId: 'v1', arcId: 'a1', title: '第一章', summary: '决定送信', order: 1 },
      { id: 'c2', type: 'chapter', parentId: 'v1', arcId: 'a1', title: '第二章', summary: '送出信件', order: 2 },
    ],
  };

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/\/$/, '');
    const method = request.method();

    if (path === '/api/auth/me') return ok(route, { id: 'mock', username: 'mock', isAdmin: false });

    // 第二个项目：让侧边栏「最近项目」能列出并点击，从而走**客户端路由**切换项目。
    // 编辑器没有按 projectId 加 key，所以切项目时同一个组件实例会继续服务新项目。
    // 这段必须排在下面通用的 `endsWith('/chapters')` 之前，否则会把兄弟项目的
    // 章节请求交给主项目的数据应答。
    if (options.sibling) {
      if (path === '/api/projects' && method === 'GET') {
        return ok(route, [
          { ...projectPayload(EDITOR_PID, '续写 token 回归'), isStarred: false },
          { ...projectPayload(SIBLING_PID, '另一部作品'), isStarred: false },
        ]);
      }
      if (path === siblingBase && method === 'GET') {
        return ok(route, { ...projectPayload(SIBLING_PID, '另一部作品'), outline: siblingOutline });
      }
      if (path === `${siblingBase}/chapters` && method === 'GET') return ok(route, [siblingChapter]);
      if (path === `${siblingBase}/style-contract`) {
        return ok(route, { styleTone: '', stylePlanId: '', stylePlanRevision: 0, contract: {} });
      }
      if (path.startsWith(`${siblingBase}/chapters/`)) {
        const cid = path.slice(`${siblingBase}/chapters/`.length);
        if (cid.endsWith('/projections')) return ok(route, []);
        if (method === 'GET') return ok(route, siblingChapter);
      }
      if (method === 'GET' && path.startsWith(siblingBase)) {
        if (path.endsWith('/chat-conversations')) return ok(route, { records: [] });
        if (path.endsWith('/outline-candidates')) return ok(route, []);
        if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'sibling-outline' });
        if (path.endsWith('/story-state')) return ok(route, { projectId: SIBLING_PID, version: 1 });
        if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
        if (path.endsWith('/style-profile')) return ok(route, styleProfilePayload(SIBLING_PID));
        if (path.endsWith('/style-transition')) {
          return route.fulfill({ status: 404, json: { detail: '项目尚未设置风格过渡。' } });
        }
        if (path.endsWith('/recoverable')) return ok(route, []);
      }
      if (method === 'GET' && path === `/api/ai/project/${SIBLING_PID}`) return ok(route, null);
      if (method === 'GET' && path === `/api/ai/project/${SIBLING_PID}/recoverable`) return ok(route, []);
    }

    // 候选卡靠对话消息的元数据渲染，所以这里必须提供一条真实对话
    const conversation = {
      id: 'conv', project_id: EDITOR_PID, title: '正文修改', archived: false,
      revision: state.conversationRevision, created_at: NOW, updated_at: NOW, messages: state.messages,
    };
    if (path === `${base}/chat-conversations` && method === 'GET') {
      const archived = new URL(request.url()).searchParams.get('archived') === 'true';
      return ok(route, { records: archived ? [] : [conversation] });
    }
    if (path === `${base}/chat-conversations/conv` && method === 'GET') {
      return ok(route, conversation);
    }

    // 契约：顶层 stylePlanId / stylePlanRevision 是「未启用」的明确取值，不能被当成缺失。
    if (path === `${base}/style-contract`) {
      state.contractCalls++;
      if (state.holdContract) {
        state.contractPaused = true;
        await new Promise<void>((resolve) => { state.releaseContract = resolve; });
      }
      if (state.failContract) return route.fulfill({ status: 503, json: { detail: 'style contract unavailable' } });
      return ok(route, {
        styleTone: '克制、精准',
        stylePlanId: state.planId,
        stylePlanRevision: state.planRevision,
        contract: {},
      });
    }

    if (path === '/api/ai/continue-writing' && method === 'POST') {
      state.continueCount++;
      state.continueRequests.push(request.postDataJSON());
      if (options.deferContinueResponse) {
        await new Promise<void>((resolve) => { state.releaseContinue = resolve; });
      }
      // 客户端可能在闸门放行前就 abort（例如切换项目）。此时请求已被丢弃，
      // fulfill 会报错，但那是预期行为，不应让夹具自己炸掉。
      const fail = () => {};
      // 首个事件是 started（携带本次生成的计划版本），不是正文。
      const events = [
        `data: ${JSON.stringify({ chunk: '', status: 'started', ...(state.missingStartedToken ? {} : { stylePlanId: state.planId, stylePlanRevision: options.startedRevision ?? state.planRevision }) })}`,
        `data: ${JSON.stringify({ chunk: '生成正文。' })}`,
        'data: [DONE]',
      ].join('\n\n') + '\n\n';
      await route
        .fulfill({ status: 200, headers: { 'Content-Type': 'text/event-stream' }, body: events })
        .catch(fail);
      return;
    }

    if (path === '/api/ai/start' && method === 'POST') {
      state.autoStartRequests.push(request.postDataJSON());
      return ok(route, {
        id: 'job-started', project_id: EDITOR_PID, mode: 'single', requested_chapter_id: 'c1',
        status: 'completed', message: '完成', target_words: 500, total_chapters: 1,
        completed_chapters: 1, failed_chapters: 0, fallback_chapters: 0, generated_words: 10,
        current_chapter_id: 'c1', current_chapter_title: '第一章', llm_provider: '', llm_model: '',
        last_generation_source: '', started_at: NOW, updated_at: NOW, finished_at: NOW, error: '',
        retry_count: 0, max_retries: 0,
      });
    }

    if (path === '/api/copilot/candidates' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.candidateRequests.push(body);
      const candidate = {
        id: `candidate-${state.candidates.length + 1}`, project_id: EDITOR_PID, chapter_id: 'c1',
        rewrite_type: 'rewrite', status: 'proposed',
        original: chapter.content, rewritten: '候选：她握紧信封，推开门。', candidate: '候选：她握紧信封，推开门。',
        diff: [{ type: 'replace', original: chapter.content, candidate: '候选：她握紧信封，推开门。' }],
        ...(options.candidateMissingToken ? {} : { stylePlanId: state.planId, stylePlanRevision: state.planRevision }),
      };
      state.candidates.push(candidate);
      if (!body.regenerateMessageId) {
        state.messages.push(
          { id: `u-${state.candidates.length}`, role: 'user', content: body.instruction, timestamp: NOW, status: 'completed', metadata: {} },
          {
            id: `a-${state.candidates.length}`, role: 'assistant', content: '已生成候选，请确认。',
            timestamp: NOW, status: 'completed',
            metadata: { kind: 'copilot', mode: 'llm', operation: body, candidate },
          },
        );
      } else {
        const old = state.messages.find((message) => message.id === body.regenerateMessageId);
        if (old) old.status = 'pending';
      }
    state.conversationRevision++;
      return ok(route, candidate);
    }

    if (path.endsWith('/accept') && method === 'POST') {
      state.acceptCount++;
      const id = path.split('/').slice(-2)[0];
      const candidate = state.candidates.find((item) => item.id === id);
      if (!candidate) return route.fulfill({ status: 404, json: { detail: 'candidate not found' } });
      // 计划已变化：服务端用候选持久化的 token 校验 → 409，候选保持 proposed
      if (candidate.stylePlanId !== state.planId || candidate.stylePlanRevision !== state.planRevision) {
        return route.fulfill({
          status: 409,
          json: {
            detail: {
              code: 'style_plan_conflict',
              message: '风格过渡计划或进度已更新，正文候选已保留且未覆盖正文；请基于最新计划重新生成。',
            },
          },
        });
      }
      candidate.status = 'applied';
      chapter.content = String(candidate.rewritten ?? chapter.content);
      return ok(route, {
        ...candidate, applied_content: chapter.content,
        styleTransition: v2Transition({ pid: EDITOR_PID, pace: 'medium', targetChapters: 60, completedCount: 1 }),
      });
    }

    if (path.startsWith(`${base}/chapters/`)) {
      const cid = path.slice(`${base}/chapters/`.length);
      const chapterRecord = cid === 'c2' ? secondChapter : chapter;
      if (cid.endsWith('/projections')) return ok(route, []);
      if (method === 'GET') return ok(route, chapterRecord);
      if (method === 'PUT') {
        const body = request.postDataJSON() as Record<string, unknown>;
        state.saveRequests.push(body);
        if (options.deferSaveResponse) {
          await new Promise<void>((resolve) => { state.releaseSave = resolve; });
        }
        if (state.conflictOnSave) {
          return route.fulfill({
            status: 409,
            json: {
              detail: {
                code: 'style_plan_conflict',
                message: '风格过渡计划或进度已更新，正文候选已保留且未覆盖正文；请基于最新计划重新生成。',
              },
            },
          });
        }
        chapterRecord.content = String(body.content ?? chapterRecord.content);
        return ok(route, { ...chapterRecord, styleTransition: v2Transition({ pid: EDITOR_PID, pace: 'medium', targetChapters: 60, completedCount: 1 }) });
      }
    }

    if (method === 'GET') {
      if (path === base) return ok(route, { ...projectPayload(EDITOR_PID, '续写 token 回归'), outline });
      if (path === '/api/projects') return ok(route, []);
      if (path.endsWith('/chapters')) return ok(route, [chapter, secondChapter]);
      if (path.endsWith('/outline-candidates')) return ok(route, []);
      if (path.endsWith('/chapter-trash')) return ok(route, { records: [], targets: [], outlineHash: 'outline-v1' });
      if (path.endsWith('/story-state')) return ok(route, { projectId: EDITOR_PID, version: 1 });
      if (path.endsWith('/foreshadows')) return ok(route, { records: [], stats: {} });
      if (path.endsWith('/style-profile')) return ok(route, styleProfilePayload(EDITOR_PID));
      if (path === `/api/ai/project/${EDITOR_PID}`) return ok(route, state.latestJob);
      if (path === `/api/ai/project/${EDITOR_PID}/recoverable`) return ok(route, []);
      if (path.startsWith('/api/settings/')) return ok(route, []);
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  await page.goto(`/project/${EDITOR_PID}`);
  await expect(page.getByRole('heading', { name: '续写 token 回归', exact: true })).toBeVisible();
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toBeVisible();
  return state;
}

// ══════════════════════════════════════════════════════
// A/B/C. 计划设置载荷与边界
// ══════════════════════════════════════════════════════

test('custom pace sends the target preset, the text supplement and an explicit chapter count', async ({ page }) => {
  const { state, setter } = await installPanelFixture(page);
  await setter.ingest();

  await page.getByTestId('style-pace-custom').check();
  await page.getByTestId('style-custom-chapters').fill('37');
  await page.getByTestId('style-target-preset').selectOption('emotional_resonance');
  await page.getByTestId('style-target-prompt').fill('对白更口语化，减少四字成语');
  await page.getByTestId('style-transition-start-button').click();

  await expect(page.getByTestId('style-transition-summary')).toBeVisible();
  expect(state.transitionBodies).toEqual([{
    pace: 'custom',
    targetChapters: 37,
    targetStyle: { presetKey: 'emotional_resonance', customPrompt: '对白更口语化，减少四字成语' },
  }]);
  // 周期与完成数直接来自后端响应，不由前端推算
  await expect(page.getByTestId('style-transition-target-chapters')).toContainText('37 章');
  await expect(page.getByTestId('style-transition-completed-count')).toContainText('0 章');
  await expect(page.getByTestId('style-transition-summary')).toContainText('细腻情感 / 共鸣型');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('quick paces rely on the backend defaults of 9, 60 and 200 chapters', async ({ page }) => {
  for (const [pace, chapters] of [['fast', 9], ['medium', 60], ['slow', 200]] as const) {
    const { state, setter } = await installPanelFixture(page);
    await setter.ingest();

    // 默认值对作者可见（来源与后端 PACE_CONFIG 一致）
    await expect(page.getByTestId(`style-pace-default-${pace}`)).toContainText(String(chapters));
    await page.getByTestId(`style-pace-${pace}`).check();
    await page.getByTestId('style-transition-start-button').click();

    // 只发 pace：默认章节数由后端决定，前端不各存一份
    expect(state.transitionBodies, pace).toEqual([{ pace }]);
    const target = page.getByTestId('style-transition-target-chapters');
    await expect(target, pace).toContainText(`${chapters} 章`);
  }
});

test('chapter-count boundaries accept 1 and 200 and reject 0, negatives, 201 and decimals', async ({ page }) => {
  const { state, setter } = await installPanelFixture(page);
  await setter.ingest();
  await page.getByTestId('style-pace-custom').check();
  const input = page.getByTestId('style-custom-chapters');
  const button = page.getByTestId('style-transition-start-button');

  for (const valid of ['1', '200']) {
    await input.fill(valid);
    state.transitionBodies.length = 0;
    await button.click();
    expect(state.transitionBodies, `valid ${valid}`).toEqual([{ pace: 'custom', targetChapters: Number(valid) }]);
    await page.getByTestId('style-transition-reset-button').click();
  }

  // 非法值在前端就被挡下（后端会 422），且不能发出请求
  for (const invalid of ['0', '-3', '201']) {
    await input.fill(invalid);
    state.transitionBodies.length = 0;
    await button.click();
    await expect(page.getByTestId('style-transition-error'), invalid).toContainText('1');
    expect(state.transitionBodies, `invalid ${invalid}`).toEqual([]);
  }

  // 小数不是整数：同样不发请求
  await input.fill('3.5');
  state.transitionBodies.length = 0;
  await button.click();
  await expect(page.getByTestId('style-transition-error')).toContainText('整数');
  expect(state.transitionBodies).toEqual([]);
  // 全程只有两次「1」和「200」的合法请求被服务端接受，没有任何 4xx
  expect(state.transitionResponses).toEqual([200, 200]);
});

// ══════════════════════════════════════════════════════
// D. 刷新恢复
// ══════════════════════════════════════════════════════

test('a page refresh restores the plan snapshot and progress from the backend', async ({ page }) => {
  const { setter } = await installPanelFixture(page, {
    transition: v2Transition({
      pid: PANEL_PID, pace: 'slow', targetChapters: 200, completedCount: 12,
      targetPresetKey: 'epic', targetPrompt: '语言更庄重',
    }),
  });
  await setter.ingest();

  await expect(page.getByTestId('style-transition-target-chapters')).toContainText('200 章');
  await expect(page.getByTestId('style-transition-completed-count')).toContainText('12 章');
  await expect(page.getByTestId('style-transition-progress-label')).toContainText('6%');
  await expect(page.getByTestId('style-transition-revision')).toContainText('计划版本 1');
  await expect(page.getByTestId('style-transition-summary')).toContainText('雨夜原稿画像');
  await expect(page.getByTestId('style-transition-summary')).toContainText('史诗宏大');

  // 刷新后重新 GET，计划信息与刷新前一致（完成数来自后端，不来自本地存储）
  await page.reload();
  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('import-mode-markdown').click();
  // 面板挂在导入预览步骤下，刷新后需要重新扫描才能看到
  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await expect(page.getByTestId('style-analysis-panel')).toBeVisible();
  await expect(page.getByTestId('style-transition-target-chapters')).toContainText('200 章');
  await expect(page.getByTestId('style-transition-completed-count')).toContainText('12 章');
  await expect(page.getByTestId('style-transition-revision')).toContainText('计划版本 1');

  // 完成数只能来自后端：本地存储里不得出现进度/完成数（否则刷新会“记住”旧值）
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(stored).not.toMatch(/completedCount|targetChapters/);
});

test('an existing plan is not migrated by an automatic PUT and no second start button is offered', async ({ page }) => {
  const { state, setter } = await installPanelFixture(page, {
    transition: v2Transition({ pid: PANEL_PID, pace: 'medium', targetChapters: 60, completedCount: 3 }),
  });
  await setter.ingest();

  await expect(page.getByTestId('style-transition-summary')).toBeVisible();
  // 打开面板本身绝不能触发任何 PUT（否则每次打开都会生成新 planId / revision）
  expect(state.transitionBodies).toEqual([]);
  expect(state.transitionResponses).toEqual([]);
  // 表单收起：只有「重新设置计划」入口，没有直接可点的开始按钮
  await expect(page.getByTestId('style-transition-start-button')).toHaveCount(0);
  await expect(page.getByTestId('style-transition-reset-button')).toHaveCount(1);
});

// ══════════════════════════════════════════════════════
// E. v1 旧计划只读
// ══════════════════════════════════════════════════════

test('a legacy v1 plan is shown read-only without errors or fabricated progress counts', async ({ page }) => {
  const { state, setter } = await installPanelFixture(page, { transition: v1Transition(PANEL_PID) });
  await setter.ingest();

  await expect(page.getByTestId('style-transition-legacy-notice')).toContainText('旧版计划，重新设置后启用新进度记录');
  // 不虚构版本号，不把旧步骤数折算成完成章节
  await expect(page.getByTestId('style-transition-revision')).toHaveCount(0);
  await expect(page.getByTestId('style-transition-completed-count')).toHaveCount(0);
  await expect(page.getByTestId('style-transition-summary')).toHaveCount(0);
  // 旧进度照旧可读
  await expect(page.getByTestId('style-transition-progress-label')).toContainText('40%');
  // 不报错、不白屏
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
  expect(state.errors).toEqual([]);
  expect(state.transitionBodies).toEqual([]);

  // 重新设置才启用 v2 记录
  await page.getByTestId('style-transition-reset-button').click();
  await expect(page.getByTestId('style-transition-start-button')).toBeVisible();
});

// ══════════════════════════════════════════════════════
// F. 无画像 / 非法维度不白屏
// ══════════════════════════════════════════════════════

test('no profile yet still renders the plan form without a white screen', async ({ page }) => {
  const { state, setter } = await installPanelFixture(page, { profile: 'none' });
  await setter.ingest();

  await expect(page.getByTestId('style-transition-start-button')).toBeVisible();
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
  await expect(page.getByTestId('style-analysis-panel')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('illegal profile dimensions are treated as unknown instead of crashing the panel', async ({ page }) => {
  const { state, setter } = await installPanelFixture(page, { profile: 'illegal' });
  await setter.ingest();

  // 面板与计划表单都在，且没有把对象当成 React 子节点渲染
  await expect(page.getByTestId('style-analysis-panel')).toBeVisible();
  await expect(page.getByTestId('style-transition-start-button')).toBeVisible();
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
  expect(state.errors).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

// ══════════════════════════════════════════════════════
// K. 完成文案不得夸大
// ══════════════════════════════════════════════════════

test('a completed plan only claims the chapter count is done, never that the style is achieved', async ({ page }) => {
  const { setter } = await installPanelFixture(page, {
    transition: v2Transition({ pid: PANEL_PID, pace: 'fast', targetChapters: 9, completedCount: 9 }),
  });
  await setter.ingest();

  const complete = page.getByTestId('style-transition-complete-text');
  await expect(complete).toContainText('计划已完成');
  await expect(complete).toContainText('人工判断');
  // 这些夸大说法一个都不许出现
  await expect(page.getByText('已对齐目标风格')).toHaveCount(0);
  await expect(page.getByText('文风已达标')).toHaveCount(0);
  await expect(page.getByText('过渡已完成，当前文风已对齐目标预设。')).toHaveCount(0);
  // 进度条按 progress 渲染（此处为 100%）
  await expect(page.getByTestId('style-transition-progress-label')).toContainText('100%');
});

// ══════════════════════════════════════════════════════
// G/H. 续写 token 生命周期
// ══════════════════════════════════════════════════════

test('continue-writing sends the latest contract token and saves the exact body with the distinct started token', async ({ page }) => {
  const state = await installEditorFixture(page, { startedRevision: 7 });

  await page.getByTestId('editor-ai-continue').click();
  await expect.poll(() => state.contractCalls).toBeGreaterThan(0);
  await expect.poll(() => state.continueCount).toBe(1);
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);

  // 发请求前重新取契约，token 必须带上
  expect(state.continueRequests[0].stylePlanId).toBe('plan-1');
  expect(state.continueRequests[0].stylePlanRevision).toBe(1);
  // started 事件不是正文：不得把空 chunk 当文本写进正文
  expect(String(state.continueRequests[0].contextBefore)).not.toContain('started');

  // 保存回传的是**生成时**的 token
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  expect(state.saveRequests[0].stylePlanId).toBe('plan-1');
  expect(state.saveRequests[0].stylePlanRevision).toBe(7);
  expect(state.unexpected).toEqual([]);
});

test('generated draft keeps its original style token after the editor is unmounted and remounted', async ({ page }) => {
  const state = await installEditorFixture(page, { startedRevision: 7, conflictOnSave: true });
  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  await page.getByTestId('workspace-tab-outline').click();
  await expect(page.getByTestId('editor-textarea')).toHaveCount(0);
  state.planRevision = 8;
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('原稿：雨停后，她把信放回抽屉。生成正文。');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  expect(state.saveRequests[0].stylePlanId).toBe('plan-1');
  expect(state.saveRequests[0].stylePlanRevision).toBe(7);
  await expect(page.getByTestId('editor-save-message')).toContainText('风格过渡计划');
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  expect(state.errors).toEqual([]);
});

test('manual draft remains a manual save after the editor is remounted', async ({ page }) => {
  const state = await installEditorFixture(page);
  await page.getByTestId('editor-textarea').fill('手写草稿。');
  await page.getByTestId('workspace-tab-outline').click();
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('手写草稿。');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanId');
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanRevision');
  expect(state.errors).toEqual([]);
});

test('reopening a project loads server prose without reviving a discarded generated draft token', async ({ page }) => {
  const state = await installEditorFixture(page, { sibling: true });
  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  await page.getByRole('button', { name: '另一部作品', exact: true }).click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('另一部作品的原稿：他在站台等天亮。');
  await page.getByRole('button', { name: '续写 token 回归', exact: true }).click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('原稿：雨停后，她把信放回抽屉。');
  await page.getByTestId('editor-textarea').fill('重新打开后手写的正文。');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanId');
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanRevision');
  expect(state.errors).toEqual([]);
});

test('a late Markdown ingest cannot overwrite a newer draft in the same project', async ({ page }) => {
  const state = await installEditorFixture(page);
  let release: (() => void) | undefined;
  await page.route('**/api/import/markdown-files', (route) => route.fulfill({ json: { success: true, data: {
    success: true, totalFiles: 2, totalWords: 14, chapters: CHAPTERS,
    analysis: { characters: [], locations: [], timeline: [], foreshadowing: [], summary: '' },
  } } }));
  await page.route('**/api/import/markdown-files/ingest', async (route) => {
    await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ json: { success: true, data: { project: projectPayload(EDITOR_PID, '续写 token 回归'), chapters: CHAPTERS, importedChapters: 2 } } });
  });
  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await page.getByTestId('markdown-ingest-button').click();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.getByTestId('workspace-tab-write').click();
  await page.getByTestId('editor-textarea').fill('导入期间刚写的草稿。');
  release!();
  await page.getByTestId('workspace-tab-import').click();
  await expect(page.getByTestId('markdown-ingest-success')).toContainText('已保留当前草稿');
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('导入期间刚写的草稿。');
  expect(state.errors).toEqual([]);
});

test('a late Markdown ingest cannot write its chapter into a different active project', async ({ page }) => {
  const state = await installEditorFixture(page, { sibling: true });
  let release: (() => void) | undefined;
  await page.route('**/api/import/markdown-files', (route) => route.fulfill({ json: { success: true, data: {
    success: true, totalFiles: 2, totalWords: 14, chapters: CHAPTERS,
    analysis: { characters: [], locations: [], timeline: [], foreshadowing: [], summary: '' },
  } } }));
  await page.route('**/api/import/markdown-files/ingest', async (route) => {
    await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ json: { success: true, data: { project: projectPayload(EDITOR_PID, '续写 token 回归'), chapters: CHAPTERS, importedChapters: 2 } } });
  });
  await page.getByTestId('workspace-tab-import').click();
  await page.getByTestId('markdown-file-input').setInputFiles(PICKED);
  await page.getByTestId('markdown-upload-scan-button').click();
  await page.getByTestId('markdown-ingest-button').click();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.getByRole('button', { name: '另一部作品', exact: true }).click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('另一部作品的原稿：他在站台等天亮。');
  await page.getByTestId('editor-textarea').fill('B 项目的新草稿。');
  const response = page.waitForResponse('**/api/import/markdown-files/ingest');
  release!();
  await response;
  await page.getByTestId('workspace-tab-outline').click();
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('B 项目的新草稿。');
  expect(state.errors).toEqual([]);
});

for (const changedContext of ['chapter', 'draft', 'project'] as const) {
  test(`a late Word suggestion preserves the current ${changedContext} and retains its unapplied result`, async ({ page }) => {
    const state = await installEditorFixture(page, { sibling: changedContext === 'project' });
    let release: (() => void) | undefined;
    await page.route('**/api/import/suggestion-docx', (route) => route.fulfill({ json: { success: true, data: {
      success: true, fileName: 'mock.docx', totalSuggestions: 1,
      suggestions: [{ id: 's1', order: 1, status: 'pending', action: 'enhance', target: '正文', description: '测试建议', priority: 'high', dependencies: [] }],
    } } }));
    await page.route('**/api/import/apply-suggestion', async (route) => {
      await new Promise<void>((resolve) => { release = resolve; });
      await route.fulfill({ json: { success: true, data: { result: '迟到的建议正文。', status: 'applied' } } });
    });
    await page.getByTestId('workspace-tab-import').click();
    await page.getByTestId('import-mode-word').click();
    await page.getByTestId('word-file-input').setInputFiles({ name: 'mock.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('mock-only') });
    await page.getByTestId('word-parse-button').click();
    await page.getByTestId('word-suggestion-apply-s1').click();
    await expect.poll(() => Boolean(release)).toBe(true);
    if (changedContext === 'project') {
      await page.getByRole('button', { name: '另一部作品', exact: true }).click();
      await expect(page.getByTestId('editor-textarea')).toHaveValue('另一部作品的原稿：他在站台等天亮。');
    } else {
      await page.getByTestId('workspace-tab-write').click();
      if (changedContext === 'chapter') {
        for (const id of ['chapter-tree-volume-v1', 'chapter-tree-arc-a1']) {
          const node = page.getByTestId(id);
          if (await node.getAttribute('aria-expanded') === 'false') await node.click();
        }
        await page.getByTestId('chapter-tree-item-c2').click();
      }
      else await page.getByTestId('editor-textarea').fill('建议请求期间的新草稿。');
    }
    const expected = changedContext === 'project' ? '另一部作品的原稿：他在站台等天亮。'
      : changedContext === 'chapter' ? '第二章原稿。' : '建议请求期间的新草稿。';
    await expect(page.getByTestId('editor-textarea')).toHaveValue(expected);
    const response = page.waitForResponse('**/api/import/apply-suggestion');
    release!();
    await response;
    await page.getByTestId('workspace-tab-outline').click();
    await page.getByTestId('workspace-tab-write').click();
    await expect(page.getByTestId('editor-textarea')).toHaveValue(expected);
    if (changedContext !== 'project') {
      await page.getByTestId('workspace-tab-import').click();
      await expect(page.getByTestId('word-import-panel')).toContainText('建议结果已保留但未覆盖当前正文');
      await expect(page.getByTestId('word-import-panel')).toContainText('迟到的建议正文。');
      await expect(page.getByTestId('word-suggestion-apply-s1')).toBeEnabled();
    }
    expect(state.errors).toEqual([]);
  });
}

test('an explicit no-plan started token is saved as an empty id and revision zero, while manual saves omit the pair', async ({ page }) => {
  const state = await installEditorFixture(page, { planId: '', revision: 0 });

  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanId');
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanRevision');

  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  await expect(page.getByTestId('editor-textarea')).toHaveValue('原稿：雨停后，她把信放回抽屉。生成正文。');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(2);
  expect(state.saveRequests[1].stylePlanId).toBe('');
  expect(state.saveRequests[1].stylePlanRevision).toBe(0);
  expect(state.unexpected).toEqual([]);
});

test('malformed started metadata rolls back generated prose so it cannot be committed as a manual save', async ({ page }) => {
  const state = await installEditorFixture(page, { missingStartedToken: true });
  const original = '原稿：雨停后，她把信放回抽屉。';
  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(original);
  await expect(page.getByTestId('editor-save-message')).toContainText('未收到有效的风格计划版本');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  expect(state.saveRequests[0].content).toBe(original);
  expect(state.saveRequests[0].content).not.toContain('生成正文。');
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanId');
  expect(state.saveRequests[0]).not.toHaveProperty('stylePlanRevision');
});

test('a malformed retry restores the prior generated draft token for the next save', async ({ page }) => {
  const state = await installEditorFixture(page);

  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  state.missingStartedToken = true;
  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-save-message')).toContainText('未收到有效的风格计划版本');
  await expect(page.getByTestId('editor-textarea')).toHaveValue('原稿：雨停后，她把信放回抽屉。生成正文。');

  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  expect(state.saveRequests[0].stylePlanId).toBe('plan-1');
  expect(state.saveRequests[0].stylePlanRevision).toBe(1);
});

test('continuation locks chapter navigation while the current style contract is pending', async ({ page }) => {
  const state = await installEditorFixture(page);
  state.holdContract = true;

  await page.getByTestId('editor-ai-continue').click();
  await expect.poll(() => state.contractPaused).toBe(true);
  await page.getByTestId('chapter-tree-arc-a1').click();
  await page.getByTestId('chapter-tree-item-c2').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('原稿：雨停后，她把信放回抽屉。');
  await expect(page.getByTestId('editor-textarea')).toHaveValue('原稿：雨停后，她把信放回抽屉。');

  state.releaseContract?.();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  await page.getByTestId('chapter-tree-item-c2').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章原稿。');
  await expect(page.getByTestId('editor-textarea')).not.toHaveValue(/生成正文。/);
});

test('task chapter navigation preserves an unsaved draft and permits returning to its own chapter', async ({ page }) => {
  const state = await installEditorFixture(page);
  await page.route(`**/api/ai/project/${EDITOR_PID}/history`, (route) => route.fulfill({ json: { success: true, data: [] } }));
  await page.route(`**/api/projects/${EDITOR_PID}/conflicts`, (route) => route.fulfill({
    json: { success: true, data: ['c1', 'c2'].map((chapterId) => ({
      id: `conflict-${chapterId}`, chapterId, projectId: EDITOR_PID, content: '模拟写作冲突',
      summary: '模拟写作冲突', rootCause: '', createdAt: NOW,
    })) },
  }));
  await page.reload();
  await page.getByTestId('workspace-tab-write').click();
  await page.getByTestId('editor-textarea').fill('第一章尚未保存的修改。');
  await page.getByTestId('workspace-tab-tasks').click();
  await page.getByRole('tab', { name: /写作冲突/ }).click();
  await page.getByTestId('conflict-card-conflict-c2').getByRole('button', { name: '查看章节' }).click();
  await expect(page.getByText('请先保存当前章节的未保存修改。', { exact: true })).toBeVisible();
  await expect(page.getByTestId('conflict-card-conflict-c2')).toBeVisible();
  await page.getByTestId('conflict-card-conflict-c1').getByRole('button', { name: '查看章节' }).click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('第一章尚未保存的修改。');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  await expect(page.getByTestId('editor-save-message')).toContainText('已保存');
  await page.getByTestId('workspace-tab-tasks').click();
  await page.getByRole('tab', { name: /写作冲突/ }).click();
  await page.getByTestId('conflict-card-conflict-c2').getByRole('button', { name: '查看章节' }).click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章原稿。');
  expect(state.errors).toEqual([]);
});

test('saving a generated draft prevents tab unmount until its acknowledgement clears the draft token', async ({ page }) => {
  const state = await installEditorFixture(page, { deferSaveResponse: true });
  await expect(page.getByTestId('editor-textarea')).toHaveValue('原稿：雨停后，她把信放回抽屉。');
  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  await page.getByTestId('workspace-tab-tasks').click();
  await expect(page.getByText('章节正在保存，请保存完成后切换视图。', { exact: true })).toBeVisible();
  await expect(page.getByTestId('editor-textarea')).toBeVisible();
  state.releaseSave?.();
  await expect(page.getByTestId('editor-save-message')).toContainText('已保存');
  await page.getByTestId('workspace-tab-outline').click();
  await expect(page.getByTestId('editor-textarea')).toHaveCount(0);
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);
  await page.getByTestId('editor-textarea').fill('保存成功后继续手工修改。');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(2);
  expect(state.saveRequests[0]).toMatchObject({ stylePlanId: 'plan-1', stylePlanRevision: 1 });
  expect(state.saveRequests[1]).not.toHaveProperty('stylePlanId');
  expect(state.saveRequests[1]).not.toHaveProperty('stylePlanRevision');
  state.releaseSave?.();
  expect(state.errors).toEqual([]);
});

test('a late continuation response from the old chapter cannot replace the newly selected chapter', async ({ page }) => {
  const state = await installEditorFixture(page, { deferContinueResponse: true });

  await page.getByTestId('editor-ai-continue').click();
  await expect.poll(() => state.continueCount).toBe(1);
  const beforeForcedSwitch = await page.evaluate(async () => {
    // HMR can give the application's singleton a timestamped URL. Import that
    // exact module instead of accidentally creating a second isolated store.
    const loadedStoreUrl = performance.getEntriesByType('resource').map((entry) => entry.name)
      .find((url) => new URL(url).pathname === '/src/stores/index.ts');
    const { useEditorStore } = await import(loadedStoreUrl || '/src/stores/index.ts');
    const snapshot = useEditorStore.getState();
    useEditorStore.getState().setCurrentChapter('c2');
    return { projectId: snapshot.activeProjectId, chapterId: snapshot.currentChapterId };
  });
  expect(beforeForcedSwitch).toEqual({ projectId: EDITOR_PID, chapterId: 'c1' });
  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章原稿。');
  state.releaseContinue?.();

  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章原稿。');
  await expect(page.getByTestId('editor-ai-continue')).toBeEnabled();
  expect(state.saveRequests).toHaveLength(0);
  expect(state.errors).toEqual([]);
});

test('a late save response from the old chapter cannot mark the newly selected chapter saved', async ({ page }) => {
  const state = await installEditorFixture(page, { deferSaveResponse: true });

  await page.getByTestId('editor-textarea').fill('第一章的新稿。');
  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);
  await page.evaluate(async () => {
    const loadedStoreUrl = performance.getEntriesByType('resource').map((entry) => entry.name)
      .find((url) => new URL(url).pathname === '/src/stores/index.ts');
    const { useEditorStore } = await import(loadedStoreUrl || '/src/stores/index.ts');
    useEditorStore.getState().setCurrentChapter('c2');
  });
  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章原稿。');
  state.releaseSave?.();

  await expect(page.getByTestId('editor-textarea')).toHaveValue('第二章原稿。');
  await expect(page.getByTestId('editor-save-message')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('switching project mid-continuation aborts the old request and never touches the new project', async ({ page }) => {
  // 侧边栏「最近项目」点击走客户端路由，**不刷新页面**；编辑器也没有按 projectId 加 key，
  // 所以这是真实的同页项目切换：同一个组件实例接着服务新项目。
  const aborted: string[] = [];
  page.on('requestfailed', (request) => {
    if (request.url().includes('/ai/continue-writing')) {
      aborted.push(request.failure()?.errorText ?? 'unknown');
    }
  });
  const state = await installEditorFixture(page, { sibling: true, deferContinueResponse: true });

  await page.getByTestId('editor-ai-continue').click();
  await expect.poll(() => state.continueCount).toBe(1);
  await expect(page.getByTestId('editor-ai-continue')).toBeDisabled();

  // 在续写仍在途时切到另一个项目
  await page.getByRole('button', { name: '另一部作品', exact: true }).click();
  await expect(page.getByRole('heading', { name: '另一部作品', exact: true })).toBeVisible();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('另一部作品的原稿：他在站台等天亮。');

  // 放行旧项目那份响应：它已被中止，绝不能写进新项目的正文
  state.releaseContinue?.();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('另一部作品的原稿：他在站台等天亮。');

  // 旧请求必须被真正中止，而不是继续在 provider 上跑到结束（那是实打实的费用）
  await expect.poll(() => aborted.length).toBe(1);

  // 中止后不再有 onDone 收尾，按钮状态靠切项目的清理复位，不能卡在「生成中」
  await expect(page.getByTestId('editor-ai-continue')).toBeEnabled();
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
  expect(state.errors).toEqual([]);
  expect(state.unexpected, `未处理的请求：\n${state.unexpected.join('\n')}`).toEqual([]);
});

test('save stays disabled while continuation is awaiting its stream response', async ({ page }) => {
  const state = await installEditorFixture(page, { deferContinueResponse: true });

  await page.getByTestId('editor-ai-continue').click();
  await expect.poll(() => state.continueCount).toBe(1);
  await expect(page.getByTestId('editor-save')).toBeDisabled();
  state.releaseContinue?.();
  await expect(page.getByTestId('editor-save')).toBeEnabled();
});

test('auto-write sends a paired token read from the latest style contract', async ({ page }) => {
  const state = await installEditorFixture(page);
  await page.getByTestId('editor-auto-write-mode').selectOption('single');
  await page.getByTestId('editor-auto-write').click();
  await expect.poll(() => state.autoStartRequests.length).toBe(1);
  expect(state.autoStartRequests[0].stylePlanId).toBe('plan-1');
  expect(state.autoStartRequests[0].stylePlanRevision).toBe(1);
});

test('auto-write fails closed when the current style contract cannot be read', async ({ page }) => {
  const state = await installEditorFixture(page);
  state.failContract = true;
  await page.getByTestId('editor-auto-write-mode').selectOption('single');
  await page.getByTestId('editor-auto-write').click();
  await expect(page.getByTestId('editor-status-bar')).toContainText('读取当前风格过渡计划失败');
  expect(state.autoStartRequests).toHaveLength(0);
});

test('a stale plan between generation and save is rejected with 409 and the prose is kept', async ({ page }) => {
  const state = await installEditorFixture(page, { conflictOnSave: true });
  const original = await page.getByTestId('editor-textarea').inputValue();

  await page.getByTestId('editor-ai-continue').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue(/生成正文。/);

  await page.getByTestId('editor-save').click();
  await expect.poll(() => state.saveRequests.length).toBe(1);

  // 409 可执行提示；正文与已有草稿都保留
  await expect(page.getByTestId('editor-save-message')).toContainText('风格过渡计划');
  const after = await page.getByTestId('editor-textarea').inputValue();
  expect(after).toContain('生成正文。');
  expect(after).not.toBe(original);
  // 不插入本地兜底占位文本，不自动重试、不再次调用模型
  expect(after).not.toContain('本地');
  expect(after).not.toContain('降级');
  expect(state.continueCount).toBe(1);
  expect(state.saveRequests.length).toBe(1);
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
});

// ══════════════════════════════════════════════════════
// I. 候选版本展示与陈旧候选
// ══════════════════════════════════════════════════════

test('a candidate shows its plan version and accepting a stale one keeps the card', async ({ page }) => {
  const state = await installEditorFixture(page);

  // 写作页右侧的紧凑助手面板：口语化改写指令会走正文候选通道
  const assistant = page.getByTestId('assistant-panel-compact');
  const input = assistant.getByTestId('chat-input');
  await expect(input).toBeEnabled();
  await input.fill('调整本章正文');
  await input.press('Enter');

  await expect(assistant.getByTestId('copilot-candidate')).toBeVisible();
  await expect(assistant.getByTestId('copilot-style-plan-version')).toContainText('风格计划版本 1');
  // 创建候选必须带当前计划版本
  expect(state.candidateRequests).toHaveLength(1);
  expect(state.candidateRequests[0].stylePlanId).toBe('plan-1');
  expect(state.candidateRequests[0].stylePlanRevision).toBe(1);

  // 计划在候选创建之后发生变化
  state.planId = 'plan-2';
  state.planRevision = 2;

  await assistant.getByTestId('copilot-accept').click();
  // 陈旧候选不被写入，且卡片与候选都还在，并提供用户主动的重生成入口
  expect(state.acceptCount).toBe(1);
  await expect(assistant.getByTestId('copilot-candidate')).toBeVisible();
  await expect(assistant.getByTestId('copilot-stale-notice')).toContainText('计划已更新');
  await expect(assistant.getByTestId('copilot-regenerate-after-conflict')).toBeVisible();
  // 不自动重试
  await expect.poll(() => state.acceptCount).toBe(1);
  await expect(page.getByTestId('app-error-boundary')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

// ══════════════════════════════════════════════════════
// J. 计划冲突任务：展示保留候选，不自动恢复
// ══════════════════════════════════════════════════════

test('latest-job conflict is rendered once and recoverable query only supplies resumable jobs', async ({ page }) => {
  const state = await installEditorFixture(page);
  state.latestJob = {
    id: 'job-conflict', project_id: EDITOR_PID, mode: 'first-unfinished', status: 'failed',
    message: '风格计划已变化，旧候选已保留。', target_words: 0, total_chapters: 3,
    completed_chapters: 1, failed_chapters: 1, fallback_chapters: 0, generated_words: 0,
    current_chapter_id: 'c1', current_chapter_title: '第一章', llm_provider: '', llm_model: '',
    last_generation_source: '', started_at: NOW, updated_at: NOW, finished_at: NOW, error: '',
    retry_count: 0, max_retries: 0, checkpoint_type: 'style_plan_conflict', recoverable: false,
    resume_target_json: JSON.stringify({ chapter_id: 'c1', statusCode: 409, stylePlanId: 'plan-1', stylePlanRevision: 1,
      candidateContent: '未保存的候选正文：她把信交到门外的人手里。' }),
    preview_chapter_id: 'c1', preview_content: '未保存的候选正文：她把信交到门外的人手里。',
  };
  await page.route(`**/api/ai/project/${EDITOR_PID}/recoverable`, (route) => route.fulfill({
    json: { success: true, data: [{
      id: 'job-normal', project_id: EDITOR_PID, mode: 'first-unfinished', status: 'interrupted',
      message: '已中断', target_words: 0, total_chapters: 3, completed_chapters: 1, failed_chapters: 0,
      fallback_chapters: 0, generated_words: 0, current_chapter_id: 'c1', current_chapter_title: '第一章',
      llm_provider: '', llm_model: '', last_generation_source: '', started_at: NOW, updated_at: NOW,
      finished_at: null, error: '', retry_count: 0, max_retries: 2, checkpoint_type: '', recoverable: true,
      resume_target_json: '',
    }] },
  }));

  await page.reload();
  await page.getByTestId('workspace-tab-write').click();

  const banner = page.getByTestId('editor-style-conflict-banner');
  await expect(page.getByTestId('editor-style-conflict-banner')).toHaveCount(1);
  await expect(page.getByTestId('editor-style-plan-conflict-notice')).toHaveCount(0);
  await expect(banner).toContainText('计划变化，未保存候选已保留');
  await expect(page.getByTestId('editor-style-conflict-preview')).toContainText('未保存的候选正文');
  // 冲突任务不能被当成可恢复任务，绝不出现「恢复任务」按钮
  await expect(banner.getByTestId('editor-resume-button')).toHaveCount(0);
  // 普通可恢复任务仍然保留恢复入口（证明确实是按任务类型区分，而非一概不显示）
  const normal = page.getByTestId('editor-resume-banner');
  await expect(normal).toBeVisible();
  await expect(normal.getByTestId('editor-resume-button')).toHaveCount(1);
  expect(state.errors).toEqual([]);
});

test('regenerating a candidate appends a new candidate turn and preserves the previous candidate card', async ({ page }) => {
  const state = await installEditorFixture(page);
  const assistant = page.getByTestId('assistant-panel-compact');
  await assistant.getByTestId('chat-input').fill('调整本章正文');
  await assistant.getByTestId('chat-input').press('Enter');
  await expect(assistant.getByTestId('copilot-candidate')).toHaveCount(1);

  await assistant.getByTestId('copilot-regenerate').first().click();
  await expect(assistant.getByTestId('copilot-candidate')).toHaveCount(2);
  expect(state.candidateRequests).toHaveLength(2);
  expect(state.candidateRequests[1]).not.toHaveProperty('regenerateMessageId');
  await expect(assistant.getByTestId('copilot-candidate').first()).toContainText('候选：她握紧信封，推开门。');
  await expect(assistant.getByTestId('copilot-candidate').last()).toContainText('候选文本');
});

test('a legacy candidate without a token is not presented as a known no-plan candidate', async ({ page }) => {
  const state = await installEditorFixture(page, { candidateMissingToken: true });
  const assistant = page.getByTestId('assistant-panel-compact');
  await assistant.getByTestId('chat-input').fill('调整本章正文');
  await assistant.getByTestId('chat-input').press('Enter');
  await expect(assistant.getByTestId('copilot-style-plan-version')).toContainText('未记录风格计划版本');
  // 未知状态不得冒充「未启用计划」
  await expect(assistant.getByTestId('copilot-style-plan-version')).not.toContainText('未启用渐进计划');
  // 并且必须自带显式的重新生成入口，而不是等 Accept 被 409 才提示
  await expect(assistant.getByTestId('copilot-unrecorded-plan-notice')).toContainText('未记录风格计划版本');
  await expect(assistant.getByTestId('copilot-regenerate-unrecorded')).toBeVisible();
  expect(state.candidateRequests).toHaveLength(1);
  expect(state.candidateRequests[0].stylePlanId).toBe('plan-1');
  expect(state.candidateRequests[0].stylePlanRevision).toBe(1);

  // 点该入口要真的发起新一轮：新增候选卡，旧卡保留
  await assistant.getByTestId('copilot-regenerate-unrecorded').click();
  await expect(assistant.getByTestId('copilot-candidate')).toHaveCount(2);
  expect(state.candidateRequests).toHaveLength(2);
  expect(state.candidateRequests[1].stylePlanRevision).toBe(1);
});

test('a candidate generated with a known no-plan contract carries the explicit empty token', async ({ page }) => {
  const state = await installEditorFixture(page, { planId: '', revision: 0 });
  const assistant = page.getByTestId('assistant-panel-compact');
  await assistant.getByTestId('chat-input').fill('调整本章正文');
  await assistant.getByTestId('chat-input').press('Enter');
  await expect(assistant.getByTestId('copilot-style-plan-version')).toContainText('未启用渐进计划');
  expect(state.candidateRequests[0].stylePlanId).toBe('');
  expect(state.candidateRequests[0].stylePlanRevision).toBe(0);
});

test('accepting a current candidate refreshes editor, chapter consumers, and transition progress', async ({ page }) => {
  const state = await installEditorFixture(page);
  const assistant = page.getByTestId('assistant-panel-compact');
  await assistant.getByTestId('chat-input').fill('调整本章正文');
  await assistant.getByTestId('chat-input').press('Enter');
  await expect(assistant.getByTestId('copilot-candidate')).toBeVisible();
  await page.evaluate(() => {
    window.addEventListener('novelcraft:style-transition-changed', (event) => {
      (window as Window & { acceptedTransition?: unknown }).acceptedTransition = (event as CustomEvent).detail;
    }, { once: true });
    window.addEventListener('novelcraft:chapter-changed', (event) => {
      (window as Window & { acceptedChapterChange?: unknown }).acceptedChapterChange = (event as CustomEvent).detail;
    }, { once: true });
  });
  await assistant.getByTestId('copilot-accept').click();
  await expect(page.getByTestId('editor-textarea')).toHaveValue('候选：她握紧信封，推开门。');
  await expect.poll(() => page.evaluate(() => (window as Window & { acceptedTransition?: { transition?: { completedCount?: number } } }).acceptedTransition?.transition?.completedCount)).toBe(1);
  await expect.poll(() => page.evaluate(() => (window as Window & { acceptedChapterChange?: { action?: string; chapterId?: string; appliedContent?: string } }).acceptedChapterChange)).toEqual({
    projectId: EDITOR_PID,
    chapterId: 'c1',
    action: 'copilot-accept',
    appliedContent: '候选：她握紧信封，推开门。',
  });
  expect(state.acceptCount).toBe(1);
});

test('a streamed style-plan conflict job is displayed even though it is absent from recoverable jobs', async ({ page }) => {
  const state = await installEditorFixture(page);
  state.latestJob = {
    id: 'job-stream', project_id: EDITOR_PID, mode: 'single', requested_chapter_id: 'c1', status: 'running',
    message: '创作中', target_words: 500, total_chapters: 1, completed_chapters: 0, failed_chapters: 0,
    fallback_chapters: 0, generated_words: 0, current_chapter_id: 'c1', current_chapter_title: '第一章',
    llm_provider: '', llm_model: '', last_generation_source: '', started_at: NOW, updated_at: NOW,
    finished_at: null, error: '', retry_count: 0, max_retries: 0,
  };
  const conflict = {
    ...state.latestJob,
    status: 'failed', checkpoint_type: 'style_plan_conflict', recoverable: false,
    preview_chapter_id: 'c1', preview_content: 'SSE 保留的候选正文。',
    message: '风格计划已变化，旧候选已保留。',
  };
  await page.route('**/api/ai/job-stream/events', (route) => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
    body: `event: job\ndata: ${JSON.stringify(conflict)}\n\n`,
  }));

  await page.reload();
  await page.getByTestId('workspace-tab-write').click();
  await expect(page.getByTestId('editor-style-conflict-banner')).toContainText('计划变化，未保存候选已保留');
  await expect(page.getByTestId('editor-style-conflict-preview')).toContainText('SSE 保留的候选正文');
  await expect(page.getByTestId('editor-style-conflict-banner')).toHaveCount(1);
  expect(state.errors).toEqual([]);
});
