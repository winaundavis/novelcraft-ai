import { Routes, Route, Navigate } from 'react-router-dom';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { AlertTriangle, ShieldAlert, X } from 'lucide-react';
import { usePermissionStore, useSettingsStore } from './stores';
import { AI_DEGRADED_EVENT, authApi, isConnectionError, isLegacyAccountMissing, isRateLimitedError, isUnauthorizedError } from './services/api';
import { ToastProvider } from './components/common';
import { Button, Input, Modal } from './components/common';
import TermsDialog from './components/auth/TermsDialog';
import { getInviteToken, setInviteToken } from './services/betaAccess';
import {
  AUTH_MARKER_KEY,
  clearLocalIdentity,
  hasAuthMarker,
  markAuthenticated,
} from './services/session';
import AppLayout from './components/layout/AppLayout';
import Dashboard from './pages/Dashboard';
import ProjectWorkspace from './pages/ProjectWorkspace';
import SettingsPage from './pages/SettingsPage';
import AdminConsole from './components/admin/AdminConsole';
import AccountPage from './pages/AccountPage';
import type { UsernameAvailability } from './types';

const DEGRADED_MESSAGES: Record<string, string> = {
  'continue-writing': 'AI 服务暂时不可用，本次续写内容由本地模板生成，质量可能下降。',
  chat: 'AI 服务暂时不可用，本次回复由本地模板生成，质量可能下降。',
  outline: 'AI 服务暂时不可用，本次大纲由本地模板生成，质量可能下降。',
  character: 'AI 服务暂时不可用，本次角色由本地模板生成，质量可能下降。',
  review: 'AI 服务暂时不可用，本次审校由本地模板生成，质量可能下降。',
};

/**
 * 有明确诊断原因的降级：**不能**再说"服务暂时不可用"。
 *
 * 2026-10-19 事故（审计 babfeffb67b1）：模型把输出预算全用在推理上、正文 0 字，
 * 前端却显示"AI 服务暂时不可用"，把排查引向网络和密钥 —— 服务其实好得很。
 */
const DEGRADED_ERROR_MESSAGES: Record<string, string> = {
  output_budget_exhausted:
    '模型把输出预算用在了推理上，没有产出正文，本次为本地模板生成。这不是网络或密钥问题：请重试；若反复出现，需要调整该模型的推理设置。',
  budget_exhausted: '今日 AI 使用额度已用完，模型未被调用，本次为本地模板生成。',
};

const DEFAULT_DEGRADED_MESSAGE = 'AI 服务暂时不可用，本次结果为本地生成的降级内容，质量可能下降。';
const DEGRADED_TOAST_DURATION_MS = 6000;
// 安全默认：只有显式设置为 "false" 才关闭邀请码校验。
// 生产/本地 Beta 若遗漏环境变量，也不会出现“页面可用但 API 全部 401”的假登录状态。
const BETA_ACCESS_REQUIRED = import.meta.env.VITE_BETA_AUTH_REQUIRED !== 'false';

/** 认证状态机：checking → authenticated | unauthenticated | connection_error | rate_limited。 */
type AuthStatus = 'checking' | 'authenticated' | 'unauthenticated' | 'connection_error' | 'rate_limited';

/**
 * 身份入口的三种模式（PRD 3.2/3.4）：
 * - `login`   ：用户名 + 密码
 * - `register`：邀请码 + 用户名 + 密码（成功即自动登录）
 * - `invite`  ：只用邀请码（Q1=C：码就是账号的凭据）
 */
type AuthMode = 'login' | 'register' | 'invite';

const AUTH_MODE_LABELS: Array<{ mode: AuthMode; label: string; testId: string }> = [
  { mode: 'login', label: '登录', testId: 'auth-mode-login' },
  { mode: 'register', label: '注册', testId: 'auth-mode-register' },
  { mode: 'invite', label: '用邀请码进入', testId: 'auth-mode-invite' },
];

/**
 * 管理入口守卫：只让管理员看到「模型设置」。
 *
 * 判定结果来自服务端 `/auth/permissions`，而它和 `require_maintainer` 用**同一套**
 * 解析与判定，所以这里挡住的与后端会拒绝的永远是同一批人 —— 不会出现
 * 「菜单藏起来了但接口仍放行」或「接口拒绝但菜单还在」的分裂。
 *
 * 不用静默重定向：手输 URL 时被弹回首页会让人以为链接坏了；直接说明原因更好排查。
 */
