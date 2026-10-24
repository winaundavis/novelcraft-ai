import type {
  ApiResponse,
  Chapter,
  ChapterTrashBatchDeleteResult,
  ChapterTrashBatchRestoreResult,
  ChapterTrashList,
  ChapterTrashRecord,
  Character,
  CharacterAnalysisResult,
  CharacterProfileAnalysis,
  ChatContextOptions,
  ChatMessage,
  ChatConversation,
  StoryState,
  CopilotCandidate,
  ContinueRequest,
  DeaiResult,
  AuthAccount,
  AuthPermission,
  BoundInvite,
  Foreshadow,
  ForeshadowDraftResult,
  ForeshadowListData,
  ForeshadowSuggestion,
  FulltextAskResult,
  FulltextStats,
  GenerateCharacterRequest,
  GenerateOutlineRequest,
  LLMConfig,
  LLMHealth,
  MemoryProjection,
  NovelJobPayload,
  Outline,
  Project,
  ReferenceActiveItem,
  ReferenceWork,
  ReferenceWorkSummary,
  ReplayProjectionResult,
  ReviewRequest,
  StartNovelJobRequest,
  StyleProfile,
  StyleTransition,
  StyleTransitionTarget,
  StylePlanToken,
  ContinueResult,
  TransitionPace,
  UsernameAvailability,
  WritingConflict,
} from '../types';
import {
  buildFallbackCharacter,
  buildFallbackContinuation,
  buildFallbackOutline,
  buildFallbackReviewResult,
  looksGarbled,
  normalizeChapter,
  normalizeChapters,
  normalizeCharacter,
  normalizeChatReply,
  normalizeForeshadowListData,
  normalizeForeshadowSuggestion,
  normalizeOutline,
  normalizeProject,
  normalizeProjects,
  normalizeReviewResult,
  normalizeStyleProfile,
  normalizeStyleTransition,
} from './normalize';
import { getInviteHeaders } from './betaAccess';
import { TERMS_VERSION } from './terms';

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');

// 降级提示事件名：AI 请求失败并走本地 fallback 时触发，UI 层监听后向用户提示质量下降。
export const AI_DEGRADED_EVENT = 'novelcraft:ai-degraded';

/** 触发一次"AI 降级"全局事件（fallback 生效时调用）。 */
function emitDegradedNotice(kind: string, detail?: { errorCode?: string | null; traceId?: string }): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(AI_DEGRADED_EVENT, { detail: { kind, ...detail } }));
}

/**
 * 带状态码的请求错误。
 * - status 为 HTTP 状态码；网络失败/超时为 null。
 * - isNetworkError：无法与服务端建立/完成请求（网络中断、DNS、CORS、超时）。
 * - isTimeout：本地超时中止。
 * UI 依赖它区分 401（未认证）与网络/5xx（连接失败），不再把二者混为"密码错误"。
 */
export class ApiError extends Error {
  readonly status: number | null;
  readonly isNetworkError: boolean;
  readonly isTimeout: boolean;
  readonly traceId?: string;
  /** 后端结构化 detail 里的业务错误码（如 style_plan_conflict）；普通文本 detail 时为 undefined。 */
  readonly code?: string;

  constructor(
    message: string,
    options: { status?: number | null; isTimeout?: boolean; traceId?: string; code?: string } = {},
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = options.status ?? null;
    this.isTimeout = options.isTimeout ?? false;
    this.isNetworkError = this.status === null && !this.isTimeout;
    this.traceId = options.traceId;
    this.code = options.code;
  }
}

/** 判断错误是否为服务端"未认证"（401）。 */
export function isUnauthorizedError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** 老 `.env` 邀请码映射到的账号行不存在（唯一的"用码进不去但允许回落"场景）。 */
export const LEGACY_ACCOUNT_MISSING_CODE = 'legacy_account_missing';

/**
 * 判断这个邀请码失败是否属于"老 `.env` 码指向的账号不存在"。
 *
 * **只有这一种情况**才允许前端回落到「请求头带码」的遗留通道（Q2=A）。
 * 其余任何失败（码写错、还没被注册、已停用）都必须留在弹层里如实告知——
 * 把它们也当成"老码缺账号"会把错码写进 sessionStorage，从而关掉登录闸门，
 * 用户被放进创作台却每个接口都 401。
 */
export function isLegacyAccountMissing(error: unknown): boolean {
  return error instanceof ApiError && error.code === LEGACY_ACCOUNT_MISSING_CODE;
}

/**
 * 判断错误是否为服务端限流（429）。
 * 限流是**可重试的暂时状态**，不代表未认证：不能把用户当成"未登录"而弹登录层。
 */
export function isRateLimitedError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 429;
}

/** 风格过渡计划冲突（409）：服务端拒绝用陈旧的计划版本写正文或接受候选。 */
export const STYLE_PLAN_CONFLICT_CODE = 'style_plan_conflict';

export function isStylePlanConflict(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  if (error.code === STYLE_PLAN_CONFLICT_CODE) return true;
  // 兼底：旧路由可能只返回纯文本 detail，用提示文案识别。
  return error.message.includes('风格过渡计划');
}

/** 判断错误是否为连接层失败（网络中断 / 超时 / 5xx），区别于明确的 4xx 业务错误。 */
export function isConnectionError(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.isNetworkError || error.isTimeout || (error.status !== null && error.status >= 500);
  }
  return true;
}

const REQUEST_TIMEOUT_MS = 60_000;
const FAST_AI_TIMEOUT_MS = 20_000;
// 创建改写候选走**写作角色**：后端 ROLE_TIMEOUT_SECONDS["writing"] = 600s。
// 这里原用默认 60s，整章改写/长段落经常 60s 以上 → 前端先超时中断，
// 表现就是「一键修改改到几条就失败」（服务端其实还在跑）。取 180s 作平衡。
const CANDIDATE_TIMEOUT_MS = 180_000;
// 综合审校 = 语言审校 + 逻辑审校两次**串行** LLM 调用；后端 check 角色单次上限 90s。
// 真实数据（arts/deepseek-v4-flash）单次 check 为 15-63s，两次串行远超 20s；
// 浏览器截止时间必须高于 2×90s，让后端返回分类错误，而不是被前端中断后静默降级。
const REVIEW_TIMEOUT_MS = 210_000;
// Planning can legitimately take up to the backend's 180s role timeout when
// Arts/DeepSeek thinking is enabled. Keep a safety margin so the browser does
// not switch to a local outline while the backend is still completing.
const OUTLINE_TIMEOUT_MS = 240_000;
// Keep the browser deadline above the backend planning deadline (180s), so the
// backend can return a classified provider error instead of a client abort.
const CHAT_PLANNING_TIMEOUT_MS = 210_000;
const CONTINUE_WRITING_TIMEOUT_MS = 180_000;
const IMPORT_TIMEOUT_MS = 45_000;
// 扫描/入库接口要对**整本书**跑一遍分词与实体抽取：实测 120 章小说约 47 秒。
// 45 秒的上限会把「后端其实已经成功」的响应提前掐掉，前端只能看到
// `signal is aborted without reason`，看起来像导入失败。全书量级给足 5 分钟。
const MARKDOWN_UPLOAD_TIMEOUT_MS = 300_000;
const ANALYZE_CHARACTERS_TIMEOUT_MS = 120_000;
// 角色档案分析：后端用 planning 角色（180s 上限）通读正文与大纲，
// 浏览器截止时间必须高于它，让后端返回分类错误而不是被前端中断。
const CHARACTER_PROFILE_TIMEOUT_MS = 200_000;
// M4 深度文风分析：采样 + LLM 结构化抽取四组规则，可能耗时较长
const STYLE_ANALYZE_TIMEOUT_MS = 120_000;
const FULLTEXT_TIMEOUT_MS = 300_000; // 全文问答：大书（50 万字）直灌 LLM 首响应可能达数分钟

