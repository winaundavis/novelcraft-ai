export interface Project {
  id: string;
  name: string;
  subtitle: string;
  genre: Genre;
  targetWordCount: number;
  currentWordCount: number;
  status: 'planning' | 'drafting' | 'revising' | 'completed';
  createdAt: string;
  updatedAt: string;
  volumes: Volume[];
  characters: Character[];
  locations: Location[];
  worldRules: WorldRule[];
  styleConfig: StyleConfig;
  outline: Outline | null;
  tags: string[];
  isStarred: boolean;
  inspirationFragments?: InspirationFragment[];
  premise?: string;
  targetChapters?: number;
  importAnalysis?: {
    characters?: Array<{ name: string; frequency: number; aliases?: string[] }>;
    locations?: Array<{ name: string; frequency: number }>;
    timeline?: Array<{ text: string; type: string; chapter: number }>;
    foreshadowing?: Array<{ text: string; category: string; context: string }>;
    summary?: string;
  } | null;
}

export interface Volume {
  id: string;
  projectId: string;
  title: string;
  order: number;
  chapters: Chapter[];
}

export interface Chapter {
  id: string;
  volumeId: string;
  title: string;
  order: number;
  content: string;
  wordCount: number;
  status: 'draft' | 'writing' | 'completed' | 'revised';
  summary: string;
  notes: string;
  createdAt: string;
  updatedAt: string;
}

export interface ChapterTrashRecord {
  id: string;
  projectId: string;
  sourceChapterId: string;
  title: string;
  originalLocation: string;
  reason: string;
  removedAt: string;
  wordCount: number;
  contentHash: string;
}

export interface ChapterTrashTarget {
  chapterId: string;
  title: string;
  location: string;
  hasContent: boolean;
  contentHash: string;
}

/**
 * 批量还原里被跳过的记录。
 * `reason` 是稳定的机器可读 code，`message` 是可直接展示的中文原因——
 * 文案由服务端给出，避免前端各处各写一套。
 */
export interface ChapterTrashSkip {
  trashId: string;
  title: string;
  reason:
    | 'missing_record'
    | 'source_not_in_outline'
    | 'target_has_content'
    | 'duplicate_source'
    | 'restore_failed';
  message: string;
}

export interface ChapterTrashBatchRestoreResult {
  restored: Array<{ trashId: string; chapterId: string; title: string }>;
  skipped: ChapterTrashSkip[];
  outlineHash: string;
}

export interface ChapterTrashBatchDeleteResult {
  deleted: string[];
  /** 已被删除/从未存在过的记录：批量重试时属正常状态，不是错误。 */
  missing: string[];
}

export interface ChapterTrashList {
  records: ChapterTrashRecord[];
  targets: ChapterTrashTarget[];
  outlineHash: string;
}

export type Genre =
  | 'xuanhuan'
  | 'xianxia'
  | 'scifi'
  | 'urban'
  | 'romance'
  | 'mystery'
  | 'historical'
  | 'wuxia'
  | 'apocalypse'
  | 'unlimited_flow'
  | 'gaming'
  | 'spy'
  | 'business'
  | 'light_novel'
  | 'horror'
  | 'fantasy';

export const GENRE_LABELS: Record<Genre, string> = {
  xuanhuan: '玄幻',
  xianxia: '仙侠',
  scifi: '科幻',
  urban: '都市',
  romance: '言情',
  mystery: '悬疑推理',
  historical: '历史',
  wuxia: '武侠',
  apocalypse: '末日',
  unlimited_flow: '无限流',
  gaming: '游戏竞技',
  spy: '谍战',
  business: '商战',
  light_novel: '轻小说',
  horror: '惊悚',
  fantasy: '西方奇幻',
};

export interface StyleConfig {
  id: string;
  name: string;
  avgSentenceLength: number;
  dialogueRatio: number;
  descriptionDensity: number;
  paragraphLength: 'short' | 'medium' | 'long';
  tone: string;
  customPrompt: string;
}

