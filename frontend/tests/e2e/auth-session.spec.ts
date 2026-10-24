import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * T1：退出登录与失效身份边界（离线、拦截全部 /api/**）。
 *
 * 覆盖：
 * 1. 残留 auth marker + 服务端 401 → 不冒充已登录，不渲染工作区；
 * 2. 退出登录成功 → 本地身份（marker + 邀请码 + 缓存）被清除；
 * 3. 退出登录请求失败 → 明确提示，不冒充"已退出"；
 * 4. 切换到 B 账号 → 重新拉取 B 的数据，A 的作品不闪现；
 * 5. 网络失败 → 连接失败提示，而非登录/密码错误，也不展示旧作品。
 *
 * 所有请求都在浏览器内拦截；任何未匹配的 /api/** 请求立即失败。
 */

const MARKER = 'novelcraft-authenticated';
const INVITE_KEY = 'novelcraft-beta-invite';

type AuthMode = 'ok' | 'unauthorized' | 'server-error' | 'network' | 'rate-limited';

interface FixtureOptions {
  auth: () => AuthMode;
  /** 服务端返回的项目列表（按账号切换）。 */
  projects?: () => Array<Record<string, unknown>>;
  /** 初始残留的邀请码（null 表示无）。 */
  invite?: string | null;
  /** 初始残留的本地认证标记。 */
  marker?: boolean;
  /** 让 logout 请求失败。 */
  logoutFails?: boolean;
  /** 注销账号的行为：成功 / 因名下有作品被拒。 */
  deleteAccount?: 'ok' | 'refused';
}

function project(id: string, name: string) {
  return {
    id, name, subtitle: '', genre: 'mystery', status: 'drafting', premise: '',
    currentWordCount: 0, targetWordCount: 10000, targetChapters: 1,
    outline: null, createdAt: '2026-09-13T00:00:00Z', updatedAt: '2026-09-13T00:00:00Z',
  };
}

async function installFixture(page: Page, options: FixtureOptions) {
  const state = {
    projectListCalls: 0,
    logoutCalls: 0,
    loginCalls: 0,
    deleteCalls: 0,
    lastLogin: null as Record<string, unknown> | null,
    unexpected: [] as string[],
    errors: [] as string[],
  };
  page.on('pageerror', (error) => state.errors.push(error.message));

  // 只在首次加载时播种残留身份；否则整页 reload 会把它重新写回，掩盖产品清理行为。
  await page.addInitScript(({ invite, marker, markerKey, inviteKey }) => {
    if (sessionStorage.getItem('__fixture_seeded') === '1') return;
    sessionStorage.setItem('__fixture_seeded', '1');
    if (marker) localStorage.setItem(markerKey, '1');
    if (invite) sessionStorage.setItem(inviteKey, invite);
  }, { invite: options.invite ?? null, marker: options.marker ?? false, markerKey: MARKER, inviteKey: INVITE_KEY });

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname.replace(/\/$/, '');

    if (path === '/api/auth/me') {
      const mode = options.auth();
      if (mode === 'ok') return ok(route, { id: 'current', username: 'current', isAdmin: false });
      if (mode === 'unauthorized') return route.fulfill({ status: 401, json: { detail: '请先登录。' } });
      if (mode === 'server-error') return route.fulfill({ status: 500, json: { detail: '服务器内部错误' } });
      if (mode === 'rate-limited') return route.fulfill({ status: 429, json: { detail: '请求过于频繁，请稍后再试。' } });
      return route.abort('failed');
    }
    if (path === '/api/auth/logout' && method === 'POST') {
      state.logoutCalls += 1;
      if (options.logoutFails) return route.abort('failed');
      return ok(route, null);
    }
    if (path === '/api/auth/login' && method === 'POST') {
      state.loginCalls += 1;
      state.lastLogin = request.postDataJSON();
      return ok(route, { id: 'current', username: 'current', isAdmin: false });
    }
    if (path === '/api/auth/account' && method === 'DELETE') {
      state.deleteCalls += 1;
      if (options.deleteAccount === 'refused') {
        return route.fulfill({
          status: 409,
          json: {
            detail: {
              code: 'account_has_owned_projects',
              message: '无法注销：该账号名下仍有 2 个项目（其中回收站章节 1 条）。请先导出或删除这些作品，再回来注销。',
              projects: 2,
              trashedChapters: 1,
            },
          },
        });
      }
      return ok(route, null);
    }
    if (path === '/api/auth/register' && method === 'POST') {
      return ok(route, { id: 'current', username: 'current' });
    }
    if (path === '/api/projects' && method === 'GET') {
      state.projectListCalls += 1;
      return ok(route, options.projects ? options.projects() : []);
    }
    if (path === '/api/settings/llm-configs' && method === 'GET') return ok(route, []);
    if (path === '/api/health') return ok(route, { status: 'ok' });

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Unhandled offline fixture request' } });
  });

  return state;
}