async function request<T>(path: string, options?: RequestInit, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json', ...getInviteHeaders(), ...options?.headers },
      credentials: 'include',
      signal: controller.signal,
      ...options,
    });

    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ detail: response.statusText }));
      const rawDetail = (errorBody as { detail?: unknown })?.detail;
      // 后端可能返回结构化 detail（如注销被拒、风格计划冲突的 {code, message}）；
      // 优先展示其用户可读文案，同时保留 code 供 UI 区分冲突类型。
      const detail = typeof rawDetail === 'string'
        ? rawDetail
        : rawDetail && typeof rawDetail === 'object' && typeof (rawDetail as { message?: unknown }).message === 'string'
          ? (rawDetail as { message: string }).message
          : `HTTP ${response.status}`;
      const code =
        rawDetail && typeof rawDetail === 'object' && typeof (rawDetail as { code?: unknown }).code === 'string'
          ? (rawDetail as { code: string }).code
          : undefined;

      if (
        response.status === 503
        && detail.includes('No LLM provider is configured')
      ) {
        throw new ApiError('尚未配置可用的 AI 服务，请先到设置中添加并启用 API。', { status: response.status, code });
      }

      throw new ApiError(detail, { status: response.status, code });
    }

    return response.json();
  } catch (error: unknown) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ApiError('请求超时，请确认前后端服务和模型服务都在运行。', { isTimeout: true });
    }
    throw new ApiError(error instanceof Error ? error.message : '网络请求失败，请检查网络连接后重试。');
  } finally {
    window.clearTimeout(timeoutId);
  }
}

export const authApi = {
  /** 用户名 + 密码登录。明确同意本次阅读的协议版本后才可登录。 */
  login: (username: string, password: string, termsAccepted = false) =>
    request<ApiResponse<AuthAccount>>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password, termsAccepted, termsVersion: termsAccepted ? TERMS_VERSION : '' }),
    }),
  /**
   * **只用邀请码**登录（Q1=C：码就是账号的凭据）。
   *
   * 老 `.env` 码映射到的身份可能没有 `users` 行（迁移前），后端无法为它建会话，
   * 此时返回 401 —— 调用方需回落到既有的「邀请码即凭据（请求头带码）」通道。
   */
  loginWithInvite: (invite: string, termsAccepted = false) =>
    request<ApiResponse<AuthAccount>>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: '', password: '', invite, termsAccepted, termsVersion: termsAccepted ? TERMS_VERSION : '' }),
    }),
  /**
   * 注册：邀请码 + 用户名 + 密码。成功后**服务端已种好会话 cookie**（自动登录，Q4）。
   * `termsAccepted` 在服务端是**强制**的：没勾选会返回 400。
   */
  register: (username: string, password: string, invite: string, termsAccepted = false) =>
    request<ApiResponse<AuthAccount>>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ username, password, invite, termsAccepted, termsVersion: termsAccepted ? TERMS_VERSION : '' }),
    }),
  /** 注册表单失焦时查询用户名占用（不合规也会返回原因，前端直接显示）。 */
  usernameAvailable: (username: string) =>
    request<ApiResponse<UsernameAvailability>>(`/auth/username-available?username=${encodeURIComponent(username)}`),
  /** 当前账号已绑定的邀请码（只有 hint，不回显明文；没绑定则空数组）。 */
  myInvites: () =>
    request<ApiResponse<Array<Record<string, any>>>>('/auth/invites/mine').then((json) => ({
      ...json,
      data: (json.data || []).map((row) => ({
        codeHint: String(row?.code_hint ?? row?.codeHint ?? ''),
        label: String(row?.label ?? ''),
        boundAt: String(row?.bound_at ?? row?.boundAt ?? ''),
      })) as BoundInvite[],
    })),
  me: () => request<ApiResponse<AuthAccount>>('/auth/me'),
  /**
   * 当前凭据的权限（目前只有 `isAdmin`）。
   *
   * **与后端 `require_maintainer` 共用同一套解析与判定**，所以管理入口显示与否
   * 不会与接口放行与否分裂。未登录时后端返回 401。
   */
  permissions: () => request<ApiResponse<AuthPermission>>('/auth/permissions'),
  logout: () => request<ApiResponse<null>>('/auth/logout', { method: 'POST' }),
  deleteAccount: () => request<ApiResponse<null>>('/auth/account', { method: 'DELETE' }),
};

function buildApiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

interface OutlineGenerationMeta {
  mode?: 'llm' | 'fallback';
  error_code?: string | null;
  trace_id?: string;
}

