import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 管理入口的可见性（离线，拦截全部 /api/**）。
 *
 * 需求：普通用户**看不到**「模型设置」，只有管理员账号能看到。
 *
 * 这里要钉的不只是"菜单藏起来了"，而是三件更容易出错的事：
 * A. 管理员能看到并进得去（否则就是把功能藏坏了）；
 * B. 普通用户看不到、**手输 URL 也打不开**，而且**不会**悄悄去拉模型配置
 *    （否则就变成"藏起来但数据照样请求"，是假隐藏）；
 * C. 把管理入口收起来，不能顺手拿走普通用户导出**自己**数据的能力；
 * D. 没有会话、只有老 `.env` 邀请码的通道也要判对 —— `App` 会额外问一次
 *    `/auth/permissions`，而这条通道下 `/auth/me` 是看不到的。
 */

const INVITE_KEY = 'novelcraft-beta-invite';

interface FixtureOptions {
  /** `/api/auth/me` 的返回：管理员 / 普通用户 / 未登录。 */
  session: 'admin' | 'ordinary' | 'anonymous';
  /** 是否在 sessionStorage 里播种老 `.env` 邀请码（模拟"邀请码当凭据"通道）。 */
  legacyInvite?: string;
  /** `/api/auth/permissions` 的返回（只有无会话时才会被调用）。 */
  permissions?: { isAdmin: boolean };
  /** `/api/projects` 的返回。 */
  projects?: unknown[];
}

async function installFixture(page: Page, options: FixtureOptions) {
  const state = {
    /** 模型配置接口被请求的次数 —— 普通用户必须始终为 0。 */
    settingsCalls: [] as string[],
    permissionCalls: 0,
    unexpected: [] as string[],
    errors: [] as string[],
  };
  page.on('pageerror', (error) => state.errors.push(error.message));

  await page.addInitScript(({ invite, inviteKey }) => {
    localStorage.setItem('novelcraft-authenticated', '1');
    if (invite) sessionStorage.setItem(inviteKey, invite);
  }, { invite: options.legacyInvite ?? null, inviteKey: INVITE_KEY });

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/\/$/, '');
    const method = request.method();

    if (path === '/api/auth/me') {
      if (options.session === 'anonymous') {
        return route.fulfill({ status: 401, json: { detail: '请先登录。' } });
      }
      return ok(route, {
        id: options.session === 'admin' ? 'admin-1' : 'user-1',
        username: options.session === 'admin' ? '作者' : '普通作者',
        isAdmin: options.session === 'admin',
      });
    }

    // 只在"没有会话"的通道下才会被问到（见 App.resolvePermissionsWithoutSession）。
    if (path === '/api/auth/permissions' && method === 'GET') {
      state.permissionCalls += 1;
      return ok(route, {
        userId: options.permissions?.isAdmin ? 'admin-1' : 'user-1',
        isAdmin: Boolean(options.permissions?.isAdmin),
      });
    }

    if (path === '/api/auth/invites/mine' && method === 'GET') return ok(route, []);
    if (path === '/api/projects' && method === 'GET') return ok(route, options.projects ?? []);
    if (path === '/api/health') return ok(route, { status: 'ok' });

    // 模型配置接口：记录**再**放行，这样"普通用户不该请求它"能被断言出来。
    if (path.startsWith('/api/settings/')) {
      state.settingsCalls.push(`${method} ${path}`);
      if (path === '/api/settings/llm-configs' && method === 'GET') return ok(route, []);
      if (path === '/api/settings/llm-health' && method === 'GET') {
        return ok(route, { configured: true, provider: 'arts', model: 'synthetic-model', ok: false, error: '', checked: false });
      }
      if (path === '/api/settings/llm-health' && method === 'POST') {
        return ok(route, { configured: true, provider: 'arts', model: 'synthetic-model', ok: true, error: '', checked: true });
      }
      return ok(route, []);
    }

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 500, json: { detail: 'fixture 未覆盖的请求' } });
  });

  return state;
}