function RequireAdmin({ children }: { children: ReactNode }) {
  const isAdmin = usePermissionStore((state) => state.isAdmin);
  // 未确定（还没问过服务端）时不渲染任何管理内容：既不会漏给非管理员，
  // 也不会让管理员进入页面时先闪一下「仅管理员可见」。
  if (isAdmin === null) {
    return <div className="flex flex-1 items-center justify-center px-6" data-testid="admin-guard-pending" />;
  }
  if (isAdmin) return <>{children}</>;

  return (
    <div className="flex flex-1 items-center justify-center px-6" data-testid="admin-only-notice">
      <div className="max-w-md rounded-lg border border-warning/40 bg-white p-6 text-center shadow-lg dark:bg-surface-800">
        <ShieldAlert size={28} className="mx-auto text-warning" />
        <h1 className="mt-3 text-lg font-semibold text-surface-800 dark:text-surface-100">仅管理员可见</h1>
        <p className="mt-2 text-sm text-surface-500">
          「模型设置」是服务器级配置（接口密钥、模型档位），只有管理员账号可以查看和修改。
          如果你觉得自己应该有权限，请联系管理员把你的账号加入管理员名单。
        </p>
      </div>
    </div>
  );
}

export default function App() {
  const theme = useSettingsStore((s) => s.theme);
  const [degradedToast, setDegradedToast] = useState<{ id: number; kind: string; errorCode?: string; traceId?: string } | null>(null);
  const [inviteToken, setInviteTokenState] = useState(() => getInviteToken());
  const [inviteOpen, setInviteOpen] = useState(false);
  /**
   * 用户协议（免责条款）是否已勾选：**勾选即视为同意**。
   * 每次打开登录闸门都重置为未勾选 —— 同意必须是一次明确动作，不能沿用上一次。
   */
  const [termsAccepted, setTermsAccepted] = useState(false);
  const [termsOpen, setTermsOpen] = useState(false);
  const termsButtonRef = useRef<HTMLButtonElement | null>(null);
  const termsWasOpenRef = useRef(false);
  const [authStatus, setAuthStatus] = useState<AuthStatus>('checking');
  const [authMode, setAuthMode] = useState<AuthMode>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [usernameHint, setUsernameHint] = useState<UsernameAvailability | null>(null);
  const [authError, setAuthError] = useState('');
  const [authBusy, setAuthBusy] = useState(false);
  const toastTimerRef = useRef<number | null>(null);

  const setPermissions = usePermissionStore((state) => state.setPermissions);

  // 每次闸门重新打开都把勾选清掉：同意必须是一次明确动作，不能沿用上次的勾。
  useEffect(() => {
    if (inviteOpen) setTermsAccepted(false);
  }, [inviteOpen]);

  useEffect(() => {
    if (termsWasOpenRef.current && !termsOpen && inviteOpen) {
      termsButtonRef.current?.focus();
    }
    termsWasOpenRef.current = termsOpen;
  }, [termsOpen, inviteOpen]);

  /**
   * 会话不可用时的权限判定。
   *
   * `/auth/me` 只认会话 cookie，而老 `.env` 邀请码走的是**请求头**通道 —— `me` 看不到它，
   * 于是用老码登录的管理员会被当成普通用户、管理入口凭空消失。
   * `/auth/permissions` 走 `current_user_id`（cookie 或请求头都能解析），且与后端
   * `require_maintainer` 同源，所以这时单独问它一次；没有凭据时连请求都不发，
   * 直接按非管理员处理。会话可用时**不**打这个请求，直接用 `/auth/me` 的结果。
   */
  const resolvePermissionsWithoutSession = useCallback(async () => {
    if (!getInviteToken()) {
      setPermissions(false);
      return;
    }
    try {
      const json = await authApi.permissions();
      setPermissions(Boolean(json.data?.isAdmin));
    } catch {
      setPermissions(false);
    }
  }, [setPermissions]);

  const checkAuth = useCallback(async () => {
    setAuthStatus('checking');
    try {
      const json = await authApi.me();
      // 会话可用：直接用它的 isAdmin，不额外打 `/auth/permissions`。
      setPermissions(Boolean(json.data?.isAdmin));
      setInviteOpen(false);
      setAuthStatus('authenticated');
    } catch (error) {
      if (isUnauthorizedError(error)) {
        // 服务端明确未认证：清掉可能过期的本地标记，回到未认证状态。
        // 保留受控旧 invite 登录——有邀请码时仍以邀请码为凭据（不弹登录层）。
        if (hasAuthMarker()) {
          try { window.localStorage.removeItem(AUTH_MARKER_KEY); } catch { /* ignore */ }
        }
        setAuthStatus('unauthenticated');
        await resolvePermissionsWithoutSession();
        setInviteOpen(BETA_ACCESS_REQUIRED && !getInviteToken());
        return;
      }
      if (isRateLimitedError(error)) {
        // 限流是可重试的暂时状态，不能当成"未登录"而弹登录层（那会让用户以为登录失效）。
        setAuthStatus('rate_limited');
        return;
      }
      if (isConnectionError(error)) {
        // 网络中断 / 超时 / 5xx：显示连接失败，不误当密码错误，也不继续展示旧用户作品。
        setAuthStatus('connection_error');
        return;
      }
      setAuthStatus('unauthenticated');
      await resolvePermissionsWithoutSession();
      setInviteOpen(BETA_ACCESS_REQUIRED && !getInviteToken());
    }
  }, [resolvePermissionsWithoutSession]);

  useEffect(() => {
    void checkAuth();
  }, [checkAuth]);


  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
  }, [theme]);

  useEffect(() => {
    const handleDegraded = (event: Event) => {
      const detail = (event as CustomEvent<{ kind?: string; errorCode?: string; traceId?: string }>).detail;
      setDegradedToast({ id: Date.now(), kind: detail?.kind || 'unknown', errorCode: detail?.errorCode || undefined, traceId: detail?.traceId || undefined });
      if (toastTimerRef.current !== null) window.clearTimeout(toastTimerRef.current);
      toastTimerRef.current = window.setTimeout(() => setDegradedToast(null), DEGRADED_TOAST_DURATION_MS);
    };

    const handleDegradedCleanup = () => {
      if (toastTimerRef.current !== null) window.clearTimeout(toastTimerRef.current);
    };

    window.addEventListener(AI_DEGRADED_EVENT, handleDegraded);
    window.addEventListener('beforeunload', handleDegradedCleanup);
    return () => {
      window.removeEventListener(AI_DEGRADED_EVENT, handleDegraded);
      window.removeEventListener('beforeunload', handleDegradedCleanup);
      handleDegradedCleanup();
    };
  }, []);

  // 认证未确认时不阻塞渲染（保持既有挂载时序，避免编辑器章节恢复竞态）；
  // 仅在「连接失败」或「必须登录」时暂停工作区，避免展示过期账户内容。
  // 保留 legacy invite：有邀请码时视为可用凭据，不弹登录层。
  const connectionBlocked = BETA_ACCESS_REQUIRED && authStatus === 'connection_error';
  const rateLimited = BETA_ACCESS_REQUIRED && authStatus === 'rate_limited';
  const loginRequired = BETA_ACCESS_REQUIRED && authStatus === 'unauthenticated' && inviteOpen;
  const showWorkspace = !connectionBlocked && !rateLimited && !loginRequired;

  /** 注册模式下查一次用户名占用（失焦时调用）；失败不阻塞提交。 */
  const checkUsername = async () => {
    const account = username.trim();
    if (authMode !== 'register' || !account) {
      setUsernameHint(null);
      return;
    }
    try {
      const json = await authApi.usernameAvailable(account);
      setUsernameHint(json.data);
    } catch {
      setUsernameHint(null); // 退化为由服务端在提交时给 409/400
    }
  };

  const submitAuth = async () => {
    try {
      setAuthError('');
      // 兜底：按钮在未勾选时已禁用，这里再挡一次（回车提交、以及将来新增入口都走这里）。
      if (!termsAccepted) {
        setAuthError('请先阅读并同意《用户协议与免责条款》后再继续。');
        return;
      }
      setAuthBusy(true);

      if (authMode === 'register') {
        const invite = inviteToken.trim();
        const account = username.trim();
        if (!invite) {
          setAuthError('请填写邀请码。');
          return;
        }
        if (!account || !password) {
          setAuthError('请填写用户名和密码。');
          return;
        }
        if (password !== confirmPassword) {
          setAuthError('两次输入的密码不一致。');
          return;
        }
        // 注册成功时服务端**已经种好会话 cookie**（Q4 自动登录）。
        await authApi.register(account, password, invite, termsAccepted);
        clearLocalIdentity();
        markAuthenticated();
        setInviteOpen(false);
        window.location.reload();
        return;
      }

      if (authMode === 'invite') {
        const token = inviteToken.trim();
        if (!token) {
          setAuthError('请填写邀请码。');
          return;
        }
        try {
          await authApi.loginWithInvite(token, termsAccepted);
          clearLocalIdentity();
          markAuthenticated();
          setInviteOpen(false);
        } catch (error) {
          // 只有"老 `.env` 码映射到的账号行不存在"才允许回落到「请求头带码」的遗留通道。
          // 其它 401/403（码写错了、还没被注册、已停用）**必须原样告知并留在弹层**——
          // 否则会把一个错码写进 sessionStorage，登录闸门被它关掉，用户被放进创作台
          // 而每个接口都 401，正是本文件开头声称要避免的"假登录状态"。
          if (isLegacyAccountMissing(error)) {
            clearLocalIdentity();
            setInviteToken(token);
          } else {
            throw error;
          }
        }
        window.location.reload();
        return;
      }

      const account = username.trim();
      await authApi.login(account, password, termsAccepted);
      // 切换账号：先清掉上一账号的本地身份，再标记当前账号。
      clearLocalIdentity();
      markAuthenticated();
      setInviteOpen(false);
      window.location.reload();
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : '登录失败。');
    } finally {
      setAuthBusy(false);
    }
  };

  /** 表单提交（回车或点主按钮都会走到这里）。必须 preventDefault，否则浏览器会真的提交。 */
  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void submitAuth();
  };

  const authPlaceholder = (() => {
    if (rateLimited) {
      return (
        <div
          data-testid="auth-rate-limited"
          className="flex min-h-screen items-center justify-center bg-surface-50 px-6 dark:bg-surface-900"
        >
          <div className="w-full max-w-md rounded-lg border border-warning/50 bg-white p-6 text-center shadow-lg dark:bg-surface-800">
            <AlertTriangle size={28} className="mx-auto text-warning" />
            <h1 className="mt-3 text-lg font-semibold text-surface-800 dark:text-surface-100">请求过于频繁</h1>
            <p className="mt-2 text-sm text-surface-500">
              后端按 IP 的每分钟请求配额已用尽（默认 30 次/60 秒，可用后端 <code>RATE_LIMIT</code> 调整）。
              请等待约一分钟后重试。为避免显示过期内容，页面已暂停加载。
            </p>
            <div className="mt-5 flex justify-center gap-3">
              <Button onClick={() => { void checkAuth(); }} data-testid="auth-retry">重新连接</Button>
            </div>
          </div>
        </div>
      );
    }
    if (!connectionBlocked) return null;
    return (
      <div
        data-testid="auth-connection-error"
        className="flex min-h-screen items-center justify-center bg-surface-50 px-6 dark:bg-surface-900"
      >
        <div className="w-full max-w-md rounded-lg border border-error/40 bg-white p-6 text-center shadow-lg dark:bg-surface-800">
          <AlertTriangle size={28} className="mx-auto text-error" />
          <h1 className="mt-3 text-lg font-semibold text-surface-800 dark:text-surface-100">无法连接服务器</h1>
          <p className="mt-2 text-sm text-surface-500">
            暂时无法确认登录状态，请检查网络或后端服务后重试。为避免显示过期的账户内容，页面已暂停加载。
          </p>
          <div className="mt-5 flex justify-center gap-3">
            <Button onClick={() => { void checkAuth(); }} data-testid="auth-retry">重新连接</Button>
            <Button
              variant="secondary"
              onClick={() => { setAuthStatus('unauthenticated'); setInviteOpen(BETA_ACCESS_REQUIRED && !getInviteToken()); }}
              data-testid="auth-use-invite"
            >使用邀请码登录</Button>
          </div>
        </div>
      </div>
    );
  })();

  return (
    <ToastProvider>
      {showWorkspace ? (
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/project/:projectId/*" element={<ProjectWorkspace />} />
            <Route path="/settings" element={<RequireAdmin><SettingsPage /></RequireAdmin>} />
            <Route path="/admin" element={<RequireAdmin><AdminConsole /></RequireAdmin>} />
            <Route path="/account" element={<AccountPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      ) : authPlaceholder}

      {degradedToast && (
        <div
          role="status"
          data-testid="ai-degraded-toast"
          className="fixed right-4 top-4 z-50 flex max-w-sm items-start gap-3 rounded-lg border border-warning bg-warning-soft px-4 py-3 text-sm text-warning shadow-lg dark:border-warning/40 dark:bg-warning-soft-dark dark:text-warning-dark"
        >
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-warning" />
          <div className="min-w-0 flex-1">
            <p className="font-semibold">AI 服务降级</p>
            <p className="mt-0.5 text-xs leading-relaxed opacity-90">
              {DEGRADED_ERROR_MESSAGES[degradedToast.errorCode || '']
                || DEGRADED_MESSAGES[degradedToast.kind]
                || DEFAULT_DEGRADED_MESSAGE}
            </p>
            {(degradedToast.errorCode || degradedToast.traceId) && (
              <p className="mt-1 text-[11px] opacity-75">
                {degradedToast.errorCode ? `原因：${degradedToast.errorCode}` : ''}
                {degradedToast.traceId ? ` · 诊断编号：${degradedToast.traceId}` : ''}
              </p>
            )}
          </div>
          <button
            onClick={() => setDegradedToast(null)}
            className="shrink-0 rounded p-1 text-warning transition-colors hover:bg-warning-soft dark:text-warning-dark dark:hover:bg-warning-soft-dark"
            aria-label="关闭提示"
            title="关闭提示"
          >
            <X size={14} />
          </button>
        </div>
      )}
      <Modal
        // 阅读协议时暂时收起登录弹窗，避免两个焦点陷阱和 inert 状态互相干扰。
        // 表单值由 App 保存，返回时不会丢失，也不会自动勾选同意。
        open={inviteOpen && !termsOpen}
        onClose={() => undefined}
        title="受控 Beta 访问"
        width="sm"
        // 登录闸门必须在这里做完决定：不渲染那个点了没反应的"关闭"按钮，
        // Escape 与点遮罩也不生效（背景已 inert，Tab 跑不到后面去）。
        dismissible={false}
      >
        {/* 真的用 <form>：
            ① 输入框里按回车 = 提交（打完密码顺手回车的习惯）；
            ② 浏览器只在检测到真实表单提交时才提示"保存密码"，不做成 form 就丢了这个能力；
            ③ method="post" 是防御性写法 —— 万一 preventDefault 没生效，凭据也不会落到 URL 上。 */}
        <form className="space-y-4" method="post" onSubmit={handleSubmit}>
          {/* 三种身份入口（PRD 3.2/3.4）：登录 / 注册 / 只用邀请码。
              语义用 radiogroup 而不是 tablist：没有 tabpanel 的 "tab" 是语义撒谎，
              而单选组准确描述了"三选一"。触达高度在弹层内提到 44px（新用户第一眼、常在手机上）。 */}
          <div
            className="flex gap-1 rounded-lg bg-surface-100 p-1 dark:bg-surface-800"
            role="radiogroup"
            aria-label="登录方式"
          >
            {AUTH_MODE_LABELS.map((entry) => (
              <button
                key={entry.mode}
                type="button"
                role="radio"
                aria-checked={authMode === entry.mode}
                data-testid={entry.testId}
                onClick={() => {
                  setAuthMode(entry.mode);
                  setAuthError('');
                  setUsernameHint(null);
                }}
                className={
                  authMode === entry.mode
                    ? 'min-h-[44px] flex-1 rounded-md bg-white px-2 py-2 text-xs font-medium text-surface-800 shadow-sm dark:bg-surface-700 dark:text-surface-100'
                    : 'min-h-[44px] flex-1 rounded-md px-2 py-2 text-xs text-surface-500 hover:text-surface-700 dark:text-surface-400'
                }
              >
                {entry.label}
              </button>
            ))}
          </div>

          <p className="text-sm text-surface-600 dark:text-surface-300">
            {authMode === 'register'
              ? '用邀请码注册新账号：成功后会直接登录。'
              : authMode === 'invite'
                ? '只用邀请码进入（码就是账号的凭据）；如果这个码还没被注册，会提示你先注册。'
                : '登录已有账户。'}
          </p>

          {authMode === 'register' && (
            <Input
              type="text"
              label="邀请码"
              name="invite"
              autoComplete="off"
              value={inviteToken}
              onChange={(event) => setInviteTokenState(event.target.value)}
              placeholder="作者发给你的那串码"
              data-testid="register-invite-input"
            />
          )}

          {authMode !== 'invite' && (
            <Input
              label={authMode === 'register' ? '用户名' : '账户名'}
              name="username"
              autoComplete="username"
              maxLength={30}
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
                setUsernameHint(null);
              }}
              onBlur={() => void checkUsername()}
              error={authMode === 'register' && usernameHint && !usernameHint.available ? usernameHint.reason : undefined}
              errorTestId="auth-username-hint"
              placeholder={authMode === 'register' ? '3–30 字符，可用中文' : '你的账户名'}
              data-testid="auth-username-input"
            />
          )}

          {authMode !== 'invite' && (
            <Input
              type="password"
              label="密码"
              name="password"
              autoComplete={authMode === 'register' ? 'new-password' : 'current-password'}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={authMode === 'register' ? '至少 8 位，不能与用户名相同' : ''}
              data-testid="auth-password-input"
            />
          )}

          {authMode === 'register' && (
            <Input
              type="password"
              label="确认密码"
              name="confirm-password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              placeholder="再输一次"
              data-testid="register-confirm-input"
            />
          )}

          {/* 只有「用邀请码进入」模式才需要这个框：登录模式上摆一个不生效的邀请码框，
              会让用户以为"三样都要填"，而实际上输入被丢弃。 */}
          {authMode === 'invite' && (
            <Input
              type="text"
              label="邀请码"
              name="invite"
              autoComplete="off"
              value={inviteToken}
              onChange={(event) => setInviteTokenState(event.target.value)}
              placeholder="你的邀请码"
              data-testid="beta-invite-input"
            />
          )}

          {authError ? (
            <p role="alert" className="text-sm text-error" data-testid="auth-error">
              {authError}
            </p>
          ) : null}

          {/* 用户协议（免责条款）：勾选即视为同意；服务端按版本留痕（users.terms_version）。 */}
          <div className="flex items-start gap-2 rounded-lg bg-surface-100 px-3 py-2 dark:bg-surface-800">
            <input
              id="auth-terms"
              type="checkbox"
              className="mt-0.5 h-4 w-4 shrink-0 accent-primary-600"
              checked={termsAccepted}
              onChange={(event) => setTermsAccepted(event.target.checked)}
              data-testid="auth-terms-checkbox"
            />
            <label htmlFor="auth-terms" className="text-xs leading-5 text-surface-600 dark:text-surface-300">
              我已阅读并同意
              <button
                ref={termsButtonRef}
                type="button"
                className="mx-0.5 font-medium text-primary-600 underline decoration-dotted hover:text-primary-700"
                onClick={() => setTermsOpen(true)}
                data-testid="auth-terms-open"
              >
                《用户协议与免责条款》
              </button>
              ，承诺不创作、不传播违法违规内容。
            </label>
          </div>

          <div className="flex justify-end">
            <Button
              type="submit"
              className="min-h-[44px]"
              disabled={
                authBusy ||
                !termsAccepted ||
                (authMode === 'register'
                  ? !inviteToken.trim() || !username.trim() || !password || !confirmPassword
                  : authMode === 'invite'
                    ? !inviteToken.trim()
                    : !username.trim() || !password)
              }
              data-testid="beta-invite-submit"
            >{authBusy ? '处理中…' : authMode === 'register' ? '注册并进入' : '进入创作台'}</Button>
          </div>
        </form>
      </Modal>
      <TermsDialog open={termsOpen} onClose={() => setTermsOpen(false)} />
    </ToastProvider>
  );
}