export const projectApi = {
  list: async () => {
    const json = await request<ApiResponse<Project[]>>('/projects/');
    return { ...json, data: normalizeProjects(json.data || []) };
  },

  get: async (id: string) => {
    const json = await request<ApiResponse<Project>>(`/projects/${id}`);
    return { ...json, data: normalizeProject(json.data) };
  },

  create: (data: Partial<Project>) =>
    request<ApiResponse<Project>>('/projects/', {
      method: 'POST',
      body: JSON.stringify(data),
    }).then((json) => ({ ...json, data: normalizeProject(json.data) })),

  update: (id: string, data: Partial<Project>) =>
    request<ApiResponse<Project>>(`/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }).then((json) => ({ ...json, data: normalizeProject(json.data) })),

  delete: (id: string) =>
    request<ApiResponse<null>>(`/projects/${id}`, {
      method: 'DELETE',
    }),

  // 一键分析作品人物与人物关系（AI 分析全文，可能耗时较长）
  analyzeCharacters: (id: string) =>
    request<ApiResponse<CharacterAnalysisResult>>(
      `/projects/${id}/analyze-characters`,
      { method: 'POST' },
      ANALYZE_CHARACTERS_TIMEOUT_MS,
    ).then((json) => ({
      ...json,
      data: {
        characters: json.data?.characters || [],
        relations: json.data?.relations || [],
        summary: json.data?.summary || '',
      },
    })),

  // 通读正文与大纲，为指定角色生成各档案字段的建议值。
  // 失败时不抛硬错误：返回 degraded + reason，由面板就地提示，绝不静默覆盖作者内容。
  analyzeCharacterProfile: (projectId: string, characterName: string) =>
    request<ApiResponse<CharacterProfileAnalysis>>(
      `/projects/${projectId}/analyze-character-profile`,
      { method: 'POST', body: JSON.stringify({ characterName }) },
      CHARACTER_PROFILE_TIMEOUT_MS,
    )
      .then((json) => ({
        ...json,
        data: {
          suggestions: json.data?.suggestions || {},
          rejected: json.data?.rejected || {},
          omitted: json.data?.omitted || [],
          inferred: json.data?.inferred || [],
          degraded: Boolean(json.data?.degraded),
          reason: json.data?.reason ?? null,
          basis: json.data?.basis ?? null,
        },
      }))
      .catch((error: unknown) => {
        const reason = describeCharacterProfileFailure(error);
        emitDegradedNotice('character-profile', { errorCode: reason.code });
        return {
          success: true,
          data: {
            suggestions: {},
            rejected: {},
            omitted: [],
            inferred: [],
            degraded: true,
            reason: reason.message,
            basis: null,
          },
        };
      }),
};

export const chapterApi = {
  list: (projectId: string) =>
    request<ApiResponse<Chapter[]>>(`/projects/${projectId}/chapters/`)
      .then((json) => ({ ...json, data: normalizeChapters(json.data || []) })),

  get: (projectId: string, chapterId: string) =>
    request<ApiResponse<Chapter>>(`/projects/${projectId}/chapters/${chapterId}`)
      .then((json) => ({ ...json, data: normalizeChapter(json.data) })),

  save: (
    projectId: string,
    chapterId: string,
    payload: {
      content: string;
      title?: string;
      volumeId?: string;
      order?: number;
      /**
       * 生成这段正文时服务端下发的计划版本；必须原样回传。
       * 计划已变更时服务端以 409 拒绝写入，**不会**静默覆盖正文。
       */
      stylePlanId?: string;
      stylePlanRevision?: number;
    },
  ) =>
    request<ApiResponse<Chapter & { styleTransition?: StyleTransition }>>(
      `/projects/${projectId}/chapters/${chapterId}`,
      {
        method: 'PUT',
        body: JSON.stringify(payload),
      },
    ).then((json) => {
      // 后端把最新的 styleTransition 放在 data 内与 chapter 同级，保存后可直接刷新进度。
      const { styleTransition, ...chapterFields } = (json.data ?? {}) as Chapter & {
        styleTransition?: StyleTransition;
      };
      return {
        ...json,
        data: {
          ...(normalizeChapter(chapterFields as Chapter) as Chapter),
          styleTransition: normalizeStyleTransition(styleTransition) ?? undefined,
        },
      };
    }),

  create: (projectId: string, data: Partial<Chapter>) =>
    request<ApiResponse<Chapter>>(`/projects/${projectId}/chapters/`, {
      method: 'POST',
      body: JSON.stringify(data),
    }).then((json) => ({ ...json, data: normalizeChapter(json.data) })),

  delete: (projectId: string, chapterId: string) =>
    request<ApiResponse<null>>(`/projects/${projectId}/chapters/${chapterId}`, {
      method: 'DELETE',
    }),

  // P1：记忆投影状态（正文已保存但记忆未同步的可见状态 + 单项重试）
  listProjections: (projectId: string, chapterId: string) =>
    request<ApiResponse<MemoryProjection[]>>(
      `/projects/${projectId}/chapters/${chapterId}/projections`,
    ),

  replayProjection: (projectId: string, chapterId: string, projectionType: string) =>
    request<ApiResponse<ReplayProjectionResult>>(
      `/projects/${projectId}/chapters/${chapterId}/projections/${projectionType}/replay`,
      { method: 'POST' },
    ),
};

export const chapterTrashApi = {
  list: (projectId: string) => request<ApiResponse<ChapterTrashList>>(`/projects/${projectId}/chapter-trash`),
  get: (projectId: string, id: string) => request<ApiResponse<ChapterTrashRecord & { content: string }>>(
    `/projects/${projectId}/chapter-trash/${id}`,
  ),
  restore: (projectId: string, id: string, data: {
    targetChapterId: string;
    outlineHash: string;
    targetContentHash: string;
    confirmReplace: boolean;
  }) => request<ApiResponse<{ chapterId: string; displacedTrashId: string | null }>>(
    `/projects/${projectId}/chapter-trash/${id}/restore`, { method: 'POST', body: JSON.stringify(data) },
  ),
  delete: (projectId: string, id: string) => request<ApiResponse<null>>(
    `/projects/${projectId}/chapter-trash/${id}?confirm=true`, { method: 'DELETE' },
  ),
  /**
   * 批量还原：每条记录各自回到**它自己的原章节**，因此不需要逐条指定目标。
   * 冲突（原章节已不在大纲 / 已有正文）由服务端跳过并逐条返回原因，不会覆盖正文。
   */
  batchRestore: (projectId: string, ids: string[], outlineHash: string) =>
    request<ApiResponse<ChapterTrashBatchRestoreResult>>(
      `/projects/${projectId}/chapter-trash/batch-restore`,
      { method: 'POST', body: JSON.stringify({ ids, outlineHash }) },
    ),
  /** 批量永久删除。已被删除的 id 会在 `missing` 里返回，而不是整体报错。 */
  batchDelete: (projectId: string, ids: string[]) =>
    request<ApiResponse<ChapterTrashBatchDeleteResult>>(
      `/projects/${projectId}/chapter-trash/batch-delete`,
      { method: 'POST', body: JSON.stringify({ ids }) },
    ),
};

export const aiApi = {
  conversations: {
    list: (projectId: string, archived = false) => request<ApiResponse<{ records: ChatConversation[] }>>(
      `/projects/${projectId}/chat-conversations?archived=${archived}`),
    create: (projectId: string) => request<ApiResponse<ChatConversation>>(`/projects/${projectId}/chat-conversations`, {
      method: 'POST', body: JSON.stringify({}),
    }),
    get: (projectId: string, id: string) => request<ApiResponse<ChatConversation>>(`/projects/${projectId}/chat-conversations/${id}`),
    update: (projectId: string, id: string, data: { archived?: boolean; title?: string }) => request<ApiResponse<ChatConversation>>(
      `/projects/${projectId}/chat-conversations/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
    delete: (projectId: string, id: string) => request<ApiResponse<null>>(`/projects/${projectId}/chat-conversations/${id}`, { method: 'DELETE' }),
  },
  generateOutline: (data: GenerateOutlineRequest) =>
    request<ApiResponse<Outline & OutlineGenerationMeta>>(
      '/ai/generate-outline',
      {
        method: 'POST',
        body: JSON.stringify(data),
      },
      OUTLINE_TIMEOUT_MS,
    )
      .then((json) => {
        const meta: OutlineGenerationMeta = {
          mode: json.data?.mode,
          error_code: json.data?.error_code,
          trace_id: json.data?.trace_id,
        };
        if (meta.mode === 'fallback') {
          emitDegradedNotice('outline', { errorCode: meta.error_code, traceId: meta.trace_id });
        }
        return {
          ...json,
          meta,
        data: normalizeOutline(json.data, {
          premise: data.premise,
          structure: data.structure,
          targetWordCount: data.targetWordCount,
          targetChapters: data.targetChapters,
          }),
        };
      })
      .catch(() => {
        emitDegradedNotice('outline', { errorCode: 'client_request_failed' });
        return {
          success: true,
          data: buildFallbackOutline(data),
          meta: { mode: 'fallback' as const, error_code: 'client_request_failed', trace_id: '' },
        };
      }),

  generateCharacter: (data: GenerateCharacterRequest) =>
    request<ApiResponse<Character>>(
      '/ai/generate-character',
      {
        method: 'POST',
        body: JSON.stringify(data),
      },
      FAST_AI_TIMEOUT_MS,
    )
      .then((json) => ({ ...json, data: normalizeCharacter(json.data, data.hints) }))
      .catch(() => {
        emitDegradedNotice('character');
        return {
          success: true,
          data: buildFallbackCharacter(data),
        };
      }),

  /**
   * 流式续写。签名向后兼容：`onMetadata` 为可选第 4 参。
   *
   * 两条硬约束：
   * 1. SSE 首个事件形如 `{chunk:'',status:'started',stylePlanId,stylePlanRevision}`，
   *    只走 `onMetadata`——**不能**当正文交给 `onChunk`，否则草稿开头会多出空事件。
   * 2. 计划版本冲突（409）由服务端在开流前拒绝，此时**没有产生任何模型输出**：
   *    不注入本地兜底文案、不自动重试、不伪造"已生成"，只把可操作信息交给 `onDone.error`，
   *    并保留原文与已有草稿。
   */
  continueWriting: (
    data: ContinueRequest,
    onChunk: (chunk: string) => void,
    onDone: (result?: ContinueResult) => void,
    onMetadata?: (token: StylePlanToken) => void,
  ) => {
    const abortController = new AbortController();
    const timeoutId = window.setTimeout(() => abortController.abort(), CONTINUE_WRITING_TIMEOUT_MS);

    fetch(`${API_BASE}/ai/continue-writing`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getInviteHeaders() },
      body: JSON.stringify(data),
      signal: abortController.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          // 开流前的错误是结构化 detail：优先取 {code, message}，保留 409 语义。
          const rawBody = await response.text().catch(() => '');
          let code: string | undefined;
          let message = rawBody || '续写请求失败。';
          try {
            const parsed = JSON.parse(rawBody) as { detail?: unknown };
            const detail = parsed?.detail;
            if (detail && typeof detail === 'object') {
              const structured = detail as { code?: unknown; message?: unknown };
              if (typeof structured.code === 'string') code = structured.code;
              if (typeof structured.message === 'string') message = structured.message;
            } else if (typeof detail === 'string') {
              message = detail;
            }
          } catch {
            // 非 JSON 响应：正文文本就是提示语。
          }
          throw new ApiError(message, { status: response.status, code });
        }

        const reader = response.body?.getReader();
        if (!reader) {
          onDone();
          return;
        }

        const decoder = new TextDecoder();
        let buffer = '';
        let generationIncomplete = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const events = buffer.split('\n\n');
          buffer = events.pop() || '';

          for (const event of events) {
            const line = event.split('\n').find((item) => item.startsWith('data: '));
            if (!line) continue;

            const payload = line.slice(6);
            if (payload === '[DONE]') {
              window.clearTimeout(timeoutId);
              onDone({ generationIncomplete });
              return;
            }

            try {
              const parsed = JSON.parse(payload);
              if (parsed.generation_incomplete === true) {
                generationIncomplete = true;
                continue;
              }
              // 开流事件只声明计划版本，不是正文；误当 chunk 会在文首凭空插入内容。
              if (parsed.status === 'started') {
                if (
                  onMetadata
                  && typeof parsed.stylePlanId === 'string'
                  && typeof parsed.stylePlanRevision === 'number'
                ) {
                  onMetadata({
                    stylePlanId: parsed.stylePlanId,
                    stylePlanRevision: parsed.stylePlanRevision,
                  });
                }
                continue;
              }
              if (parsed.chunk) onChunk(parsed.chunk);
            } catch {
              onChunk(payload);
            }
          }
        }

        window.clearTimeout(timeoutId);
        onDone({ generationIncomplete });
      })
      .catch((error) => {
        window.clearTimeout(timeoutId);

        if (abortController.signal.aborted || (error instanceof Error && error.name === 'AbortError')) {
          onDone();
          return;
        }

        // 计划冲突：服务端根本没开始生成。这里绝不能降级成"本地兜底正文"，
        // 否则作者会把占位文本当成模型输出保存，且完全不知情。
        if (isStylePlanConflict(error)) {
          onDone({
            error: {
              code: STYLE_PLAN_CONFLICT_CODE,
              message:
                error instanceof Error && error.message
                  ? error.message
                  : '风格过渡计划或进度已更新，正文候选已保留且未覆盖正文；请基于最新计划重新生成。',
              status: 409,
            },
          });
          return;
        }

        console.error('AI stream error:', error);
        emitDegradedNotice('continue-writing');
        // 非冲突失败沿用既有降级行为：插入本地草案并提示 AI 服务降级。
        onChunk(`\n\n${buildFallbackContinuation(data.contextBefore, data.direction, data.wordCount)}`);
        onDone({
          error: {
            code: error instanceof ApiError ? error.code : undefined,
            message: error instanceof Error ? error.message : '续写请求失败。',
            status: error instanceof ApiError ? (error.status ?? undefined) : undefined,
          },
        });
      });

    return abortController;
  },

  chat: (
    projectId: string,
    messages: Omit<ChatMessage, 'id' | 'timestamp'>[],
    context: ChatContextOptions = {},
  ) =>
    request<ApiResponse<{
      reply: string;
      context?: { profile: string; chars: number; budget: number; action?: string };
      action?: string;
      mode?: 'llm' | 'fallback' | 'failed';
      is_local_suggestion?: boolean;
      error_code?: string | null;
      request_id?: string;
      trace_id?: string;
      intent?: 'consultation' | 'outline_edit';
      outline_failure_stage?: 'scope' | 'json_extract' | 'normalize' | 'merge' | 'validate' | 'candidate_create';
      outline_failure_code?: string;
      candidate?: import('../types').OutlineCandidate;
    }>>(
      '/ai/chat',
      {
        method: 'POST',
        body: JSON.stringify({ projectId, messages, ...context }),
      },
      CHAT_PLANNING_TIMEOUT_MS,
    )
      .then((json) => ({
        ...json,
        data: {
          ...(json.data || {}),
          reply: normalizeChatReply(json.data?.reply || ''),
        },
      })),

  outlineCandidates: {
    list: (projectId: string, status = 'proposed') =>
      request<ApiResponse<import('../types').OutlineCandidate[]>>(`/projects/${projectId}/outline-candidates?status=${encodeURIComponent(status)}`),
    apply: (id: string, confirmSavedChapterRemoval = false) => request<ApiResponse<import('../types').OutlineCandidate>>(
      `/outline-candidates/${id}/apply${confirmSavedChapterRemoval ? '?confirmSavedChapterRemoval=true' : ''}`,
      { method: 'POST' },
    ),
    reject: (id: string) => request<ApiResponse<import('../types').OutlineCandidate>>(`/outline-candidates/${id}/reject`, { method: 'POST' }),
    revert: (id: string) => request<ApiResponse<import('../types').OutlineCandidate>>(`/outline-candidates/${id}/revert`, { method: 'POST' }),
  },

  enhance: (text: string, mode: string, intensity: number) =>
    request<ApiResponse<{ result: string }>>('/ai/enhance', {
      method: 'POST',
      body: JSON.stringify({ text, mode, intensity }),
    }),

  // 建议1 去 AI 味后处理：正文 + 项目画像反 AI 规则 + 检测结果 → LLM 重写
  // chapterId 用于跨章检测（开头重复 / 场景关键词组）。
  deai: (projectId: string, text: string, chapterId = '') =>
    request<ApiResponse<DeaiResult>>(
      '/ai/deai',
      {
        method: 'POST',
        body: JSON.stringify({ projectId, text, chapterId }),
      },
      FAST_AI_TIMEOUT_MS,
    ).catch(() => {
      emitDegradedNotice('deai');
      return {
        success: true,
        data: {
          result: text,
          changes: [],
          degraded: true,
          scoreBefore: 100,
          scoreAfter: 100,
          segments: 0,
          keptWindows: 0,
        },
      };
    }),

  // 长篇创作任务
  novelJob: {
    start: (payload: StartNovelJobRequest) =>
      request<ApiResponse<NovelJobPayload>>('/ai/start', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    getByProject: (projectId: string) =>
      request<ApiResponse<NovelJobPayload | null>>(`/ai/project/${projectId}`),

    getRecoverable: (projectId: string) =>
      request<ApiResponse<NovelJobPayload[]>>(`/ai/project/${projectId}/recoverable`),

    getHistory: (projectId: string) =>
      request<ApiResponse<NovelJobPayload[]>>(`/ai/project/${projectId}/history`),

    get: (jobId: string) =>
      request<ApiResponse<NovelJobPayload>>(`/ai/${jobId}`),

    resume: (jobId: string, skipFailedChapter = false) =>
      request<ApiResponse<NovelJobPayload>>(
        `/ai/${jobId}/resume?skip_failed_chapter=${skipFailedChapter}`,
        {
          method: 'POST',
        },
      ),

    cancel: (jobId: string) =>
      request<ApiResponse<NovelJobPayload>>(`/ai/${jobId}/cancel`, {
        method: 'POST',
      }),

    subscribe: (
      jobId: string,
      handlers: {
        onMessage: (job: NovelJobPayload) => void;
        onError?: (event: Event) => void;
      },
    ) => {
      // 原 EventSource 无法携带 X-NovelCraft-Invite，生产模式下 SSE 会被 401，
      // 前端只能在停止任务后通过普通 GET 看到最终正文。使用 fetch 流式读取，
      // 与其它 API 共用邀请码请求头。
      const controller = new AbortController();
      let closed = false;
      void (async () => {
        try {
          const response = await fetch(buildApiUrl(`/ai/${jobId}/events`), {
            headers: { ...getInviteHeaders() },
            signal: controller.signal,
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const reader = response.body?.getReader();
          if (!reader) throw new Error('任务流不可用');
          const decoder = new TextDecoder();
          let buffer = '';
          while (!closed) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const events = buffer.split('\n\n');
            buffer = events.pop() || '';
            for (const event of events) {
              const line = event.split('\n').find((item) => item.startsWith('data: '));
              if (!line) continue;
              try {
                handlers.onMessage(JSON.parse(line.slice(6)) as NovelJobPayload);
              } catch (error) {
                console.error('Novel job stream parse error:', error);
              }
            }
          }
        } catch (error) {
          if (!closed) handlers.onError?.(error as Event);
        }
      })();

      return () => {
        closed = true;
        controller.abort();
      };
    },
  },

  copilot: {
    /**
     * 创建改写候选。`stylePlanId`/`stylePlanRevision` 应为**当前最新**计划版本：
     * 服务端会用 `validate_generation_token` 校验，陈旧则 409（不会启动模型）。
     * 创建候选**不**推进完成数，只有 Accept 才推进。
     */
    createCandidate: (data: {
      projectId: string;
      chapterId: string;
      action: string;
      selectionText?: string;
      currentText?: string;
      instruction?: string;
      conversationId?: string;
      requestId?: string;
      regenerateMessageId?: string;
      stylePlanId?: string;
      stylePlanRevision?: number;
    }) =>
      request<ApiResponse<CopilotCandidate>>(
        '/copilot/candidates',
        { method: 'POST', body: JSON.stringify(data) },
        CANDIDATE_TIMEOUT_MS,
      ),
    /**
     * 接受候选：服务端用候选**持久化时**记录的 token 校验，
     * 而不是面板上当前看到的计划。计划已变则 409，候选与正文都不会被改动。
     * 成功时 data 上额外带 `applied_content` 与 `styleTransition`。
     */
    accept: (id: string) =>
      request<ApiResponse<CopilotCandidate>>(`/copilot/candidates/${id}/accept`, { method: 'POST' }),
    reject: (id: string) => request<ApiResponse<CopilotCandidate>>(`/copilot/candidates/${id}/reject`, { method: 'POST' }),
    /** 重新生成会返回**新的**候选 id；旧候选不会被自动拒绝或改写。 */
    regenerate: (id: string, instruction = '') =>
      request<ApiResponse<CopilotCandidate>>(
        `/copilot/candidates/${id}/regenerate`,
        {
          method: 'POST',
          body: JSON.stringify({ instruction }),
        },
        CANDIDATE_TIMEOUT_MS,
      ),
  },

  // P2-2b：写作冲突（任务中心冲突视图）
  conflicts: {
    list: (projectId: string) =>
      request<ApiResponse<WritingConflict[]>>(`/projects/${projectId}/conflicts`),

    resolve: (projectId: string, factId: string) =>
      request<ApiResponse<null>>(`/projects/${projectId}/conflicts/${factId}`, {
        method: 'PATCH',
      }),
  },
};

/**
 * 把审校失败翻译成用户能据以行动的原因。
 * 之前所有失败都被笼统说成"AI 服务降级"，导致限流/未配置/超时无法区分。
 */
function describeReviewFailure(error: unknown): { code: string; message: string } {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return {
        code: 'rate_limited',
        message: '后端请求过于频繁（按 IP 的每分钟配额已用尽），与模型无关。请等待约一分钟后重试。',
      };
    }
    if (error.status === 401 || error.status === 403) {
      return { code: 'unauthorized', message: `没有审校权限或登录已失效：${error.message}` };
    }
    if (error.status === 503) {
      return { code: 'provider_unconfigured', message: `尚未配置可用的 AI 服务：${error.message}` };
    }
    if (error.isTimeout) {
      return {
        code: 'client_timeout',
        message: `审校等待超过 ${Math.round(REVIEW_TIMEOUT_MS / 1000)} 秒仍未返回。综合审校需要两次串行模型调用，耗时较长，请稍后重试。`,
      };
    }
    if (error.status !== null) {
      return { code: `http_${error.status}`, message: `模型服务返回错误：${error.message}` };
    }
  }
  return {
    code: 'network',
    message: `审校请求失败：${error instanceof Error ? error.message : '未知错误'}`,
  };
}