export const STYLE_PRESETS: Record<string, Omit<StyleConfig, 'id' | 'customPrompt'>> = {
  concise: {
    name: '简洁冷峻',
    avgSentenceLength: 15,
    dialogueRatio: 0.3,
    descriptionDensity: 0.4,
    paragraphLength: 'short',
    tone: '简洁、冷静、精准',
  },
  poetic: {
    name: '诗意华丽',
    avgSentenceLength: 25,
    dialogueRatio: 0.25,
    descriptionDensity: 0.7,
    paragraphLength: 'medium',
    tone: '诗意、华美、富有韵律',
  },
  humorous: {
    name: '幽默诙谐',
    avgSentenceLength: 18,
    dialogueRatio: 0.5,
    descriptionDensity: 0.3,
    paragraphLength: 'short',
    tone: '轻松、机智、带反差感',
  },
  intense: {
    name: '紧张凌厉',
    avgSentenceLength: 12,
    dialogueRatio: 0.35,
    descriptionDensity: 0.5,
    paragraphLength: 'short',
    tone: '紧张、迅疾、压迫感强',
  },
  tender: {
    name: '细腻温情',
    avgSentenceLength: 20,
    dialogueRatio: 0.4,
    descriptionDensity: 0.55,
    paragraphLength: 'medium',
    tone: '温柔、克制、富有情绪余温',
  },
  epic: {
    name: '史诗宏大',
    avgSentenceLength: 28,
    dialogueRatio: 0.2,
    descriptionDensity: 0.65,
    paragraphLength: 'long',
    tone: '宏阔、庄重、具有史诗感',
  },
  lightNovel: {
    name: '日系轻小说',
    avgSentenceLength: 16,
    dialogueRatio: 0.55,
    descriptionDensity: 0.35,
    paragraphLength: 'short',
    tone: '轻快、口语化、青春感强',
  },
  western: {
    name: '西方奇幻',
    avgSentenceLength: 22,
    dialogueRatio: 0.3,
    descriptionDensity: 0.6,
    paragraphLength: 'medium',
    tone: '典雅、细致、具有异世界史观感',
  },
};

export type OutlineStructure = 'three_act' | 'kishotenketsu' | 'hero_journey' | 'free';

/** 大纲结构的中文名。唯一来源，展示与选择器都从这里取。 */
export const OUTLINE_STRUCTURE_LABELS: Record<OutlineStructure, string> = {
  three_act: '三幕式结构',
  kishotenketsu: '起承转合',
  hero_journey: '英雄之旅',
  free: '自由结构',
};

/**
 * 把 `outline.structure` 翻成中文名。
 *
 * 为什么要做拼写归一：后端声明的枚举是下划线（`schemas.py` 的
 * `Literal["three_act", ...]`），但导入链路（`api/imports.py`）历史上写入的是连字符
 * （`three-act`）。于是同一个字段在库里存在两种写法，直接查表会漏掉导入来的项目，
 * 把内部枚举值原样显示给作者。这里统一归一后再查表。
 *
 * 未知值**不猜**：原样返回，避免把不认识的数据悄悄显示成某个已知结构。
 */
export function outlineStructureLabel(value: string | null | undefined): string {
  const raw = (value ?? '').trim();
  if (!raw) return '未设置';
  const canonical = raw.toLowerCase().replace(/-/g, '_');
  return OUTLINE_STRUCTURE_LABELS[canonical as OutlineStructure] ?? raw;
}

export interface Outline {
  id: string;
  projectId: string;
  structure: OutlineStructure;
  premise: string;
  mainSummary: string;
  nodes: OutlineNode[];
}

export interface OutlineNode {
  id: string;
  parentId: string | null;
  type: 'volume' | 'arc' | 'chapter' | 'scene';
  arcId?: string | null;
  title: string;
  summary: string;
  conflict: string;
  revelation: string;
  emotion: string;
  isKeyPoint: boolean;
  order: number;
  targetWordCount: number;
  povCharacterId: string | null;
}

