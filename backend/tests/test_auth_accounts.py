from fastapi.testclient import TestClient
from app.core.config import settings
from app.core.terms import TERMS_VERSION
from app.dao import invites as invites_dao
from main import app

_TERMS = {'termsAccepted': True, 'termsVersion': TERMS_VERSION}


def _mint_invite() -> str:
    """取一个库里的一次性邀请码。

    注册现在**只认库里的 pending 码**（PRD 3.1/3.2）；老 `.env` 码保留的是
    `X-NovelCraft-Invite` 免密码登录通道，不再能用于注册。
    """
    return invites_dao.generate_invites(1, 'pytest-auth-accounts', 'pytest')[0]


def test_register_login_cookie_and_logout(monkeypatch):
    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    with TestClient(app) as client:
        created = client.post('/api/auth/register', json={'username':'alice','password':'strong-pass-1','invite':_mint_invite(), **_TERMS})
        assert created.status_code == 200
        assert 'password' not in created.text and 'hash' not in created.text
        login = client.post('/api/auth/login', json={'username':'alice','password':'strong-pass-1', **_TERMS})
        assert login.status_code == 200
        assert 'novelcraft_session' in login.cookies
        me = client.get('/api/auth/me')
        assert me.status_code == 200 and me.json()['data']['username'] == 'alice'
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/auth/me').status_code == 401

def test_invalid_login_and_invite(monkeypatch):
    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    with TestClient(app) as client:
        assert client.post('/api/auth/register', json={'username':'bob','password':'strong-pass-1','invite':'wrong'}).status_code == 403
        assert client.post('/api/auth/register', json={'username':'bob','password':'strong-pass-1','invite':_mint_invite(), **_TERMS}).status_code == 200
        assert client.post('/api/auth/login', json={'username':'bob','password':'wrong-pass'}).status_code == 401


# ── T1：退出登录 / 失效身份边界 ──────────────────────────────

def test_me_without_session_is_401_not_500(monkeypatch):
    """无 cookie 时 /auth/me 必须返回明确的 401（前端据此进入未认证状态）。"""
    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    with TestClient(app) as client:
        response = client.get('/api/auth/me')
        assert response.status_code == 401


def test_logout_revokes_session_server_side(monkeypatch):
    """logout 成功后服务端会话被撤销，旧 cookie 不能继续访问 /auth/me。"""
    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    with TestClient(app) as client:
        client.post('/api/auth/register', json={'username':'carol','password':'strong-pass-1','invite':_mint_invite(), **_TERMS})
        client.post('/api/auth/login', json={'username':'carol','password':'strong-pass-1', **_TERMS})
        assert client.get('/api/auth/me').status_code == 200
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/auth/me').status_code == 401
        # 幂等：再次 logout 不应 5xx
        assert client.post('/api/auth/logout').status_code == 200


def test_expired_session_is_rejected(monkeypatch):
    """过期会话必须被拒绝，前端才能识别为失效身份。"""
    from app.db import get_conn

    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    with TestClient(app) as client:
        client.post('/api/auth/register', json={'username':'dave','password':'strong-pass-1','invite':_mint_invite(), **_TERMS})
        client.post('/api/auth/login', json={'username':'dave','password':'strong-pass-1', **_TERMS})
        assert client.get('/api/auth/me').status_code == 200
        with get_conn() as conn:
            conn.execute("UPDATE user_sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
        assert client.get('/api/auth/me').status_code == 401


def test_legacy_invite_still_authenticates_without_session(monkeypatch):
    """受控旧 invite 兼容方案必须保留：无 cookie 会话时邀请码仍是有效凭据。"""
    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    with TestClient(app) as client:
        headers = {'X-NovelCraft-Invite': 'invite-1'}
        created = client.post('/api/projects/', json={'name': 'invite-owner-project'}, headers=headers)
        assert created.status_code == 200
        project_id = created.json()['data']['id']
        # 同一项目的 owner 校验仍按邀请码解析出的 user-a 生效
        assert client.get(f'/api/projects/{project_id}', headers=headers).status_code == 200
        # 无凭据的请求不得访问该项目（401，而不是 200）
        assert client.get(f'/api/projects/{project_id}').status_code == 401


# ── T3：注销账号的数据政策 ──────────────────────────────────

def _register_login(client, username: str) -> None:
    client.post(
        '/api/auth/register',
        json={'username': username, 'password': 'strong-pass-1', 'invite': _mint_invite(), **_TERMS},
    )
    client.post('/api/auth/login', json={'username': username, 'password': 'strong-pass-1', **_TERMS})


def _relax_rate_limit(monkeypatch) -> None:
    """同一测试文件请求较多，放宽限流以免 429 干扰用例语义。"""
    monkeypatch.setattr(settings, 'rate_limit', 10000)


def test_delete_account_refused_when_owner_has_projects(monkeypatch):
    """有自有作品的账号必须拒绝注销，且数据与登录态都不变。"""
    from app.db import get_conn

    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    _relax_rate_limit(monkeypatch)
    with TestClient(app) as client:
        _register_login(client, 'owner-eve')
        created = client.post('/api/projects/', json={'name': 'eve-owned-project'})
        assert created.status_code == 200
        project_id = created.json()['data']['id']

        refused = client.delete('/api/auth/account')
        assert refused.status_code == 409
        detail = refused.json()['detail']
        assert detail['code'] == 'account_has_owned_projects'
        assert detail['projects'] == 1
        assert '无法注销' in detail['message']

        # 登录态仍然有效，作品仍在
        assert client.get('/api/auth/me').status_code == 200
        assert client.get(f'/api/projects/{project_id}').status_code == 200
        with get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM projects WHERE id = ?", (project_id,)).fetchone()[0] == 1


def test_delete_account_allowed_when_no_owned_projects(monkeypatch):
    """没有作品的账号可以正常注销，并撤销全部会话。"""
    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    _relax_rate_limit(monkeypatch)
    with TestClient(app) as client:
        _register_login(client, 'empty-frank')
        assert client.get('/api/auth/me').status_code == 200
        assert client.delete('/api/auth/account').status_code == 200
        assert client.get('/api/auth/me').status_code == 401


def test_admin_delete_count_ignores_projects_owned_by_others(monkeypatch):
    """管理员的"全项目访问"不算拥有：统计只按 owner_id，不会误拦管理员注销。"""
    monkeypatch.setattr(settings, 'debug', False)
    monkeypatch.setattr(settings, 'invite_tokens', 'invite-1:user-a')
    monkeypatch.setattr(settings, 'admin_user_ids', 'admin-grace')
    _relax_rate_limit(monkeypatch)
    with TestClient(app) as client:
        # 另一个账号创建作品
        _register_login(client, 'owner-a')
        assert client.post('/api/projects/', json={'name': 'owned-by-a'}).status_code == 200
        client.post('/api/auth/logout')

        # 管理员登录（admin_user_ids 白名单，无自有作品）
        _register_login(client, 'admin-grace')
        assert client.delete('/api/auth/account').status_code == 200
        assert client.get('/api/auth/me').status_code == 401