/**
 * 把「AI 分析角色档案」的失败翻译成作者能据以行动的原因。
 * 与审校一样，不能把所有失败笼统说成"AI 服务降级"。
 */
function describeCharacterProfileFailure(error: unknown): { code: string; message: string } {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return {
        code: 'rate_limited',
        message: '后端请求过于频繁（按 IP 的每分钟配额已用尽），与模型无关。请等待约一分钟后重试。',
      };
    }
    if (error.status === 404) {
      return { code: 'character_missing', message: `角色不存在或已被重命名：${error.message}` };
    }
    if (error.status === 401 || error.status === 403) {
      return { code: 'unauthorized', message: `没有该项目的分析权限或登录已失效：${error.message}` };
    }
    if (error.status === 503) {
      return { code: 'provider_unconfigured', message: `尚未配置可用的 AI 服务：${error.message}` };
    }
    if (error.isTimeout) {
      return {
        code: 'client_timeout',
        message: `等待超过 ${Math.round(CHARACTER_PROFILE_TIMEOUT_MS / 1000)} 秒仍未返回。角色档案分析需要通读正文与大纲，请稍后重试。`,
      };
    }
    if (error.status !== null) {
      return { code: `http_${error.status}`, message: `模型服务返回错误：${error.message}` };
    }
  }
  return {
    code: 'network',
    message: `角色档案分析请求失败：${error instanceof Error ? error.message : '未知错误'}`,
  };
}