export interface OutlineCandidate {
  id: string;
  projectId: string;
  runId?: string;
  baseOutlineHash: string;
  baseOutlineVersion: number;
  candidateOutline: Outline | Record<string, unknown>;
  previousOutline?: Outline | Record<string, unknown> | null;
  diff: Record<string, unknown>;
  instruction: string;
  status: 'proposed' | 'applied' | 'rejected' | 'reverted' | 'conflicted';
  requiresConfirmation?: boolean;
  protectedRemovals?: Array<{ id: string; title: string }>;
}

export interface Character {
  id: string;
  projectId: string;
  name: string;
  aliases: string[];
  role: 'protagonist' | 'antagonist' | 'supporting' | 'mentor' | 'love_interest' | 'sidekick' | 'cameo';
  basicInfo: CharacterBasicInfo;
  personality: CharacterPersonality;
  background: string;
  coreTrauma: string;
  surfaceMotivation: string;
  deepDesire: string;
  arc: string;
  catchphrase: string;
  speechStyle: string;
  habits: string;
  values: string;
  relations: CharacterRelation[];
}

export interface CharacterBasicInfo {
  age: number;
  gender: string;
  appearance: string;
  identity: string;
  occupation: string;
}

export interface CharacterPersonality {
  /** 纯文字的性格描述；不再有 MBTI 与维度打分。 */
  description: string;
}

/** 「AI 分析正文与大纲」的结果：逐字段建议。只产出建议，是否采用由作者逐项决定。 */
export interface CharacterProfileAnalysis {
  /** 字段 key → 建议值（文本字段是字符串，age 是数字）。 */
  suggestions: Record<string, string | number>;
  /** 被拒掉的字段及原因（内容不合法 / 越界）。 */
  rejected: Record<string, string>;
  /** 模型没给建议的字段（正文证据不足）。 */
  omitted: string[];
  /** 属于推断而非文中有明写的字段（目前是 age / gender）。 */
  inferred: string[];
  degraded: boolean;
  reason?: string | null;
  basis?: {
    chapterCount: number;
    sampledChars: number;
    hasOutline: boolean;
  } | null;
}

export interface CharacterRelation {
  targetCharacterId: string;
  type: 'family' | 'friend' | 'love' | 'enemy' | 'interest' | 'secret' | 'rival' | 'master_student';
  description: string;
  sinceChapter: number;
}

// 一键分析作品人物：AI 从正文中识别出的人物（尚未入库，等待用户确认）
export interface CharacterAnalysisItem {
  name: string;
  role: Character['role'];
  summary: string;
  // 后端从文本自动推断的画像（可选，兼容旧数据）
  gender?: string;
  appearance?: string;
  personality?: CharacterPersonality;
}

// 一键分析作品人物：AI 识别出的人物关系（source/target 为人物名字，导入时映射为 id）
export interface CharacterAnalysisRelation {
  source: string;
  target: string;
  type: CharacterRelation['type'];
  description: string;
  sinceChapter: number;
}

// 一键分析作品人物的完整结果
export interface CharacterAnalysisResult {
  characters: CharacterAnalysisItem[];
  relations: CharacterAnalysisRelation[];
  summary: string;
}

export interface Location {
  id: string;
  projectId: string;
  name: string;
  type: string;
  description: string;
  firstAppearance: number;
}

export interface WorldRule {
  id: string;
  projectId: string;
  category: 'magic' | 'technology' | 'society' | 'culture' | 'geography' | 'history';
  name: string;
  description: string;
}

export interface InspirationFragment {
  id: string;
  projectId: string;
  content: string;
  type: 'idea' | 'scene' | 'dialogue' | 'character' | 'plot';
  tags: string[];
  createdAt: string;
  isConverted: boolean;
  source?: 'manual' | 'brainstorm';
  title?: string;
}

export type LLMProvider = 'openai' | 'claude' | 'deepseek' | 'qwen' | 'wenxin' | 'kimi' | 'glm' | 'local' | 'arts';

export interface LLMConfig {
  id: string;
  provider: LLMProvider;
  apiKey: string;
  apiBase: string;
  model: string;
  temperature: number;
  maxTokens: number;
  isActive?: boolean;
  /** 建议4 模型档位路由：角色级模型覆盖（如 {"writing": {"model": "..."}}） */
  roleOverrides?: RoleOverrides;
}

