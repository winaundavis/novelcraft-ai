/**
 * 统一的本地身份清理入口。
 *
 * 退出登录、切换账号、会话失效时必须调用 `clearLocalIdentity()`：一次性清掉
 * 认证标记、邀请码标记、账户相关的持久化缓存与内存视图，避免上一个账号的
 * 资料在下一个账号的 UI 中短暂闪现。
 *
 * 只清理本产品自己的身份数据，**不调用** `localStorage.clear()` 等清空整个
 * 浏览器存储的操作（主题等非身份偏好会保留）。
 */
import { useChatStore, useEditorStore, useProjectStore, useReviewStore, useSettingsStore } from '../stores';
import { clearInviteToken } from './betaAccess';

/** localStorage 中的"已认证"标记键（服务端会话才是真正的授权来源）。 */
export const AUTH_MARKER_KEY = 'novelcraft-authenticated';

export function hasAuthMarker(): boolean {
  try {
    return window.localStorage.getItem(AUTH_MARKER_KEY) === '1';
  } catch {
    return false;
  }
}

export function markAuthenticated(): void {
  try {
    window.localStorage.setItem(AUTH_MARKER_KEY, '1');
  } catch {
    // localStorage 不可用时静默忽略；服务端 cookie 仍是授权来源
  }
}

function emptyAutoWriteSession() {
  return {
    mode: 'current-to-end' as const,
    status: 'idle' as const,
    currentChapterId: null,
    completedChapterIds: [] as string[],
    failedChapterIds: [] as string[],
    totalChapters: 0,
    completedChapters: 0,
    lastSummary: '',
    progressMessage: '',
    startedAt: null,
    updatedAt: null,
    errorMessage: '',
  };
}

/**
 * 清除所有本产品身份数据：认证标记 + 邀请码 + 账户相关缓存 + 内存视图。
 * 幂等，可在任意时机重复调用。
 */
export function clearLocalIdentity(): void {
  // 1) 认证 / 邀请码标记
  try {
    window.localStorage.removeItem(AUTH_MARKER_KEY);
  } catch {
    // ignore
  }
  clearInviteToken();

  // 2) 持久化缓存（按 store 逐项清理，不动浏览器全局存储）
  useProjectStore.persist.clearStorage();
  useEditorStore.persist.clearStorage();
  useChatStore.persist.clearStorage();
  useSettingsStore.persist.clearStorage();

  // 3) 内存视图复位（防止旧账号作品残留/闪现）
  useProjectStore.setState({ projects: [], currentProjectId: null, isLoading: false });
  useEditorStore.setState({
    activeProjectId: '',
    editorDataMap: {},
    chapterContent: '',
    currentChapterId: null,
    outlineData: null,
    isDirty: false,
    wordCount: 0,
    selectionText: '',
    draftStylePlan: null,
    contentRevision: useEditorStore.getState().contentRevision + 1,
    autoWriteSession: emptyAutoWriteSession(),
  });
  useChatStore.setState({ messages: [], isStreaming: false, streamingContent: '' });
  useSettingsStore.setState({ llmConfigs: [], activeLLMConfigId: null });
  // 审校结果同样属于账户内容，切换账号时必须清掉，避免上一个账号的正文分析残留。
  useReviewStore.getState().clearAll();
}