export const reviewApi = {
  comprehensive: (payload: ReviewRequest) =>
    request<ApiResponse<unknown>>(
      '/review/comprehensive',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      REVIEW_TIMEOUT_MS,
    )
      .then((json) => ({
        ...json,
        data: normalizeReviewResult((json.data || {}) as Record<string, unknown>, payload.text),
      }))
      .catch((error: unknown) => {
        // 保底返回本地审校，但不伪造成功：把真实原因带回面板。
        const reason = describeReviewFailure(error);
        emitDegradedNotice('review', { errorCode: reason.code });
        return {
          success: true,
          data: {
            ...buildFallbackReviewResult(payload.text),
            degraded: true,
            degradedReason: reason.message,
          },
        };
      }),
};

/**
 * 上传式 Markdown 导入的公共请求体。
 *
 * `files` 与 `relativePaths` 必须**成对、同序**追加：后端按下标对齐，
 * 用相对路径还原目录结构（选整个文件夹时会带上层级）。
 */
async function postMarkdownUpload(
  path: string,
  files: File[],
  relativePaths: string[],
  extra?: { projectId?: string; projectName?: string; excludedAnalysis?: Record<string, string[]>; mergeMode?: 'replace' | 'merge' },
) {
  const formData = new FormData();
  files.forEach((file, index) => {
    formData.append('files', file, file.name);
    formData.append('relativePaths', relativePaths[index] ?? file.name);
  });
  if (extra?.projectId) formData.append('projectId', extra.projectId);
  if (extra?.projectName) formData.append('projectName', extra.projectName);
  // 写进已有项目时怎么处理既有章节：replace（全部移入回收站）/ merge（按章节序号合并）
  if (extra?.mergeMode) formData.append('mergeMode', extra.mergeMode);
  // 作者叉掉的误识别条目，按分类分组：后端只删不补，
  // 保证入库的分析结果与作者看到的列表一致
  if (extra?.excludedAnalysis && Object.values(extra.excludedAnalysis).some((list) => list.length)) {
    formData.append('excludedAnalysis', JSON.stringify(extra.excludedAnalysis));
  }

  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), MARKDOWN_UPLOAD_TIMEOUT_MS);

  try {
    const response = await fetch(buildApiUrl(path), {
      method: 'POST',
      body: formData,
      headers: getInviteHeaders(),
      signal: controller.signal,
    });

    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(errorBody.detail || 'Markdown 章节导入失败。');
    }

    return await response.json();
  } catch (error) {
    // 超时中断原本只会丢出 `signal is aborted without reason`，对作者毫无意义。
    // 翻译成「等了多久、可以怎么办」。
    if (error instanceof Error && (error.name === 'AbortError' || error.name === 'TimeoutError')) {
      const seconds = Math.round(MARKDOWN_UPLOAD_TIMEOUT_MS / 1000);
      throw new Error(
        `导入分析超时（已等待 ${seconds} 秒）。长篇小说扫描较慢，可稍后重试，或分几次导入。`,
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

export const importApi = {
  /**
   * 把导入扫描里**勾选**的候选人物加入项目档案（只增不改）。
   *
   * 导入本身不写人物（识别是启发式，误判应由作者挑掉），候选留在项目的
   * `importAnalysis` 里由「人物」页展示；这个接口负责落库，返回更新后的整个项目。
   */
  acceptAnalysisCharacters: (projectId: string, names: string[]) =>
    request<ApiResponse<{ project: Project; added: number }>>(
      `/import/projects/${projectId}/characters/from-analysis`,
      { method: 'POST', body: JSON.stringify({ names }) },
    ).then((json) => ({
      ...json,
      data: json.data ? { ...json.data, project: normalizeProject(json.data.project) } : json.data,
    })),

  markdownFolder: async (folderPath: string) => {
    const formData = new FormData();
    formData.append('folderPath', folderPath);

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), MARKDOWN_UPLOAD_TIMEOUT_MS);

    const response = await fetch(buildApiUrl('/import/markdown-folder'), {
      method: 'POST',
      body: formData,
      headers: getInviteHeaders(),
      signal: controller.signal,
    });

    window.clearTimeout(timeoutId);

    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(errorBody.detail || 'Markdown 章节文件夹扫描失败。');
    }

    const json = await response.json();
    if (json?.data?.analysis?.summary && looksGarbled(json.data.analysis.summary)) {
      json.data.analysis.summary = '章节已扫描完成，但自动剧情摘要暂时不可用。';
    }
    return json;
  },

  ingestMarkdownFolder: async (folderPath: string, projectId?: string, projectName?: string, mergeMode?: 'replace' | 'merge') => {
    const formData = new FormData();
    formData.append('folderPath', folderPath);
    if (projectId) formData.append('projectId', projectId);
    if (projectName) formData.append('projectName', projectName);
    if (mergeMode) formData.append('mergeMode', mergeMode);

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), MARKDOWN_UPLOAD_TIMEOUT_MS);

    const response = await fetch(buildApiUrl('/import/markdown-folder/ingest'), {
      method: 'POST',
      body: formData,
      headers: getInviteHeaders(),
      signal: controller.signal,
    });

    window.clearTimeout(timeoutId);

    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(errorBody.detail || 'Markdown 章节导入失败。');
    }

    const json = await response.json();
    if (json?.data?.project) {
      json.data.project = normalizeProject(json.data.project);
    }
    if (json?.data?.analysis?.summary && looksGarbled(json.data.analysis.summary)) {
      json.data.analysis.summary = '章节已导入成功，但自动分析摘要暂时不可用。';
    }
    return json;
  },

  /**
   * 上传 Markdown 章节文件并扫描。
   *
   * 这是**生产环境唯一可用**的 Markdown 导入通道：路径式接口读服务端任意路径，
   * 在 `DEBUG=false` 下一律 403，因此前端默认走这里。
   */
  markdownFiles: async (files: File[], relativePaths: string[]) => {
    const json = await postMarkdownUpload('/import/markdown-files', files, relativePaths);
    if (json?.data?.analysis?.summary && looksGarbled(json.data.analysis.summary)) {
      json.data.analysis.summary = '章节已扫描完成，但自动剧情摘要暂时不可用。';
    }
    return json;
  },

  ingestMarkdownFiles: async (
    files: File[],
    relativePaths: string[],
    projectId?: string,
    projectName?: string,
    excludedAnalysis?: Record<string, string[]>,
    mergeMode?: 'replace' | 'merge',
  ) => {
    const json = await postMarkdownUpload('/import/markdown-files/ingest', files, relativePaths, {
      projectId,
      projectName,
      excludedAnalysis,
      mergeMode,
    });
    if (json?.data?.project) {
      json.data.project = normalizeProject(json.data.project);
    }
    if (json?.data?.analysis?.summary && looksGarbled(json.data.analysis.summary)) {
      json.data.analysis.summary = '章节已导入成功，但自动分析摘要暂时不可用。';
    }
    return json;
  },

  suggestionDocxPath: async (filePath: string) => {
    const formData = new FormData();
    formData.append('filePath', filePath);

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), IMPORT_TIMEOUT_MS);

    const response = await fetch(buildApiUrl('/import/suggestion-docx'), {
      method: 'POST',
      body: formData,
      headers: getInviteHeaders(),
      signal: controller.signal,
    });

    window.clearTimeout(timeoutId);

    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(errorBody.detail || '建议书解析失败。');
    }

    return response.json();
  },

  suggestionDocxFile: async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), IMPORT_TIMEOUT_MS);

    const response = await fetch(buildApiUrl('/import/suggestion-docx'), {
      method: 'POST',
      body: formData,
      headers: getInviteHeaders(),
      signal: controller.signal,
    });

    window.clearTimeout(timeoutId);

    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(errorBody.detail || '建议书解析失败。');
    }

    return response.json();
  },

  applySuggestion: (payload: { projectId: string; suggestionId: string; action: string; target: string; context: string }) =>
    request<ApiResponse<{ result: string; status: string }>>('/import/apply-suggestion', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
};

