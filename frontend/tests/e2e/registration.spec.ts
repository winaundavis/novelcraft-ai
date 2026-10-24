import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * 账号注册 · 邀请码三模式入口（离线，拦截全部 /api/**）。
 *
 * 覆盖 PRD 的验收标准里「前端可验证」的部分：
 * A. 注册成功 → 服务端已种会话 → **自动登录**进入创作台（Q4）；
 * B. 两次密码不一致 → 前端拦截，**不发请求**；
 * C. 用户名占用 → 失焦查询后给出原因（PRD 3.3）；
 * D. `pending` 码用「用邀请码进入」→ 403 原样告知，**不得**回落到老通道混进去；
 * E. 老 `.env` 码（服务端 401，映射身份没有账号行）→ 回落到「邀请码即凭据」老通道（Q2=A）；
 * F. 账户页「我的邀请码」只显示 hint，页面**不含明文**（Q1=C 的安全要求）。
 */

const MARKER = 'novelcraft-authenticated';
const INVITE_KEY = 'novelcraft-beta-invite';

/** 明文邀请码（测试用假码；真实库里只存哈希）。 */
const NEW_CODE = 'K7QM3XZP9TAB12CD';
const LEGACY_CODE = 'OLDC0DE';

type LoginBehavior = 'ok' | 'legacy-account-missing' | 'invalid' | 'pending' | 'disabled';

interface FixtureOptions {
  /** `/api/auth/me` 的行为：未登录 → 弹层；已登录 → 直接进工作台。 */
  session?: 'anonymous' | 'authenticated';
  /** 只用邀请码登录时服务端的反应。 */
  inviteLogin?: LoginBehavior;
  /** 用户名占用查询的返回。 */
  usernameAvailable?: { available: boolean; reason: string };
  /** 注册接口的行为。 */
  register?: 'ok' | 'used' | 'taken' | 'weak-password';
  /** 账户页要展示的已绑定邀请码。 */
  myInvites?: Array<Record<string, unknown>>;
}

async function installFixture(page: Page, options: FixtureOptions = {}) {
  const state = {
    registerCalls: [] as any[],
    loginCalls: [] as any[],
    availabilityCalls: [] as string[],
    myInviteCalls: 0,
    /** 无会话时的权限查询次数（只应在「有老邀请码」时才发生）。 */
    permissionCalls: 0,
    /** 注册成功后 `/auth/me` 就应变成已登录（模拟服务端已种 cookie）。 */
    signedIn: options.session === 'authenticated',
    unexpected: [] as string[],
    errors: [] as string[],
  };
  page.on('pageerror', (error) => state.errors.push(error.message));

  await page.addInitScript(() => {
    localStorage.setItem('novelcraft-authenticated', '1');
  });

  const ok = (route: Route, data: unknown) => route.fulfill({ json: { success: true, data } });

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname.replace(/\/$/, '');
    const body = request.postDataJSON?.() ?? null;

    if (path === '/api/auth/me') {
      if (state.signedIn) return ok(route, { id: 'u-1', username: '作者', isAdmin: false });
      return route.fulfill({ status: 401, json: { detail: '请先登录。' } });
    }

    // 会话不可用、但有老 `.env` 邀请码时，`App` 会再问一次权限（见 `resolvePermissionsWithoutSession`）。
    // 这条通道下它返回的就是「老码映射到的那个账号」的权限。
    if (path === '/api/auth/permissions' && method === 'GET') {
      state.permissionCalls += 1;
      return ok(route, { userId: 'u-1', isAdmin: false });
    }

    if (path === '/api/auth/username-available' && method === 'GET') {
      const name = url.searchParams.get('username') || '';
      state.availabilityCalls.push(name);
      return ok(route, options.usernameAvailable ?? { available: true, reason: '' });
    }

    if (path === '/api/auth/register' && method === 'POST') {
      state.registerCalls.push(body);
      const behavior = options.register ?? 'ok';
      if (behavior === 'used') {
        return route.fulfill({ status: 403, json: { detail: '该邀请码已被使用，请向邀请人索取新码。' } });
      }
      if (behavior === 'taken') {
        return route.fulfill({ status: 409, json: { detail: '该用户名已被占用，换一个吧。' } });
      }
      if (behavior === 'weak-password') {
        return route.fulfill({ status: 400, json: { detail: '密码至少 8 位，且不能与用户名相同。' } });
      }
      state.signedIn = true; // 服务端已种好会话 cookie
      return ok(route, { id: 'u-1', username: String(body?.username || ''), isAdmin: false });
    }

    if (path === '/api/auth/login' && method === 'POST') {
      state.loginCalls.push(body);
      const behavior = options.inviteLogin ?? 'ok';
      if (body?.invite && !body?.username) {
        if (behavior === 'pending') {
          return route.fulfill({ status: 403, json: { detail: '这个邀请码还没被注册，请先注册。' } });
        }
        if (behavior === 'disabled') {
          return route.fulfill({ status: 403, json: { detail: '该邀请码已被停用。' } });
        }
        if (behavior === 'legacy-account-missing') {
          // 只有这个**结构化 code** 才允许前端回落到"请求头带码"的遗留通道。
          return route.fulfill({
            status: 401,
            json: {
              detail: {
                code: 'legacy_account_missing',
                message: '邀请码对应的账号不存在，请联系管理员。',
              },
            },
          });
        }
        if (behavior === 'invalid') {
          // 码写错了：必须留在弹层，绝不能把错码当成凭据放进去。
          return route.fulfill({
            status: 403,
            json: { detail: { code: 'invite_invalid', message: '邀请码无效，请检查是否输错。' } },
          });
        }
      }
      state.signedIn = true;
      return ok(route, { id: 'u-1', username: String(body?.username || '资 深作者'), isAdmin: false });
    }

    if (path === '/api/auth/invites/mine' && method === 'GET') {
      state.myInviteCalls += 1;
      return ok(route, options.myInvites ?? []);
    }

    if (path === '/api/projects') return ok(route, []);

    state.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 500, json: { detail: 'fixture 未覆盖的请求' } });
  });

  return state;
}

