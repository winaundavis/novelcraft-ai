import { create } from 'zustand';

/**
 * 导入页的流程状态。
 *
 * 背景：导入面板此前把状态放在组件里，而 `ProjectWorkspace` 是按 `activeTab === 'import'`
 * 条件渲染的 —— 切到别的页签就卸载，回来时十几个状态（已选文件、扫描结果、舍弃标记、
 * 成功提示、当前步骤）全部归零，作者得从头再选一次文件、再等一次几十秒的扫描。
 *
 * 这里把状态搬到 React 之外并按 projectId 分开存，于是：
 *   - 切页签再回来，界面原样（组件卸载不影响）；
 *   - 不同作品各存一份，互不串台；
 *   - **刷新页面即清空**（纯内存态，不落 localStorage/sessionStorage）。刻意如此：
 *     已选文件是浏览器的 `File` 对象，本来就无法序列化，保留一半只会让作者看到
 *     「文件还在」却一导入就失败的假象；
 *   - 只有作者点「清空」（或重新扫描覆盖）才会回到初始状态。
 */

export type AnalysisTabKey = 'characters' | 'locations' | 'timeline' | 'foreshadowing';

/**
 * 写入已有项目时对既有章节的处理方式。
 *
 * 默认保持历史行为（`replace`）—— 改默认值属于产品决策，不顺手做；
 * 但选到 `replace` 时界面会明确写出后果。
 */
export type ImportMergeMode = 'replace' | 'merge';

export const IMPORT_MERGE_MODES: ReadonlyArray<{
  id: ImportMergeMode;
  name: string;
  description: string;
}> = [
  {
    id: 'replace',
    name: '完全替换',
    description: '当前项目的全部章节移入回收站，再写入本次导入的内容；导入文件里没有的章节不会留在项目里。',
  },
  {
    id: 'merge',
    name: '增量合并',
    description:
      '按文件名里的章节号对齐（第3章.md → 第 3 章）：号相同的章节用新内容替换，导入里没有的号原样保留。与章节标题、选择顺序都无关。',
  },
];

export const ANALYSIS_TAB_KEYS: readonly AnalysisTabKey[] = ['characters', 'locations', 'timeline', 'foreshadowing'];

/**
 * 四类分析结果的空舍弃表（重新扫描时回到这个初始值）。
 *
 * 注意：这是模块级共享常量，**只读不写** —— 界面上的舍弃/恢复一律经
 * `changeMarkdownFlow` 生成新对象，任何就地 push 都会污染所有作品。
 */
export const EMPTY_DISCARDED: Record<AnalysisTabKey, string[]> = {
  characters: [],
  locations: [],
  timeline: [],
  foreshadowing: [],
};

export interface MarkdownChapterPreview {
  id: string;
  fileName: string;
  title: string;
  content?: string;
  wordCount: number;
  order: number;
}

/** 被认定为「旧编号残留」而一并回收的章节（上一次按顺序编号留下的重复章节）。 */
export interface ImportCleanedChapter {
  order: number;
  title: string;
}

export interface MarkdownImportResult {
  success: boolean;
  folderPath?: string;
  totalFiles?: number;
  totalWords?: number;
  chapters?: MarkdownChapterPreview[];
  /** 写进已有项目时实际用的方式，以及各类章节的真实数量（后端回传）。 */
  mergeMode?: ImportMergeMode;
  /** 合并按什么对齐：number（文件名里的章节号）/ index（文件里没有号，退回导入顺序）。 */
  mergeKey?: 'number' | 'index';
  addedChapters?: number;
  updatedChapters?: number;
  keptChapters?: number;
  recycledChapters?: number;
  /** 顺带回收的「旧编号残留」数量与明细（上一次按顺序编号留下的重复章节）。 */
  cleanedChapters?: number;
  cleanedDetails?: ImportCleanedChapter[];
  /** 本次扫描识别到的人物候选数。
   *  注意：导入**不写**项目人物，候选项留在项目里，由「人物」页勾选后才入档。 */
  charactersDetected?: number;
  analysis?: {
    characters: Array<{ name: string; frequency: number }>;
    locations: Array<{ name: string; frequency: number }>;
    timeline: Array<{ text: string; type: string; chapter: number }>;
    foreshadowing: Array<{ text: string; category: string; context: string }>;
    summary: string;
  };
  error?: string;
}

export interface MarkdownFlowState {
  step: number;
  /** 上传 / 服务器文件夹。默认上传：路径式接口在 DEBUG=false 下一律 403。 */
  mode: 'upload' | 'folder';
  folderPath: string;
  pickedFiles: File[];
  skippedCount: number;
  isLoading: boolean;
  isIngesting: boolean;
  error: string;
  successMessage: string;
  result: MarkdownImportResult | null;
  activeAnalysisTab: AnalysisTabKey;
  /** 写入已有项目时怎么处理既有章节（完全替换 / 增量合并）。 */
  mergeMode: ImportMergeMode;
  /** 上一次合并回收的旧编号残留明细（清空/重新扫描时回到空）。 */
  mergeCleaned: ImportCleanedChapter[];
  /** 作者叉掉的误识别条目，按分类分开存（人物/地点用 name，时间线/伏笔用 text）。 */
  discarded: Record<AnalysisTabKey, string[]>;
}

export const EMPTY_MARKDOWN_FLOW: MarkdownFlowState = {
  step: 1,
  mode: 'upload',
  folderPath: '',
  pickedFiles: [],
  skippedCount: 0,
  isLoading: false,
  isIngesting: false,
  error: '',
  successMessage: '',
  result: null,
  activeAnalysisTab: 'characters',
  mergeMode: 'replace',
  mergeCleaned: [],
  discarded: EMPTY_DISCARDED,
};