/** 角色级模型覆盖（建议4 模型档位路由；建议1 写作档位写入 writing 角色） */
export type RoleOverrides = Record<string, { provider?: string; model?: string; temperature?: number }>;

export interface LLMHealth {
  configured: boolean;
  provider: string;
  model: string;
  ok: boolean;
  error: string;
  checked?: boolean;
}

/** 建议1 去 AI 味后处理结果 */
export interface DeaiResult {
  result: string;
  changes: Array<{ type: string; severity: string; note: string }>;
  degraded: boolean;
  scoreBefore: number;
  scoreAfter: number;
  ruleCount?: number;
  issueCount?: number;
  /** 窗口总数（长章节会分窗口重写） */
  segments?: number;
  /** 因截断/过度缩写而保留原文的窗口数：>0 时未做全部改写 */
  keptWindows?: number;
}

export type WritingTier = 'value' | 'high';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  status?: 'completed' | 'pending' | 'failed' | 'interrupted';
  metadata?: ChatResult & { kind?: 'chat' | 'copilot'; operation?: { chapterId: string; action: string; currentText?: string; selectionText?: string; instruction?: string } | null };
}

export interface ChatResult {
  reply?: string;
  mode?: 'llm' | 'fallback' | 'failed';
  intent?: string;
  error_code?: string | null;
  outline_failure_code?: string;
  trace_id?: string;
  request_id?: string;
  action?: string;
  candidate?: OutlineCandidate | CopilotCandidate | null;
}

export interface ChatConversation {
  id: string;
  project_id: string;
  title: string;
  archived: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
  messages?: ChatMessage[];
}

export interface CopilotCandidate {
  id: string;
  project_id: string;
  chapter_id: string;
  rewrite_type: string;
  original: string;
  rewritten: string;
  candidate: string;
  reason: string;
  explanation?: string;
  applied_content?: string;
  status: 'proposed' | 'applied' | 'rejected' | 'reverted';
  diff: Array<{ type: string; original: string; candidate: string }>;
  /**
   * 生成这份候选时所用的风格计划版本。
   * 历史候选没有持久化元数据时后端返回 null——此时**不要编造**，
   * 启用 v2 计划后接受会被 409 拒绝，需要显式重新生成。
   */
  stylePlanId?: string | null;
  stylePlanRevision?: number | null;
  /** Accept 成功时返回的过渡进度，用于立即刷新导航与进度。 */
  styleTransition?: StyleTransition;
}

export interface NarrativeContextCharacter {
  name: string;
  role?: Character['role'];
  age?: number;
  gender?: string;
  appearance?: string;
  identity?: string;
  personality?: string;
  background?: string;
  coreTrauma?: string;
  surfaceMotivation?: string;
  deepDesire?: string;
  arc?: string;
  catchphrase?: string;
  speechStyle?: string;
  habits?: string;
  values?: string;
}

export interface NarrativeContextWorldRule {
  category?: WorldRule['category'];
  name: string;
  description: string;
}

export interface ContinueRequest {
  projectId: string;
  chapterId: string;
  cursorPosition: number;
  contextBefore: string;
  direction?: string;
  wordCount: number;
  chapterTitle?: string;
  premise?: string;
  outlineSummary?: string;
  chapterSummary?: string;
  styleTone?: string;
  characters?: NarrativeContextCharacter[];
  worldRules?: NarrativeContextWorldRule[];
  /** 发起时从 style-contract 取的最新计划版本；陈旧会被 409 拒绘，不会启动 SSE。 */
  stylePlanId?: string;
  stylePlanRevision?: number;
}

export type ChatContextProfile =
  | 'plot_planning'
  | 'scene_writing'
  | 'continuation'
  | 'consistency_check';

export type CopilotAction =
  | 'continue'
  | 'rewrite'
  | 'expand'
  | 'condense'
  | 'style'
  | 'character'
  | 'emotion'
  | 'pacing'
  | 'branch'
  | 'analyze_scene'
  | 'analyze_next'
  | 'continuity'
  | 'foreshadow';