export const settingsApi = {
  // 与后端路由保持一致，避免 POST/认证请求经过 307 尾斜杠重定向。
  listLLMConfigs: () => request<ApiResponse<LLMConfig[]>>('/settings/llm-configs'),

  getLLMHealth: () => request<ApiResponse<LLMHealth>>('/settings/llm-health'),

  checkLLMHealth: () => request<ApiResponse<LLMHealth>>('/settings/llm-health', { method: 'POST' }),

  createLLMConfig: (data: Omit<LLMConfig, 'id'>) =>
    request<ApiResponse<LLMConfig>>('/settings/llm-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  activateLLMConfig: (id: string) =>
    request<ApiResponse<LLMConfig>>(`/settings/llm-configs/${id}/activate`, {
      method: 'POST',
    }),

  // 建议1 写作档位：更新指定配置的角色级模型覆盖（如 {"writing": {...}}）
  updateRoleOverrides: (id: string, roleOverrides: NonNullable<LLMConfig['roleOverrides']>) =>
    request<ApiResponse<LLMConfig>>(`/settings/llm-configs/${id}/role-overrides`, {
      method: 'PUT',
      body: JSON.stringify({ roleOverrides }),
    }),

  deleteLLMConfig: (id: string) =>
    request<ApiResponse<{ id: string }>>(`/settings/llm-configs/${id}`, {
      method: 'DELETE',
    }),
};

// M4 深度文风分析：触发/查询画像 + 设置/查询渐进过渡档位
// （POST 分析为同步执行，采样+LLM 抽取可能耗时较长，已用 STYLE_ANALYZE_TIMEOUT_MS）
export const styleApi = {
  analyze: (projectId: string) =>
    request<ApiResponse<StyleProfile>>(
      `/projects/${projectId}/style-analysis`,
      { method: 'POST' },
      STYLE_ANALYZE_TIMEOUT_MS,
    ).then((json) => ({ ...json, data: normalizeStyleProfile(json.data) })),

  getProfile: (projectId: string) =>
    request<ApiResponse<StyleProfile>>(`/projects/${projectId}/style-profile`)
      .then((json) => ({ ...json, data: normalizeStyleProfile(json.data) })),

  listProfiles: (projectId: string) =>
    request<ApiResponse<StyleProfile[]>>(`/projects/${projectId}/style-profiles`)
      .then((json) => ({
        ...json,
        data: (json.data || [])
          .map(normalizeStyleProfile)
          .filter((item): item is StyleProfile => item !== null),
      })),

  createGenreProfile: (projectId: string, genre: string, name = '') =>
    request<ApiResponse<StyleProfile>>(`/projects/${projectId}/genre-style-profile`, {
      method: 'POST',
      body: JSON.stringify({ genre, name }),
    }).then((json) => ({ ...json, data: normalizeStyleProfile(json.data) })),

  duplicateProfile: (projectId: string, profileId: string, name = '') =>
    request<ApiResponse<StyleProfile>>(`/projects/${projectId}/style-profiles/${profileId}/duplicate`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }).then((json) => ({ ...json, data: normalizeStyleProfile(json.data) })),

  activateProfile: (projectId: string, profileId: string) =>
    request<ApiResponse<StyleProfile>>(`/projects/${projectId}/style-profiles/${profileId}/activate`, {
      method: 'PUT',
    }).then((json) => ({ ...json, data: normalizeStyleProfile(json.data) })),

  /**
   * 设置/重设渐进过渡计划。每次成功都会产生新的 planId 与 revision。
   * - pace='custom' 必须给 targetChapters；快/中/慢的缺省值为 9/60/200 章。
   * - targetChapters 为严格整数 1–200，非法值后端返回 422。
   */
  setTransition: (
    projectId: string,
    payload: TransitionPace | { pace: TransitionPace; targetChapters?: number; targetStyle?: StyleTransitionTarget },
  ) => {
    const body = typeof payload === 'string' ? { pace: payload } : payload;
    return request<ApiResponse<StyleTransition>>(`/projects/${projectId}/style-transition`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }).then((json) => ({ ...json, data: normalizeStyleTransition(json.data) }));
  },

  getTransition: (projectId: string) =>
    request<ApiResponse<StyleTransition>>(`/projects/${projectId}/style-transition`)
      .then((json) => ({ ...json, data: normalizeStyleTransition(json.data) })),

  /**
   * 生成上下文契约（纯读）。`stylePlanId`/`stylePlanRevision` 在顶层：
   * 未启用 v2 计划时值为 `""` 与 `0`，**不能用 `|| undefined` 去掉**，
   * 否则无法区分“未启用”与“字段缺失”。
   */
  getContract: (projectId: string) =>
    request<
      ApiResponse<{
        styleTone: string;
        stylePlanId: string;
        stylePlanRevision: number;
        contract: unknown;
      }>
    >(`/projects/${projectId}/style-contract`),
};