export interface Suggestion {
  id: string;
  order: number;
  status: 'pending' | 'applying' | 'applied' | 'skipped';
  action: string;
  target: string;
  description: string;
  priority: 'high' | 'medium' | 'low';
  dependencies: string[];
  result?: string;
}

export interface WordImportResult {
  success: boolean;
  fileName?: string;
  suggestions?: Suggestion[];
  totalSuggestions?: number;
  error?: string;
}

export interface WordFlowState {
  step: number;
  filePath: string;
  selectedFile: File | null;
  isLoading: boolean;
  error: string;
  result: WordImportResult | null;
  suggestions: Suggestion[];
}

export const EMPTY_WORD_FLOW: WordFlowState = {
  step: 1,
  filePath: '',
  selectedFile: null,
  isLoading: false,
  error: '',
  result: null,
  suggestions: [],
};

interface ImportFlows {
  hubMode: Record<string, 'markdown' | 'word'>;
  markdown: Record<string, MarkdownFlowState>;
  word: Record<string, WordFlowState>;
}

const useStore = create<ImportFlows>(() => ({ hubMode: {}, markdown: {}, word: {} }));

// ── 子页签（Markdown 章节 / Word 建议书） ──────────────────

export const useImportHubMode = (projectId: string) => useStore((s) => s.hubMode[projectId] ?? 'markdown');

export const setImportHubMode = (projectId: string, mode: 'markdown' | 'word') =>
  useStore.setState((s) => ({ hubMode: { ...s.hubMode, [projectId]: mode } }));

// ── Markdown 章节导入 ─────────────────────────────────────

export const useMarkdownFlow = (projectId: string) => useStore((s) => s.markdown[projectId] ?? EMPTY_MARKDOWN_FLOW);

export const markdownFlowSnapshot = (projectId: string) =>
  useStore.getState().markdown[projectId] ?? EMPTY_MARKDOWN_FLOW;

export function changeMarkdownFlow(
  projectId: string,
  patch: Partial<MarkdownFlowState> | ((previous: MarkdownFlowState) => Partial<MarkdownFlowState>),
) {
  useStore.setState((s) => {
    const previous = s.markdown[projectId] ?? EMPTY_MARKDOWN_FLOW;
    const next = typeof patch === 'function' ? patch(previous) : patch;
    return { markdown: { ...s.markdown, [projectId]: { ...previous, ...next } } };
  });
}

// 扫描/入库是长异步操作（上百章要几十秒）。清空或重新扫描之后，早先那次请求返回时
// 不能再往界面上写 —— 否则刚清空的面板会被旧结果「复活」。用递增令牌识别过期响应。
const runTokens = new Map<string, number>();

/** 开一次新的扫描/入库，并让此前所有在途请求失效。 */
export function beginMarkdownRun(projectId: string) {
  const token = (runTokens.get(projectId) ?? 0) + 1;
  runTokens.set(projectId, token);
  return token;
}

/** 这次请求返回时还是最新的吗？（清空过就不是了） */
export const isCurrentMarkdownRun = (projectId: string, token: number) =>
  (runTokens.get(projectId) ?? 0) === token;

/** 回到初始状态：作废在途请求，并清掉全部流程数据。 */
export function resetMarkdownFlow(projectId: string) {
  beginMarkdownRun(projectId);
  changeMarkdownFlow(projectId, EMPTY_MARKDOWN_FLOW);
}

// ── Word 建议书导入 ───────────────────────────────────────

export const useWordFlow = (projectId: string) => useStore((s) => s.word[projectId] ?? EMPTY_WORD_FLOW);

export const wordFlowSnapshot = (projectId: string) => useStore.getState().word[projectId] ?? EMPTY_WORD_FLOW;

export function changeWordFlow(
  projectId: string,
  patch: Partial<WordFlowState> | ((previous: WordFlowState) => Partial<WordFlowState>),
) {
  useStore.setState((s) => {
    const previous = s.word[projectId] ?? EMPTY_WORD_FLOW;
    const next = typeof patch === 'function' ? patch(previous) : patch;
    return { word: { ...s.word, [projectId]: { ...previous, ...next } } };
  });
}

const wordRunTokens = new Map<string, number>();

export function beginWordRun(projectId: string) {
  const token = (wordRunTokens.get(projectId) ?? 0) + 1;
  wordRunTokens.set(projectId, token);
  return token;
}

export const isCurrentWordRun = (projectId: string, token: number) =>
  (wordRunTokens.get(projectId) ?? 0) === token;

export function resetWordFlow(projectId: string) {
  beginWordRun(projectId);
  changeWordFlow(projectId, EMPTY_WORD_FLOW);
}

/** 清空按钮要不要显示：没进行过任何操作时就别摆一个点了没事发生的按钮。 */
export function hasMarkdownProgress(flow: MarkdownFlowState) {
  return (
    flow.step !== 1 ||
    flow.pickedFiles.length > 0 ||
    flow.folderPath.trim() !== '' ||
    flow.skippedCount > 0 ||
    flow.error !== '' ||
    flow.successMessage !== '' ||
    Boolean(flow.result)
  );
}

export function hasWordProgress(flow: WordFlowState) {
  return (
    flow.step !== 1 ||
    flow.filePath.trim() !== '' ||
    Boolean(flow.selectedFile) ||
    flow.error !== '' ||
    Boolean(flow.result) ||
    flow.suggestions.length > 0
  );
}