export interface ChatContextOptions {
  conversationId?: string;
  requestId?: string;
  regenerateMessageId?: string;
  chapterId?: string;
  currentText?: string;
  selectionText?: string;
  contextProfile?: ChatContextProfile;
  action?: CopilotAction;
}

export interface StoryState {
  projectId: string;
  version: number;
  currentVolumeGoal: string;
  currentPhaseGoal: string;
  protagonistGoal: string;
  protagonistObstacle: string;
  mainConflict: string;
  relationshipChanges: string[];
  currentMysteries: string[];
  activeForeshadows: string[];
  unresolvedQuestions: string[];
  nextStageDirections: string[];
}

export interface ReviewRequest {
  projectId: string;
  text: string;
  chapterId?: string;
  characters?: NarrativeContextCharacter[];
  worldRules?: NarrativeContextWorldRule[];
}

export interface GenerateOutlineRequest {
  projectId: string;
  premise: string;
  genre: Genre;
  targetWordCount: number;
  targetChapters: number;
  structure: Outline['structure'];
  additionalNotes?: string;
}

export interface GenerateCharacterRequest {
  projectId: string;
  role: Character['role'];
  hints: string;
  genre: Genre;
}

export interface ApiResponse<T> {
  success: boolean;
  data: T;
  error?: string;
}

export interface StreamingResponse {
  chunk: string;
  done: boolean;
}

// ── 长篇创作任务（后端 novel-jobs）───────────────────────

export type NovelJobMode = 'all' | 'current-to-end' | 'first-unfinished' | 'single';

export type NovelJobStatus = 'queued' | 'running' | 'interrupted' | 'completed' | 'failed' | 'cancelled';

export interface NovelJobPayload {
  id: string;
  project_id: string;
  mode: NovelJobMode;
  requested_chapter_id: string | null;
  status: NovelJobStatus;
  message: string;
  target_words: number;
  total_chapters: number;
  completed_chapters: number;
  failed_chapters: number;
  fallback_chapters: number;
  generated_words: number;
  current_chapter_id: string | null;
  current_chapter_title: string | null;
  last_completed_chapter_id?: string | null;
  last_completed_chapter_title?: string | null;
  llm_provider: string;
  llm_model: string;
  last_generation_source: string;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  error: string;
  cancel_requested?: boolean;
  retry_count: number;
  max_retries: number;
  created_at?: string;
  // M2 断点续传：checkpoint 信息 + 可恢复状态
  checkpoint_type?: string;
  resume_target_json?: string;
  pending_manual_recovery?: boolean;
  heartbeat_at?: string;
  recoverable?: boolean;
  // M3 自检闭环统计（0 表示已过自检，供任务中心指标卡展示）
  fact_check_count?: number;
  rewrite_count?: number;
  hook_miss_count?: number;
  quality_debt_count?: number;
  preview_chapter_id?: string | null;
  preview_content?: string;
}

export interface StartNovelJobRequest {
  projectId: string;
  mode: NovelJobMode;
  currentChapterId?: string;
  maxRetries?: number;
  /** 同上：后台每章会自行重新编译当前契约，但启动时仍需带 token。 */
  stylePlanId?: string;
  stylePlanRevision?: number;
}

// ── M4 文风画像引擎 ─────────────────────────

export type StyleProfileStatus = 'completed' | 'degraded';

export interface StyleRule {
  rule: string;
  key?: string;
  source?: string;
}

export interface StyleMetrics {
  totalChars: number;
  totalSentences: number;
  totalParagraphs: number;
  wordCount: number;
  avgSentenceLength: number;
  dialogueRatio: number;
  descriptionDensity: number;
  paragraphShortRatio: number;
  paragraphMediumRatio: number;
  paragraphLongRatio: number;
  sentenceShortRatio: number;
  sentenceLongRatio: number;
}

export interface StyleDimensions {
  avgSentenceLength?: number;
  dialogueRatio?: number;
  descriptionDensity?: number;
  paragraphLength?: 'short' | 'medium' | 'long';
  tone?: string;
}