export const storyStateApi = {
  get: (projectId: string) => request<ApiResponse<StoryState>>(`/projects/${projectId}/story-state`),
};

// ── P2 伏笔账本（长程跨卷张力）───────────────────────

export const foreshadowApi = {
  /** 伏笔列表（含一致性报告 / 超期计数） */
  list: (projectId: string) =>
    request<ApiResponse<ForeshadowListData>>(`/projects/${projectId}/foreshadows/`)
      .then((json) => ({ ...json, data: normalizeForeshadowListData(json.data) })),

  /** AI 建议候选（LLM 失败降级空列表，不报错） */
  suggest: (projectId: string) =>
    request<ApiResponse<ForeshadowSuggestion[]>>(
      `/projects/${projectId}/foreshadows/suggest`,
      { method: 'POST' },
      STYLE_ANALYZE_TIMEOUT_MS,
    ).then((json) => ({
      ...json,
      data: (json.data || [])
        .map(normalizeForeshadowSuggestion)
        .filter((item): item is ForeshadowSuggestion => item !== null),
    })),

  /** 确认建议后批量入库（status=planted） */
  create: (
    projectId: string,
    items: Array<{ name: string; description: string; plantChapterId: string; targetChapterId: string }>,
  ) =>
    request<ApiResponse<Foreshadow[]>>(`/projects/${projectId}/foreshadows/`, {
      method: 'POST',
      body: JSON.stringify({ items }),
    }),

  /** 把手写的一段文字整理成伏笔草稿（不落库；AI 不可用时 degraded=true + 中文原因） */
  draft: (projectId: string, text: string, plantChapterId = '') =>
    request<ApiResponse<ForeshadowDraftResult>>(
      `/projects/${projectId}/foreshadows/draft`,
      { method: 'POST', body: JSON.stringify({ text, plantChapterId }) },
      STYLE_ANALYZE_TIMEOUT_MS,
    ).then((json) => ({
      ...json,
      data: {
        items: (json.data?.items || [])
          .map(normalizeForeshadowSuggestion)
          .filter((item): item is ForeshadowSuggestion => item !== null),
        degraded: Boolean(json.data?.degraded),
        reason: json.data?.reason || '',
      },
    })),

  /** 手动改状态 / 描述 / 回收方式（可附事件备注与关联章节，用于手改正文后的补记） */
  update: (
    projectId: string,
    fid: string,
    patch: { name?: string; description?: string; status?: string; resolution?: string; note?: string; chapterId?: string },
  ) =>
    request<ApiResponse<Foreshadow>>(`/projects/${projectId}/foreshadows/${fid}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  /** 删除伏笔 */
  remove: (projectId: string, fid: string) =>
    request<ApiResponse<null>>(`/projects/${projectId}/foreshadows/${fid}`, {
      method: 'DELETE',
    }),
};

// ── P2-3 参考书档案（别人的作品；与文风档案分表、互不干扰） ──────────

/** 启用集合项（后端 activeReferences）：id / weight / name。 */
function normalizeActiveReference(row: any): ReferenceActiveItem {
  return {
    id: String(row?.id || ''),
    weight: Number(row?.weight) || 6,
    name: String(row?.name || ''),
  };
}

function normalizeActiveReferenceList(rows: any): ReferenceActiveItem[] {
  if (!Array.isArray(rows)) return [];
  return rows.map((row) => normalizeActiveReference(row)).filter((row) => row.id);
}

/** 后端返回 snake_case，这里统一转成前端 camelCase。 */
function normalizeReferenceWork<T extends Record<string, any>>(item: T): any {
  if (!item || typeof item !== 'object') return item;
  return {
    ...item,
    id: String(item.id || ''),
    projectId: String(item.project_id || item.projectId || ''),
    sourceProjectName: String(item.source_project_name ?? item.sourceProjectName ?? ''),
    name: String(item.name || ''),
    sourceNote: String(item.source_note ?? item.sourceNote ?? ''),
    charCount: Number(item.char_count ?? item.charCount ?? 0),
    storedChars: Number(item.stored_chars ?? item.storedChars ?? 0),
    truncated: Boolean(item.truncated),
    sampleChars: Number(item.sample_chars ?? item.sampleChars ?? 0),
    sampleCount: Number(item.sample_count ?? item.sampleCount ?? 0),
    status: (item.status === 'degraded' ? 'degraded' : 'completed') as 'completed' | 'degraded',
    statusReason: String(item.status_reason ?? item.statusReason ?? ''),
    llmUsed: Boolean(item.llm_used ?? item.llmUsed),
    tags: Array.isArray(item.tags) ? item.tags.map((tag: unknown) => String(tag)) : [],
    createdAt: String(item.created_at ?? item.createdAt ?? ''),
    updatedAt: String(item.updated_at ?? item.updatedAt ?? ''),
    rawText: String(item.raw_text ?? item.rawText ?? ''),
    report: String(item.report || ''),
    narrativeRules: item.narrative_rules || [],
    characterRules: item.character_rules || [],
    languageRules: item.language_rules || [],
    rhythmRules: item.rhythm_rules || [],
    antiAiRules: item.anti_ai_rules || [],
    strengths: item.strengths || [],
    weaknesses: item.weaknesses || [],
    metrics: item.metrics || {},
    dimensions: item.dimensions || {},
  };
}

export const referenceWorkApi = {
  /** 档案列表（不含原文）+ 当前启用为「创作参考」的那些（可多份 + 强度） */
  list: (projectId: string) =>
    request<
      ApiResponse<{
        items: ReferenceWorkSummary[];
        activeReferences?: unknown[];
        activeReferenceId?: string;
      }>
    >(`/projects/${projectId}/reference-works`).then((json) => ({
      ...json,
      data: {
        items: (json.data?.items || []).map((item) => normalizeReferenceWork(item)),
        activeReferences: normalizeActiveReferenceList(json.data?.activeReferences),
        activeReferenceId: String(json.data?.activeReferenceId || ''),
      },
    })),

  /**
   * **整体替换**「创作参考」集合（传空数组即全部停用）。
   * weight 只表示优先级：预算不够时先丢掉权重低的那些。
   */
  setActive: (projectId: string, items: Array<{ referenceId: string; weight: number }>) =>
    request<ApiResponse<{ activeReferences: ReferenceActiveItem[]; activeReferenceId?: string }>>(
      `/projects/${projectId}/reference-works/active`,
      { method: 'PUT', body: JSON.stringify({ items }) },
    ).then((json) => ({
      ...json,
      data: {
        activeReferences: normalizeActiveReferenceList(json.data?.activeReferences),
        activeReferenceId: String(json.data?.activeReferenceId || ''),
      },
    })),

  /** 单条详情（含原文，供回看） */
  get: (projectId: string, id: string) =>
    request<ApiResponse<ReferenceWork>>(`/projects/${projectId}/reference-works/${id}`)
      .then((json) => ({ ...json, data: normalizeReferenceWork(json.data) })),

  /** 建档：粘贴/上传的参考文本 → 拆解 + 落库（本地算指标 + 至多一次 AI 抽规则） */
  create: (
    projectId: string,
    payload: { name?: string; text: string; sourceNote?: string; tags?: string[] },
  ) =>
    request<ApiResponse<ReferenceWork>>(
      `/projects/${projectId}/reference-works`,
      { method: 'POST', body: JSON.stringify(payload) },
      STYLE_ANALYZE_TIMEOUT_MS,
    ).then((json) => ({ ...json, data: normalizeReferenceWork(json.data) })),

  /** 改名 / 改来源备注 / 改标签 */
  update: (
    projectId: string,
    id: string,
    payload: { name?: string; sourceNote?: string; tags?: string[] },
  ) =>
    request<ApiResponse<ReferenceWork>>(
      `/projects/${projectId}/reference-works/${id}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    ).then((json) => ({ ...json, data: normalizeReferenceWork(json.data) })),

  /** 用已存原文重新拆解（上次 AI 不可用时重试） */
  reanalyze: (projectId: string, id: string) =>
    request<ApiResponse<ReferenceWork>>(
      `/projects/${projectId}/reference-works/${id}/reanalyze`,
      { method: 'POST' },
      STYLE_ANALYZE_TIMEOUT_MS,
    ).then((json) => ({ ...json, data: normalizeReferenceWork(json.data) })),

  remove: (projectId: string, id: string) =>
    request<ApiResponse<null>>(`/projects/${projectId}/reference-works/${id}`, { method: 'DELETE' }),
};