async function gotoLoginModal(page: Page) {
  await page.goto('/');
  await expect(page.getByTestId('auth-mode-register')).toBeVisible();
  // 2026-10-18 起提交按钮**默认禁用**，必须先勾选用户协议（勾选即视为同意）。
  // 「未勾选时禁用」这条门禁由末尾专门的用例覆盖，这里只是让其它用例能正常提交。
  await page.getByTestId('auth-terms-checkbox').check();
}

/**
 * 勾选用户协议（不勾选时提交按钮必须禁用且不发请求）。
 *
 * 服务端同时按版本留痕（`users.terms_version` / `users.terms_accepted_at`），
 * 所以请求体必须带上 `termsAccepted: true`。
 */
async function acceptTerms(page: Page) {
  await page.getByTestId('auth-terms-checkbox').check();
}

test('协议阅读只保留一个活动弹窗，关闭后保留注册表单且不自动同意', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  await page.goto('/');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('register-invite-input').fill(NEW_CODE);
  await page.getByTestId('auth-username-input').fill('阅读后注册');
  await page.getByTestId('auth-password-input').fill('strong-pass-1');
  await page.getByTestId('register-confirm-input').fill('strong-pass-1');

  await page.getByTestId('auth-terms-open').click();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await expect(page.getByTestId('terms-dialog')).toBeVisible();
  await page.getByTestId('terms-close').click();

  await expect(page.getByTestId('auth-terms-open')).toBeFocused();
  await expect(page.locator('#root')).toHaveAttribute('inert', '');
  await expect(page.getByTestId('auth-username-input')).toHaveValue('阅读后注册');
  await expect(page.getByTestId('register-invite-input')).toHaveValue(NEW_CODE);
  await expect(page.getByTestId('auth-password-input')).toHaveValue('strong-pass-1');
  await expect(page.getByTestId('register-confirm-input')).toHaveValue('strong-pass-1');
  await expect(page.getByTestId('auth-terms-checkbox')).not.toBeChecked();
  await expect(page.getByTestId('beta-invite-submit')).toBeDisabled();
  expect(state.registerCalls).toEqual([]);
  expect(state.loginCalls).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('协议弹窗键盘焦点能够双向循环，Escape 只返回登录框', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  await gotoLoginModal(page);
  await page.getByTestId('auth-terms-open').click();
  const close = page.getByTestId('terms-dialog').getByRole('button', { name: '关闭', exact: true });
  await expect(close).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByTestId('terms-close')).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByRole('region', { name: '协议正文' })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(close).toBeFocused();
  await page.keyboard.press('Escape');

  await expect(page.getByTestId('terms-dialog')).toHaveCount(0);
  await expect(page.getByTestId('auth-terms-open')).toBeFocused();
  await expect(page.getByTestId('auth-terms-checkbox')).toBeChecked();
  await expect(page.locator('#root')).toHaveAttribute('inert', '');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('A: 注册成功即自动登录进入创作台，且请求体带上邀请码', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  await gotoLoginModal(page);

  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('register-invite-input').fill(NEW_CODE);
  await page.getByTestId('auth-username-input').fill('新作者');
  await page.getByTestId('auth-password-input').fill('strong-pass-1');
  await page.getByTestId('register-confirm-input').fill('strong-pass-1');
  await expect(page.getByTestId('beta-invite-submit')).toBeEnabled();
  await page.getByTestId('beta-invite-submit').click();

  // 注册请求体正确
  await expect.poll(() => state.registerCalls.length).toBe(1);
  expect(state.registerCalls[0]).toEqual({
    username: '新作者',
    password: 'strong-pass-1',
    invite: NEW_CODE,
    termsAccepted: true,
    termsVersion: '2026-10-18.1',
  });

  // 注册成功 → 自动登录：弹层消失、工作区出现（不需要再登录一次）
  await expect(page.getByTestId('auth-mode-register')).toHaveCount(0);
  await expect(page.getByTestId('dashboard-page')).toBeVisible();
  expect(state.loginCalls).toEqual([]); // 没有走登录接口
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('B: 两次密码不一致时不发请求，并明确提示', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  await gotoLoginModal(page);

  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('register-invite-input').fill(NEW_CODE);
  await page.getByTestId('auth-username-input').fill('新作者');
  await page.getByTestId('auth-password-input').fill('strong-pass-1');
  await page.getByTestId('register-confirm-input').fill('strong-pass-2');
  await page.getByTestId('beta-invite-submit').click();

  await expect(page.getByTestId('auth-error')).toContainText('两次输入的密码不一致');
  expect(state.registerCalls).toEqual([]); // 前端拦截，没打服务端
  expect(state.errors).toEqual([]);
});

test('C: 用户名占用在失焦时给出原因（不阻塞其它字段）', async ({ page }) => {
  const state = await installFixture(page, {
    session: 'anonymous',
    usernameAvailable: { available: false, reason: '该用户名已被占用，换一个吧。' },
  });
  await gotoLoginModal(page);

  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-username-input').fill('已被占用的名字');
  await page.getByTestId('auth-password-input').click(); // 触发 username 失焦

  await expect(page.getByTestId('auth-username-hint')).toContainText('已被占用');
  expect(state.availabilityCalls).toEqual(['已被占用的名字']);
  // 提交按钮仍然可用：占用与否最终由服务端裁决，前端提示不该变成硬拦截
  await expect(page.getByTestId('beta-invite-submit')).toBeDisabled(); // 因为邀请码/密码还没填
  expect(state.registerCalls).toEqual([]);
});

test('D: 未被注册的邀请码用「用邀请码进入」→ 原样告知，不得混进工作台', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous', inviteLogin: 'pending' });
  await gotoLoginModal(page);

  await page.getByTestId('auth-mode-invite').click();
  await page.getByTestId('beta-invite-input').fill(NEW_CODE);
  await page.getByTestId('beta-invite-submit').click();

  await expect(page.getByTestId('auth-error')).toContainText('还没被注册');
  // 弹层必须还在（没有回落到老通道混进去）
  await expect(page.getByTestId('beta-invite-submit')).toBeVisible();
  await expect(page.getByTestId('dashboard-page')).toHaveCount(0);
  expect(await page.evaluate((key) => sessionStorage.getItem(key), INVITE_KEY)).toBeNull();
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('E: 老 .env 码（服务端 401 + legacy_account_missing）回落到「邀请码即凭据」老通道', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous', inviteLogin: 'legacy-account-missing' });
  await gotoLoginModal(page);

  await page.getByTestId('auth-mode-invite').click();
  await page.getByTestId('beta-invite-input').fill(LEGACY_CODE);
  await page.getByTestId('beta-invite-submit').click();

  // 老码被存进 sessionStorage，之后所有请求都带 X-NovelCraft-Invite（改造前的行为）。
  // 注意：回落路径会整页 reload，必须先等 reload 完成再读 sessionStorage，
  // 否则 page.evaluate 会撞上「执行上下文已销毁」而随机失败。
  await expect(page.getByTestId('dashboard-page')).toBeVisible();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), INVITE_KEY)).toBe(LEGACY_CODE);
  await expect(page.getByTestId('auth-error')).toHaveCount(0);
  expect(state.loginCalls).toEqual([{ username: '', password: '', invite: LEGACY_CODE, termsAccepted: true, termsVersion: '2026-10-18.1' }]);
  expect(state.errors).toEqual([]);
});

