import { useEffect, useState } from 'react';
import { Download, Loader2 } from 'lucide-react';

import { Button, Card, useToast } from '../components/common';
import { authApi } from '../services/api';
import { clearLocalIdentity } from '../services/session';
import { downloadStorageBackup } from '../storage/indexeddb';
import type { BoundInvite } from '../types';

type ActionState =
  | { kind: 'idle' }
  | { kind: 'busy' }
  | { kind: 'error'; message: string };

/** 把后端原始 UTC ISO 串换成中文可读的当地时间（原串放 title 里备查）。 */
function formatBoundAt(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function AccountPage() {
  const [username, setUsername] = useState('');
  const [invites, setInvites] = useState<BoundInvite[]>([]);
  // 加载中不能用 `[]` 冒充"没有绑定"，否则刚用邀请码注册的用户会先被吓一下。
  const [invitesLoading, setInvitesLoading] = useState(true);
  const [state, setState] = useState<ActionState>({ kind: 'idle' });
  const [isExporting, setIsExporting] = useState(false);
  const toast = useToast();

  useEffect(() => {
    authApi.me().then((r) => setUsername(r.data.username)).catch(() => undefined);
    // 已绑定的邀请码：只有 hint（库内只有哈希，不回显明文）。失败不影响页面其它部分。
    authApi
      .myInvites()
      .then((r) => setInvites(r.data || []))
      .catch(() => undefined)
      .finally(() => setInvitesLoading(false));
  }, []);

  /**
   * 导出**自己的**本地数据备份。
   *
   * 它原本在「模型设置」页；那个页面现在只对管理员可见，而这份数据是普通作者自己的，
   * 所以迁到账户页 —— 不能因为把管理入口收起来，就把用户导出自己数据的能力一并拿走。
   */
  const handleExportBackup = async () => {
    setIsExporting(true);
    try {
      await downloadStorageBackup();
      toast.success('备份已导出', '数据已下载为 JSON 文件，已自动过滤 API 密钥。');
    } catch {
      toast.error('导出失败', '未能读取本地存储数据。');
    } finally {
      setIsExporting(false);
    }
  };

  const logout = async () => {
    setState({ kind: 'busy' });
    try {
      await authApi.logout();
      // 服务端会话已撤销：清掉全部本地身份数据后再离开页面。
      clearLocalIdentity();
      window.location.href = '/';
    } catch (error) {
      // 请求失败不冒充"已退出登录"：保留本地状态并提示用户重试。
      setState({
        kind: 'error',
        message: `退出登录失败：${error instanceof Error ? error.message : '未知错误'}。服务端会话可能仍然有效，请检查网络后重试。`,
      });
    }
  };

  const remove = async () => {
    if (!window.confirm('注销账号不可恢复。若账号下仍有作品或回收站数据，注销会被拒绝；请先自行导出或删除作品。确定继续吗？')) return;
    setState({ kind: 'busy' });
    try {
      await authApi.deleteAccount();
      clearLocalIdentity();
      window.location.href = '/';
    } catch (error) {
      setState({
        kind: 'error',
        message: `注销失败：${error instanceof Error ? error.message : '未知错误'}`,
      });
    }
  };

  return (
    <div className="flex-1 overflow-auto">
      <div className="mx-auto max-w-3xl px-8 py-8">
        <h1 className="mb-2 text-2xl font-bold">账户</h1>
        <p className="mb-8 text-sm text-surface-500">管理当前登录账户。</p>
        <Card>
          <p className="text-sm text-surface-600">用户名：{username || '未登录'}</p>
          {state.kind === 'error' ? (
            <p role="alert" data-testid="account-action-error" className="mt-4 text-sm text-error">{state.message}</p>
          ) : null}
          <div className="mt-5 flex gap-3">
            <Button variant="secondary" onClick={logout} disabled={state.kind === 'busy'} data-testid="account-logout">
              {state.kind === 'busy' ? '处理中…' : '退出登录'}
            </Button>
            <Button variant="danger" onClick={remove} disabled={state.kind === 'busy'} data-testid="account-delete">
              注销账号
            </Button>
          </div>
        </Card>

        {/* 我的邀请码：码也是本账号的登录凭据（Q1=C），所以让作者知道“是哪一把”。 */}
        <Card className="mt-4">
          <h2 className="text-sm font-semibold">我的邀请码</h2>
          {invitesLoading ? (
            <p className="mt-2 text-xs text-surface-500" data-testid="account-invites-loading">
              正在读取…
            </p>
          ) : invites.length === 0 ? (
            <p className="mt-2 text-xs text-surface-500" data-testid="account-invites-empty">
              这个账号没有绑定邀请码（例如由管理员直接创建的账号）。
            </p>
          ) : (
            <ul className="mt-2 space-y-1.5">
              {invites.map((invite) => (
                <li
                  key={`${invite.codeHint}-${invite.boundAt}`}
                  className="text-xs text-surface-600"
                  data-testid={`account-invite-${invite.codeHint}`}
                >
                  ****{invite.codeHint}
                  {invite.label ? `（${invite.label}）` : ''}
                  {invite.boundAt ? ` · 绑定于 ${formatBoundAt(invite.boundAt)}` : ''}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-xs leading-relaxed text-surface-500">
            出于安全考虑，这里只显示邀请码的<strong>后 4 位</strong>：服务端只保存哈希、不保存明文，
            所以无法回显完整邀请码。手里那串码可以当登录凭据使用；忘了就用用户名 + 密码登录，
            或者联系邀请你的作者停用旧码、重发一个。
          </p>
        </Card>

        {/* 此处导出浏览器缓存；完整作品以服务器保存的版本为准。 */}
        <Card className="mt-4">
          <h2 className="text-sm font-semibold">我的数据</h2>
          <p className="mt-2 text-xs leading-relaxed text-surface-500">
            已保存的作品与对话保存在服务器，浏览器另有缓存与草稿。
            此处仅导出浏览器缓存，已过滤 API 密钥；它不包含完整服务器数据。
            请另行导出需要保留的作品正文。
          </p>
          <div className="mt-4">
            <Button
              variant="secondary"
              onClick={() => void handleExportBackup()}
              disabled={isExporting}
              data-testid="account-export-backup"
            >
              {isExporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
              导出数据备份
            </Button>
          </div>
        </Card>
      </div>
    </div>
  );
}