export interface StyleProfile {
  id: string;
  projectId: string;
  name: string;
  sourceType: string;
  status: StyleProfileStatus;
  narrativeRules: StyleRule[];
  characterRules: StyleRule[];
  languageRules: StyleRule[];
  rhythmRules: StyleRule[];
  antiAiRules: StyleRule[];
  metrics: StyleMetrics;
  dimensions: StyleDimensions;
  strengths: string[];
  weaknesses: string[];
  transitionPlan: Record<string, unknown>;
  report: string;
  sampleCount: number;
  sampleChars: number;
  llmUsed: boolean;
  styleKind?: 'reference' | 'genre';
  version?: number;
  parentId?: string;
  identity?: Record<string, unknown>;
  mechanics?: Record<string, unknown>;
  boundaries?: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export type TransitionPace = 'fast' | 'medium' | 'slow' | 'custom';

/** 一次生成请求所用的计划版本；服务端据此判断候选/草稿是否过期。 */
export interface StylePlanToken {
  stylePlanId: string;
  stylePlanRevision: number;
}

/** 计划创建时冻结的来源画像快照（作者原稿的文风）。 */
export interface StyleSourceSnapshot {
  profileId: string;
  profileVersion: number;
  name: string;
  status: string;
  dimensions: Record<string, unknown>;
  rules: Record<string, unknown>;
  identity?: Record<string, unknown>;
  mechanics?: Record<string, unknown>;
  boundaries?: Record<string, unknown>;
}

/** 计划创建时冻结的目标快照：单一预设 + 作者的文字补充。 */
export interface StyleTargetSnapshot {
  presetKey: string;
  preset: Record<string, unknown>;
  customPrompt: string;
}

/**
 * v1 旧计划的账本是 step/steps 结构；v2 账本含基线/排除/已完成章节，
 * **前端不负责推算或修改账本**，因此这里保留未知字段但不解释它们。
 */
export interface StylePlanLedger {
  step?: number;
  weight?: number;
  steps?: Array<{ step: number; weight: number }>;
  start_chapter_count?: number;
  [key: string]: unknown;
}

export interface StyleTransition {
  id: string;
  projectId: string;
  profileId: string;
  pace: TransitionPace;
  /** 已完成比例，等于 completedCount / targetChapters。UI 进度条用这个值。 */
  progress: number;
  intervalChapters: number;
  totalSteps: number;
  /** 计划账本。v2 有值但不在这里推算；v1 是旧的 step/steps 结构。 */
  plan: StylePlanLedger;
  startedAt: string;
  updatedAt: string;
  /** 2 = 渐进过渡计划；1 = 旧版记录。缺失时按 1 处理，不猜 v2。 */
  schemaVersion: 1 | 2;
  /** 旧计划只读：已有进度不折算成完成数，需重新设置才启用 v2 记录。 */
  legacyReadOnly: boolean;
  /** 以下仅 v2 存在。缺失时保持缺失——**不得虚构 revision 或完成数**。 */
  planId?: string;
  revision?: number;
  targetChapters?: number;
  completedCount?: number;
  sourceSnapshot?: StyleSourceSnapshot;
  targetSnapshot?: StyleTargetSnapshot;
}

/** 续写流式回调的结果。error 仅在失败时存在；冲突时 code 为 style_plan_conflict。 */
export interface ContinueResult {
  generationIncomplete?: boolean;
  error?: { code?: string; message: string; status?: number };
}

/** 新建/重设计划时提交的目标文风：单一预设 + 可选文字补充。 */
export interface StyleTransitionTarget {
  presetKey: string;
  customPrompt?: string;
}

// ── P2 伏笔账本（长程跨卷张力） ────────────────────────

export type ForeshadowStatus = 'planted' | 'progressing' | 'resolved' | 'abandoned';

export interface ForeshadowEvent {
  chapterId?: string;
  chapterOrder?: number;
  status: string;
  note?: string;
  resolution?: string;
  at?: string;
}

export interface Foreshadow {
  id: string;
  projectId: string;
  name: string;
  description: string;
  plantChapterId: string;
  targetChapterId: string;
  status: ForeshadowStatus;
  resolution: string;
  events: ForeshadowEvent[];
  plantChapter: number;
  targetChapter: number;
  totalChapters: number;
  overdue: boolean;
  overdueBy: number;
  createdAt: string;
  updatedAt: string;
}

/** suggest 端点返回的建议候选（snake_case 字段，未走 camelCase 转换）。 */
export interface ForeshadowSuggestion {
  name: string;
  description: string;
  plant_chapter: number;
  target_chapter: number;
  plant_chapter_id: string;
  target_chapter_id: string;
}

export interface ForeshadowConsistencyIssue {
  type: string;
  foreshadowId: string;
  name: string;
  detail: string;
}

export interface ForeshadowConsistency {
  passed: boolean;
  total: number;
  byType: Record<string, number>;
  issues: ForeshadowConsistencyIssue[];
}

export interface ForeshadowListData {
  items: Foreshadow[];
  consistency: ForeshadowConsistency;
  alertCount: number;
}

/** POST /foreshadows/draft：作者手写文字 → 伏笔草稿（不落库）。
 *  degraded=true 表示 AI 不可用/失败，此时 items 为空且 reason 是给作者看的中文原因。 */
export interface ForeshadowDraftResult {
  items: ForeshadowSuggestion[];
  degraded: boolean;
  reason: string;
}

// ── P2-3 参考书档案（作者收藏的「别人的作品」的写法档案） ──────────

/** 一条技法规则（来自确定性统计或 AI 拆解）。 */
export interface ReferenceRule {
  rule: string;
  evidence?: string;
}

/** 列表项：刻意**不含原文**（原文可能几十万字）。 */
export interface ReferenceWorkSummary {
  id: string;
  projectId: string;
  /** 来源项目名（档案是**用户级个人资产**，可能是在别的书里收集的） */
  sourceProjectName: string;
  name: string;
  sourceNote: string;
  charCount: number;
  storedChars: number;
  truncated: boolean;
  sampleChars: number;
  sampleCount: number;
  status: 'completed' | 'degraded';
  statusReason: string;
  llmUsed: boolean;
  /** 作者自打的标签（用于素材库搜索/筛选；后端已去重、限长限量） */
  tags: string[];
  createdAt: string;
  updatedAt: string;
}

/** 单条详情：含原文与分析结果。 */
export interface ReferenceWork extends ReferenceWorkSummary {
  rawText: string;
  report: string;
  narrativeRules: ReferenceRule[];
  characterRules: ReferenceRule[];
  languageRules: ReferenceRule[];
  rhythmRules: ReferenceRule[];
  antiAiRules: ReferenceRule[];
  strengths: string[];
  weaknesses: string[];
  /** 本地确定性统计（不是模型编的）：平均句长 / 对话占比 / 描写密度… */
  metrics: Record<string, number>;
  dimensions: Record<string, unknown>;
}

/** 一份被显式启用的参考：weight 是**优先级**（预算不够时先保留高权重的）。 */
export interface ReferenceActiveItem {
  id: string;
  /** 1-10；界面上是 低=3 / 中=6 / 高=9 */
  weight: number;
  name: string;
}

/** 列表响应：附带当前被显式启用为「创作参考」的那些（按强度降序）。 */
export interface ReferenceWorkListData {
  items: ReferenceWorkSummary[];
  activeReferences: ReferenceActiveItem[];
  /** 兼容旧字段：强度最高的那份（没有启用则为空串） */
  activeReferenceId: string;
}

// ── P2-2b 写作冲突（任务中心冲突视图，后端 projects.py 已拆分字段） ──────────

/** 写作冲突记录（后端 `_conflict_to_api` 已拆分 summary / rootCause）。 */
export interface WritingConflict {
  id: string;
  projectId: string;
  /** 完整落盘文本 `"{summary}（根因：{root_cause}）"` */
  content: string;
  /** 冲突内容摘要（已从 content 拆分） */
  summary: string;
  /** 根因（已从 content 拆分，可能为空） */
  rootCause: string;
  chapterId: string;
  createdAt: string;
}

// ── 建议2：全文问答（DeepSeek 前缀缓存低成本直灌核对） ──────────

/** 全书直灌统计：字数 / token 估算 / 成本估算 */
export interface FulltextStats {
  /** 直灌正文有效字数（含章节标题行） */
  chars: number;
  /** 正文 token 估算（1 汉字 ≈ 1 token，保守偏上） */
  estimatedTokens: number;
  /** 参与直灌的章节数 */
  chapters: number;
  /** 是否因超限截断（>60 万字保护） */
  truncated: boolean;
  /** 估算费用（USD）；无已核实价格来源时为 null（不再假设缓存命中价） */
  estimatedCostUsd: number | null;
  /** 结构化估算：输入/输出 token 分开、价格来源、计价基准与币种 */
  estimate?: FulltextEstimate;
}

/** 全文问答的费用估算（与"实际账单"严格区分） */
export interface FulltextEstimate {
  inputTokens: number;
  outputTokensMax: number;
  /** 估算阶段缓存命中未知，恒为 null */
  cacheHitTokens: number | null;
  provider: string;
  model: string;
  /** 币种；未知价格为 null */
  currency: string | null;
  /** 价格来源（官方页/运营方报价），未知为 null */
  priceSource: string | null;
  /** 计价基准，如 cache_miss_input_upper_bound（按未命中价取上界） */
  basis: string | null;
  costUsd: number | null;
  /** estimated | unknown */
  status: string;
}

/** 实际用量（只报告 provider 真实回传的数据，缺失即 null） */
export interface FulltextActual {
  provider: string;
  model: string;
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  cacheHitTokens: number | null;
  currency: string | null;
  /** 无 provider 账单时恒为 null，不做换算冒充账单 */
  actualCostUsd: number | null;
  /** reported | unavailable */
  status: string;
  note?: string;
}

/** 全文问答结果（含本次消耗统计） */
export interface FulltextAskResult extends FulltextStats {
  /** LLM 回答文本（失败时为降级提示） */
  answer: string;
  /** LLM 失败降级标志（true 表示回答为降级提示；失败是否计费以服务商记录为准） */
  degraded: boolean;
  /** 本次解析出的实际 provider / model */
  provider?: string;
  model?: string;
  /** 实际用量（provider 未回传 usage 时为 null） */
  actual?: FulltextActual;
}


// ── P1：记忆投影状态（正文已保存但记忆未同步的可见状态 + 单项重试）──

export interface MemoryProjection {
  id: string;
  project_id: string;
  chapter_id: string;
  projection_type: string; // chapter_summary / facts / entities / volume / fts
  status: string;          // done / failed / skipped / pending
  detail: string;
  created_at: string;
  updated_at: string;
}

export interface ReplayProjectionResult {
  status: string;
  projection_type?: string;
  chapter_id?: string;
  detail?: string;
}

// ── 账号注册 · 邀请码（PRD：注册/邀请码绑定）──────────

/** 登录/注册成功后的账号信息（后端 `/auth/*` 统一返回这个形状）。 */
export interface AuthAccount {
  id: string;
  username: string;
  isAdmin: boolean;
}

/**
 * `/auth/permissions`：当前凭据能做什么。
 *
 * 存在的理由：`/auth/me` 只认会话 cookie，而老 `.env` 邀请码走请求头通道（无会话），
 * 前端就无从得知管理入口该不该显示。该接口与后端 `require_maintainer` 用**同一套**
 * 解析与判定，保证「显示什么」与「放行什么」不会分裂。
 */
export interface AuthPermission {
  userId: string;
  isAdmin: boolean;
}

/** 用户名是否可用（注册表单失焦时查询；不合规也算不可用，并带原因）。 */
export interface UsernameAvailability {
  available: boolean;
  reason: string;
}

/**
 * 当前账号已绑定的邀请码。
 *
 * **只有 hint（后 4 位），没有明文**：库内只存 sha256(码)，这是 Q1=C（码即凭据）
 * 的安全要求。用户拿着自己收到的那串码，靠 hint 确认“是哪一把备用钥匙”。
 */
export interface BoundInvite {
  codeHint: string;
  label: string;
  boundAt: string;
}