test('F: 账户页「我的邀请码」只显示后 4 位，页面不含明文', async ({ page }) => {
  const state = await installFixture(page, {
    session: 'authenticated',
    myInvites: [{ code_hint: 'AB12', label: '10月批次', bound_at: '2026-10-07T01:00:00Z' }],
  });

  await page.goto('/account');
  await expect(page.getByTestId('account-invite-AB12')).toBeVisible();
  const text = (await page.locator('body').innerText()) || '';
  expect(text).toContain('AB12');
  expect(text).toContain('10月批次');
  expect(text).not.toContain(NEW_CODE);
  expect(text).not.toContain(LEGACY_CODE);
  // 文案要说清“为什么看不到完整码”
  expect(text).toContain('后 4 位');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);

  void MARKER;
  void state.myInviteCalls;
});

test('G: 邀请码写错时必须留在弹层，且绝不能被写进本地凭据（防止假登录）', async ({ page }) => {
  // 这条钉住的是设计审查里最严重的一个缺陷：以前任何 401 都被当成
  // "老 .env 码对应的账号不存在"，于是错码被写进 sessionStorage → 登录闸门被这个
  // 错码关掉 → 用户被放进创作台，而每个接口都 401（假登录状态）。
  const state = await installFixture(page, { session: 'anonymous', inviteLogin: 'invalid' });

  await gotoLoginModal(page);
  await page.getByTestId('auth-mode-invite').click();
  await page.getByTestId('beta-invite-input').fill('WRONG-CODE-1234');
  await page.getByTestId('beta-invite-submit').click();

  await expect(page.getByTestId('auth-error')).toContainText('邀请码无效');
  // 仍停在弹层：输入框还在、没有进工作区
  await expect(page.getByTestId('beta-invite-input')).toBeVisible();
  await expect(page.getByTestId('beta-invite-submit')).toBeVisible();
  // 登录闸门不可关闭：不该渲染一个点了没反应的"关闭"按钮（死控件）
  await expect(page.getByLabel('关闭')).toHaveCount(0);

  // 关键断言：错码没有被持久化
  expect(await page.evaluate((key) => sessionStorage.getItem(key), INVITE_KEY)).toBeNull();

  expect(state.loginCalls).toEqual([{ username: '', password: '', invite: 'WRONG-CODE-1234', termsAccepted: true, termsVersion: '2026-10-18.1' }]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('H: 注册模式在输入框里按回车 = 提交（真表单的隐式提交）', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  await gotoLoginModal(page);

  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('register-invite-input').fill(NEW_CODE);
  await page.getByTestId('auth-username-input').fill('回车作者');
  await page.getByTestId('auth-password-input').fill('strong-pass-1');
  const confirm = page.getByTestId('register-confirm-input');

  // 在最后一个输入框里按回车：浏览器应对表单做隐式提交
  await confirm.fill('strong-pass-1');
  await confirm.press('Enter');

  await expect.poll(() => state.registerCalls.length).toBe(1);
  expect(state.registerCalls[0]).toEqual({
    username: '回车作者',
    password: 'strong-pass-1',
    invite: NEW_CODE,
    termsAccepted: true,
    termsVersion: '2026-10-18.1',
  });
  // 整页 reload 之后工作区应当出现（自动登录）
  await expect(page.getByTestId('dashboard-page')).toBeVisible();
  // 关键：凭据绝不能因为表单提交而跑到 URL 上（method="post" 的防御）
  expect(page.url()).not.toContain('strong-pass-1');
  expect(state.errors).toEqual([]);
});

test('I: 登录模式在密码框里按回车 = 提交', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  await gotoLoginModal(page);

  await page.getByTestId('auth-username-input').fill('已注册作者');
  const password = page.getByTestId('auth-password-input');
  await password.fill('strong-pass-1');
  await password.press('Enter');

  await expect.poll(() => state.loginCalls.length).toBe(1);
  expect(state.loginCalls[0]).toEqual({ username: '已注册作者', password: 'strong-pass-1', termsAccepted: true, termsVersion: '2026-10-18.1' });
  await expect(page.getByTestId('dashboard-page')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('J: 登录模式逐字输入不能丢焦点（每次按键重渲染也不许抢走焦点）', async ({ page }) => {
  // 这条钉住一个**真实事故**：Modal 的 effect 曾经依赖 `onClose`，而调用方传的是内联箭头
  // 函数（每次渲染都是新身份）→ 每敲一个字 effect 就重跑一次"初始聚焦"，把焦点从输入框
  // 抢回第一个可聚焦元素，用户必须每打一个字就重新点一次输入框。
  // 用 keyboard.type 逐字输入才会暴露它 —— `fill()` 一次性设值，永远测不出来。
  const state = await installFixture(page, { session: 'anonymous' });
  await gotoLoginModal(page);

  const username = page.getByTestId('auth-username-input');
  await username.click();
  await page.keyboard.type('zhuzi-shuru');
  await expect(username).toBeFocused();
  await expect(username).toHaveValue('zhuzi-shuru');

  const password = page.getByTestId('auth-password-input');
  await password.click();
  await page.keyboard.type('strong-pass-1');
  await expect(password).toBeFocused();
  await expect(password).toHaveValue('strong-pass-1');

  expect(state.errors).toEqual([]);
});

test('K: 注册模式逐字输入同样不丢焦点（字段更多、重渲染更多）', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  await gotoLoginModal(page);

  await page.getByTestId('auth-mode-register').click();

  const invite = page.getByTestId('register-invite-input');
  await invite.click();
  await page.keyboard.type('TYPING-CODE-1');
  await expect(invite).toBeFocused();
  await expect(invite).toHaveValue('TYPING-CODE-1');

  const username = page.getByTestId('auth-username-input');
  await username.click();
  await page.keyboard.type('zhuzi-zhuce');
  await expect(username).toBeFocused();
  await expect(username).toHaveValue('zhuzi-zhuce');

  const password = page.getByTestId('auth-password-input');
  await password.click();
  await page.keyboard.type('strong-pass-1');
  await expect(password).toBeFocused();
  await expect(password).toHaveValue('strong-pass-1');

  const confirm = page.getByTestId('register-confirm-input');
  await confirm.click();
  await page.keyboard.type('strong-pass-1');
  await expect(confirm).toBeFocused();
  await expect(confirm).toHaveValue('strong-pass-1');

  expect(state.errors).toEqual([]);
});

test('条款门禁：未勾选时按钮禁用且不发请求；可读全文；勾选后请求体带 termsAccepted', async ({ page }) => {
  const state = await installFixture(page, { session: 'anonymous' });
  // 这一条**不能**用 gotoLoginModal（它会顺手勾选），要自己打开闸门看默认状态。
  await page.goto('/');
  await expect(page.getByTestId('auth-terms-checkbox')).toBeVisible();
  await expect(page.getByTestId('auth-terms-checkbox')).not.toBeChecked();

  // 字段填全，但只要没勾选，提交仍然禁用（勾选即视为同意，必须是明确动作）
  await page.getByTestId('auth-username-input').fill('已填好的账号');
  await page.getByTestId('auth-password-input').fill('strong-pass-1');
  await expect(page.getByTestId('beta-invite-submit')).toBeDisabled();

  // 条文在登录前就必须可读（全文 + 版本号），否则"同意"是空头支票。
  // 版本号必须按**当前**版本精确断言：写成旧版号会因为子串匹配而"永远绿"，
  // 那等于没钉住"用户看到的是哪一版"。
  await page.getByTestId('auth-terms-open').click();
  await expect(page.getByTestId('terms-dialog')).toContainText('禁止创作与传播的内容');
  await expect(page.getByTestId('terms-dialog')).toContainText('版本：2026-10-18.1');
  await page.getByTestId('terms-close').click();

  await acceptTerms(page);
  await expect(page.getByTestId('beta-invite-submit')).toBeEnabled();
  await page.getByTestId('beta-invite-submit').click();

  await expect.poll(() => state.loginCalls.length).toBe(1);
  expect(state.loginCalls[0]).toMatchObject({ username: '已填好的账号', termsAccepted: true, termsVersion: '2026-10-18.1' });
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});