test('A: 管理员能在侧边栏看到「模型设置」并正常打开', async ({ page }) => {
  const state = await installFixture(page, { session: 'admin' });

  await page.goto('/');
  await expect(page.getByTestId('dashboard-page')).toBeVisible();

  const navSettings = page.getByTestId('nav-settings');
  await expect(navSettings).toBeVisible();
  await expect(navSettings).toContainText('模型设置');

  await navSettings.click();
  await expect(page.getByRole('heading', { name: '模型配置' })).toBeVisible();
  await expect(page.getByTestId('admin-only-notice')).toHaveCount(0);
  // 管理员这条路上不该有"仅管理员可见"的闪烁残留
  await expect(page.getByTestId('admin-guard-pending')).toHaveCount(0);
  // 管理员这条路径有会话，不需要额外问权限接口
  expect(state.permissionCalls).toBe(0);
  expect(state.unexpected).toEqual([]);
});

test('opening settings only reads configuration; a model probe requires an explicit click', async ({ page }) => {
  const state = await installFixture(page, { session: 'admin' });
  await page.goto('/settings');
  await expect(page.getByText(/尚未检测/)).toBeVisible();
  expect(state.settingsCalls.filter((call) => call === 'POST /api/settings/llm-health')).toEqual([]);
  await page.getByRole('button', { name: /检测连接/ }).click();
  await expect(page.getByText(/连通性正常/)).toBeVisible();
  expect(state.settingsCalls.filter((call) => call === 'POST /api/settings/llm-health')).toHaveLength(1);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('B: 普通用户看不到「模型设置」，手输 URL 也打不开且不会去拉模型配置', async ({ page }) => {
  const state = await installFixture(page, { session: 'ordinary' });

  await page.goto('/');
  await expect(page.getByTestId('dashboard-page')).toBeVisible();

  // 入口不存在（不是"灰掉"，而是根本不渲染）
  await expect(page.getByTestId('nav-settings')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '模型设置' })).toHaveCount(0);
  // 其它入口不受影响，避免"顺手把导航改坏了"
  await expect(page.getByTestId('nav-dashboard')).toBeVisible();
  await expect(page.getByTestId('nav-account')).toBeVisible();

  // 手输 URL：给出明确说明，且**不渲染**模型配置界面的任何内容
  await page.goto('/settings');
  await expect(page.getByTestId('admin-only-notice')).toBeVisible();
  await expect(page.getByTestId('admin-only-notice')).toContainText('仅管理员可见');
  await expect(page.getByRole('heading', { name: '模型配置' })).toHaveCount(0);

  // 关键：不是"藏起来但照拉" —— 一个模型配置请求都不该发出去
  expect(state.settingsCalls).toEqual([]);
  expect(state.permissionCalls).toBe(0);
  expect(state.errors).toEqual([]);
});

test('C: 收起管理入口后，普通用户仍能导出自己的数据备份', async ({ page }) => {
  // 「导出数据备份」原本在模型设置页；那页现在只给管理员看，
  // 而这份数据是普通作者自己的，所以必须能在账户页找到。
  const state = await installFixture(page, { session: 'ordinary' });

  await page.goto('/account');
  const exportButton = page.getByTestId('account-export-backup');
  await expect(exportButton).toBeVisible();
  await expect(exportButton).toContainText('导出数据备份');
  // 账户页不依赖模型配置接口
  expect(state.settingsCalls).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('D: 无会话但持老 .env 邀请码时，权限要问 /auth/permissions 才判得准', async ({ page }) => {
  // `/auth/me` 只认会话 cookie，看不到请求头里的老码；只看 `me` 会把这位管理员
  // 当成普通用户，管理入口凭空消失。所以这条通道要单独走 `/auth/permissions`。
  const state = await installFixture(page, {
    session: 'anonymous',
    legacyInvite: 'OLDC0DE',
    permissions: { isAdmin: true },
  });

  await page.goto('/');
  await expect(page.getByTestId('dashboard-page')).toBeVisible();

  expect(state.permissionCalls).toBeGreaterThanOrEqual(1);
  await expect(page.getByTestId('nav-settings')).toBeVisible();
  expect(state.unexpected).toEqual([]);
});