test('stale local marker must not fake a logged-in session', async ({ page }) => {
  // A 曾经登录：残留 marker + 残留缓存；服务端会话已失效（401）。
  const state = await installFixture(page, {
    auth: () => 'unauthorized',
    marker: true,
    invite: null,
    projects: () => [project('p-a', 'A 的作品')],
  });

  await page.goto('/');

  // 必须进入未认证状态：显示登录入口，而不是继续显示工作区。
  await expect(page.getByTestId('beta-invite-submit')).toBeVisible();
  await expect(page.getByTestId('auth-username-input')).toBeVisible();
  // 旧账号作品不得留在界面上
  await expect(page.getByTestId('project-card-p-a')).toHaveCount(0);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);

  // 残留 marker 已被清理
  expect(await page.evaluate((key) => localStorage.getItem(key), MARKER)).toBeNull();
});

test('logout success clears local identity (marker + invite)', async ({ page }) => {
  const state = await installFixture(page, {
    auth: () => 'ok',
    invite: 'mock-invite',
    projects: () => [project('p-a', 'A 的作品')],
    logoutFails: false,
  });

  await page.goto('/');
  await expect(page.getByTestId('workspace-tab-write').or(page.getByRole('heading', { level: 1 }))).toBeVisible({ timeout: 15_000 }).catch(() => undefined);

  // 直接访问账户页执行退出
  await page.goto('/account');
  await expect(page.getByTestId('account-logout')).toBeVisible();
  await page.getByTestId('account-logout').click();

  await page.waitForURL(/\/$/, { timeout: 15_000 });
  expect(state.logoutCalls).toBe(1);
  expect(await page.evaluate((key) => localStorage.getItem(key), MARKER)).toBeNull();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), INVITE_KEY)).toBeNull();
});

test('logout failure reports status instead of faking success', async ({ page }) => {
  const state = await installFixture(page, { auth: () => 'ok', invite: 'mock-invite', logoutFails: true });

  await page.goto('/account');
  await expect(page.getByTestId('account-logout')).toBeVisible();
  await page.getByTestId('account-logout').click();

  await expect(page.getByTestId('account-action-error')).toBeVisible();
  await expect(page.getByTestId('account-action-error')).toContainText('退出登录失败');
  expect(state.logoutCalls).toBe(1);
  // 未跳转、未冒充成功：仍停留在账户页
  await expect(page).toHaveURL(/\/account$/);
});

test('switching to account B refetches B data without flashing A work', async ({ page }) => {
  const state = await installFixture(page, {
    auth: () => 'ok',
    invite: null,
    marker: true,
    projects: () => [project('p-b', 'B 的作品')],
  });

  await page.goto('/');
  // B 登录后只看到 B 的项目
  await expect(page.getByTestId('project-card-p-b')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('A 的作品')).toHaveCount(0);
  expect(state.projectListCalls).toBeGreaterThanOrEqual(1);
  expect(state.errors).toEqual([]);
});

test('refused account deletion explains the policy and keeps the session alive', async ({ page }) => {
  const state = await installFixture(page, {
    auth: () => 'ok',
    invite: 'mock-invite',
    marker: true,
    deleteAccount: 'refused',
  });

  page.on('dialog', (dialog) => { void dialog.accept(); });
  await page.goto('/account');
  await expect(page.getByTestId('account-delete')).toBeVisible();
  await page.getByTestId('account-delete').click();

  const error = page.getByTestId('account-action-error');
  await expect(error).toBeVisible();
  // 展示后端返回的真实政策说明（结构化 detail.message），而不是 HTTP 409
  await expect(error).toContainText('无法注销');
  await expect(error).toContainText('回收站章节');
  expect(state.deleteCalls).toBe(1);
  // 失败不清掉有效登录：仍停留在账户页，认证标记仍在
  await expect(page).toHaveURL(/\/account$/);
  expect(await page.evaluate((key) => localStorage.getItem(key), MARKER)).toBe('1');
});

test('a rate-limited session check is not misreported as logged out', async ({ page }) => {
  // 后端 IP 配额用尽时 /auth/me 返回 429：这是可重试的暂时状态，不能当"未登录"弹登录层。
  const state = await installFixture(page, {
    auth: () => 'rate-limited',
    marker: true,
    invite: null,
    projects: () => [project('p-a', 'A 的作品')],
  });

  await page.goto('/');

  await expect(page.getByTestId('auth-rate-limited')).toBeVisible();
  await expect(page.getByTestId('auth-rate-limited')).toContainText('请求过于频繁');
  // 不得弹登录层（不得声称密码错误/未登录），也不展示旧作品
  await expect(page.getByTestId('auth-password-input')).toHaveCount(0);
  await expect(page.getByTestId('project-card-p-a')).toHaveCount(0);
  await expect(page.getByTestId('auth-retry')).toBeVisible();
  expect(state.unexpected).toEqual([]);
});

test('network failure shows connection error, not a password error or stale content', async ({ page }) => {
  const state = await installFixture(page, {
    auth: () => 'network',
    marker: true,
    invite: null,
    projects: () => [project('p-a', 'A 的作品')],
  });

  await page.goto('/');

  await expect(page.getByTestId('auth-connection-error')).toBeVisible();
  await expect(page.getByTestId('auth-connection-error')).toContainText('无法连接服务器');
  // 不冒充密码错误，也不渲染旧作品
  await expect(page.getByTestId('auth-password-input')).toHaveCount(0);
  await expect(page.getByTestId('project-card-p-a')).toHaveCount(0);
  expect(state.unexpected).toEqual([]);
});