// ── 建议2 全文问答（DeepSeek 前缀缓存低成本直灌核对） ──────────

export const fulltextApi = {
  /** 直灌全书 + 问题 → 回答（含本次消耗统计；LLM 失败降级 degraded=true，不抛错） */
  ask: (projectId: string, question: string) =>
    request<ApiResponse<FulltextAskResult>>(
      `/projects/${projectId}/fulltext-ask`,
      { method: 'POST', body: JSON.stringify({ question }) },
      FULLTEXT_TIMEOUT_MS,
    ).then((json) => ({
      ...json,
      data: {
        answer: json.data?.answer || '',
        degraded: json.data?.degraded ?? true,
        chars: json.data?.chars || 0,
        estimatedTokens: json.data?.estimatedTokens || 0,
        chapters: json.data?.chapters || 0,
        truncated: json.data?.truncated ?? false,
        estimatedCostUsd:
          json.data?.estimatedCostUsd === null || json.data?.estimatedCostUsd === undefined
            ? null
            : Number(json.data.estimatedCostUsd),
      },
    })),

  /** 全书直灌统计：字数 / token 估算 / 成本估算（前端展示"直灌成本约 $X"） */
  stats: (projectId: string) =>
    request<ApiResponse<FulltextStats>>(`/projects/${projectId}/fulltext-stats`)
      .then((json) => ({
        ...json,
        data: {
          chars: json.data?.chars || 0,
          estimatedTokens: json.data?.estimatedTokens || 0,
          chapters: json.data?.chapters || 0,
          truncated: json.data?.truncated ?? false,
          estimatedCostUsd:
            json.data?.estimatedCostUsd === null || json.data?.estimatedCostUsd === undefined
              ? null
              : Number(json.data.estimatedCostUsd),
        },
      })),
};

/* ============================ 运营后台（仅管理员） ============================
 * 服务端全部挂在 `require_maintainer` 后：这里只是调用，不承担权限判断。
 * 统计口径：calls / tokens 来自 project_llm_usage（项目+UTC 日）。
 * 隐私红线：后台只看数字，不取任何作品正文。
 */

export interface AdminReadiness {
  ready: boolean;
  errors: string[];
}

export interface AdminBackupStatus {
  lastBackupAt?: string;
  latestFile?: string;
  latestSizeBytes?: number;
  retained?: number;
  keep?: number;
  [key: string]: unknown;
}

export interface AdminDiskStatus {
  totalBytes: number;
  usedBytes: number;
  freeBytes: number;
  usedPercent: number;
}

export interface AdminOverview {
  readiness: AdminReadiness;
  serverTimeUtc: string;
  uptimeSeconds: number;
  backup: AdminBackupStatus | null;
  disk: AdminDiskStatus | null;
  counts: Record<string, number>;
}

export interface AdminUserRow {
  id: string;
  username: string;
  isAdmin: boolean;
  createdAt: string;
  projectCount: number;
  chapterCount: number;
  todayCalls: number;
  todayTokens: number;
  windowCalls: number;
  windowTokens: number;
}

export interface AdminUsageDay {
  date: string;
  calls: number;
  tokens: number;
}

export interface AdminUsageProject {
  projectId: string;
  name: string;
  ownerId: string;
  calls: number;
  tokens: number;
}

export interface AdminUsage {
  days: number;
  totals: { calls: number; tokens: number };
  byDay: AdminUsageDay[];
  byProject: AdminUsageProject[];
}

export interface AdminInviteRow {
  id: string;
  hint: string;
  label: string;
  status: string;
  usedCount: number;
  maxUses: number;
  expiresAt: string;
  boundUserId: string;
  boundUsername: string;
  createdAt: string;
}

export interface AdminCreatedInvite {
  codes: string[];
  count: number;
  label: string;
  maxUses: number;
  expiresAt: string;
  warning: string;
}

export const adminApi = {
  overview: () => request<ApiResponse<AdminOverview>>('/admin/overview'),
  users: (days = 7) =>
    request<ApiResponse<{ days: number; users: AdminUserRow[] }>>(`/admin/users?days=${days}`),
  usage: (days = 7) => request<ApiResponse<AdminUsage>>(`/admin/usage?days=${days}`),
  invites: () => request<ApiResponse<{ invites: AdminInviteRow[] }>>('/admin/invites'),
  createInvite: (payload: { label?: string; count?: number; maxUses?: number; expiresInDays?: number }) =>
    request<ApiResponse<AdminCreatedInvite>>('/admin/invites', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  disableInvite: (inviteId: string) =>
    request<ApiResponse<{ id: string; status: string }>>(`/admin/invites/${inviteId}/disable`, {
      method: 'POST',
    }),
};