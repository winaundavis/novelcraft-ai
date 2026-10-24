/**
 * 运营后台（仅管理员）。
 *
 * 设计原则：
 * - **只看数字，不看内容**：不展示任何作品正文，避免隐私风险。
 * - 只有两个写操作：生成邀请码、停用邀请码。
 * - 页面本身不做权限判断——服务端 `require_maintainer` 才是唯一防线，
 *   路由层再用 `RequireAdmin` 做体验层面的遮挡。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  HardDrive,
  RefreshCw,
  ShieldAlert,
  Timer,
  Users,
} from 'lucide-react';

import {
  adminApi,
  type AdminInviteRow,
  type AdminOverview,
  type AdminUsage,
  type AdminUserRow,
} from '../../services/api';

const fmtInt = (value: number): string => new Intl.NumberFormat('zh-CN').format(Math.round(value || 0));

function fmtBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function fmtUptime(seconds: number): string {
  const total = Math.max(0, Math.round(seconds || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

/** 复制到剪贴板；失败时返回 false，由调用方给出可复制的文本框。 */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** 生成可直接发给朋友的邀请话术（含注册地址与有效期）。 */
function buildInviteMessage(codes: string[], maxUses: number, expiresAt: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  const expiry = expiresAt ? expiresAt.slice(0, 10) : '';
  const lines = ['你好，邀请你试用 NovelCraft AI 小说创作工具：', `地址：${origin}`];
  if (codes.length === 1) {
    lines.push(`邀请码：${codes[0]}`);
    lines.push(`（可用 ${maxUses} 次，${expiry} 前有效）`);
  } else {
    lines.push('邀请码（一人一个）：');
    codes.forEach((code) => lines.push(`  ${code}`));
    lines.push(`（每个可用 ${maxUses} 次，${expiry} 前有效）`);
  }
  return lines.join('\n');
}

const STATUS_LABELS: Record<string, string> = {
  pending: '未使用',
  bound: '已绑定',
  disabled: '已停用',
};

function Card({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-surface-200 bg-white p-4 shadow-sm dark:border-surface-700 dark:bg-surface-800">
      <h2 className="flex items-center gap-2 text-sm font-semibold text-surface-700 dark:text-surface-200">
        {icon}
        {title}
      </h2>
      <div className="mt-3 text-sm text-surface-600 dark:text-surface-300">{children}</div>
    </section>
  );
}

export default function AdminConsole() {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [users, setUsers] = useState<AdminUserRow[]>([]);
  const [usage, setUsage] = useState<AdminUsage | null>(null);
  const [invites, setInvites] = useState<AdminInviteRow[]>([]);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const [label, setLabel] = useState('');
  const [count, setCount] = useState(1);
  const [maxUses, setMaxUses] = useState(1);
  const [expiresInDays, setExpiresInDays] = useState(7);
  const [createdCodes, setCreatedCodes] = useState<string[]>([]);
  const [createdExpiresAt, setCreatedExpiresAt] = useState('');
  const [inviteFilter, setInviteFilter] = useState<'all' | 'pending' | 'bound' | 'disabled'>('all');
  const createdRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [overviewJson, usersJson, usageJson, invitesJson] = await Promise.all([
        adminApi.overview(),
        adminApi.users(days),
        adminApi.usage(days),
        adminApi.invites(),
      ]);
      setOverview(overviewJson.data ?? null);
      setUsers(usersJson.data?.users ?? []);
      setUsage(usageJson.data ?? null);
      setInvites(invitesJson.data?.invites ?? []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '后台数据加载失败。');
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  // 明文只显示一次：生成后必须把结果块滚到视野里，否则用户会以为“没生成”。
  useEffect(() => {
    if (createdCodes.length > 0) {
      createdRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [createdCodes]);

  const handleCreateInvite = async () => {
    setBusy(true);
    setError('');
    setCreatedCodes([]);
    try {
      const json = await adminApi.createInvite({ label: label.trim(), count, maxUses, expiresInDays });
      const codes = json.data?.codes ?? [];
      setCreatedCodes(codes);
      setCreatedExpiresAt(json.data?.expiresAt ?? '');
      if (codes.length === 0) {
        // 兜底：宁可显式报错，也不能让人以为"点了没反应"。
        setError('生成成功但未拿到明文，请刷新页面后重试。');
      } else {
        setNotice(`已生成 ${codes.length} 个邀请码。`);
      }
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '邀请码生成失败。');
    } finally {
      setBusy(false);
    }
  };

  const handleCopy = async (text: string, okMessage: string) => {
    const ok = await copyText(text);
    if (ok) setNotice(okMessage);
    else setError('复制失败，请手动选中文本复制。');
  };

  const handleDisable = async (invite: AdminInviteRow) => {
    setBusy(true);
    setError('');
    try {
      await adminApi.disableInvite(invite.id);
      setNotice(`已停用邀请码 ****${invite.hint}`);
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '停用失败。');
    } finally {
      setBusy(false);
    }
  };

  const readiness = overview?.readiness;
  const visibleInvites = invites.filter((row) => inviteFilter === 'all' || row.status === inviteFilter);

  return (
    <div className="flex-1 overflow-auto px-6 py-5" data-testid="admin-console">
      <div className="mx-auto flex max-w-5xl flex-col gap-4">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold text-surface-800 dark:text-surface-100">运营后台</h1>
            <p className="mt-0.5 text-xs text-surface-500">
              只展示统计数字，不展示任何作品内容。所有接口仅管理员可访问。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={days}
              onChange={(event) => setDays(Number(event.target.value))}
              className="rounded border border-surface-300 bg-white px-2 py-1 text-xs dark:border-surface-600 dark:bg-surface-700"
              data-testid="admin-days"
            >
              <option value={1}>今日</option>
              <option value={7}>近 7 天</option>
              <option value={30}>近 30 天</option>
            </select>
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading}
              className="flex items-center gap-1 rounded border border-surface-300 px-2 py-1 text-xs hover:bg-surface-100 disabled:opacity-50 dark:border-surface-600 dark:hover:bg-surface-700"
              data-testid="admin-refresh"
            >
              <RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> 刷新
            </button>
          </div>
        </header>

        {error && (
          <div className="flex items-start gap-2 rounded border border-error/40 bg-error-soft px-3 py-2 text-xs text-error" data-testid="admin-error">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" /> {error}
          </div>
        )}
        {notice && (
          <div className="rounded border border-success/40 bg-success-soft px-3 py-2 text-xs text-success" data-testid="admin-notice">
            {notice}
          </div>
        )}

        <div className="grid gap-4 md:grid-cols-2">
          <Card title="系统就绪状态" icon={<CheckCircle2 size={14} />}>
            {readiness ? (
              <div className="space-y-1">
                <p className="font-medium">
                  {readiness.ready ? (
                    <span className="text-success">已就绪（/api/ready = 200）</span>
                  ) : (
                    <span className="text-error">未就绪</span>
                  )}
                </p>
                {readiness.errors.length > 0 && (
                  <ul className="list-inside list-disc text-xs text-error" data-testid="admin-readiness-errors">
                    {readiness.errors.map((code) => (
                      <li key={code}>{code}</li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <p className="text-surface-400">加载中…</p>
            )}
          </Card>

          <Card title="运行与备份" icon={<Timer size={14} />}>
            <dl className="grid grid-cols-2 gap-y-1 text-xs">
              <dt className="text-surface-500">运行时长</dt>
              <dd>{overview ? fmtUptime(overview.uptimeSeconds) : '-'}</dd>
              <dt className="text-surface-500">服务器时间</dt>
              <dd className="break-all">{overview?.serverTimeUtc || '-'}</dd>
              <dt className="text-surface-500">最近备份</dt>
              <dd>{overview?.backup?.lastBackupAt ? String(overview.backup.lastBackupAt) : '暂无记录'}</dd>
              <dt className="text-surface-500">备份份数</dt>
              <dd>{overview?.backup?.retained !== undefined ? `${overview.backup.retained}` : '-'}</dd>
              <dt className="text-surface-500">最新备份大小</dt>
              <dd>{overview?.backup?.latestSizeBytes ? fmtBytes(Number(overview.backup.latestSizeBytes)) : '-'}</dd>
            </dl>
          </Card>

          <Card title="磁盘与数据规模" icon={<HardDrive size={14} />}>
            <dl className="grid grid-cols-2 gap-y-1 text-xs">
              <dt className="text-surface-500">磁盘使用</dt>
              <dd>{overview?.disk ? `${overview.disk.usedPercent}%（剩余 ${fmtBytes(overview.disk.freeBytes)}）` : '-'}</dd>
              <dt className="text-surface-500">用户</dt>
              <dd>{fmtInt(overview?.counts?.users ?? 0)}</dd>
              <dt className="text-surface-500">项目</dt>
              <dd>{fmtInt(overview?.counts?.projects ?? 0)}</dd>
              <dt className="text-surface-500">章节</dt>
              <dd>{fmtInt(overview?.counts?.chapters ?? 0)}</dd>
              <dt className="text-surface-500">可用邀请码</dt>
              <dd>{fmtInt(overview?.counts?.invitesPending ?? 0)} 未使用 / {fmtInt(overview?.counts?.invitesBound ?? 0)} 已绑定</dd>
            </dl>
          </Card>

          <Card title={`调用量（近 ${usage?.days ?? days} 天）`} icon={<Database size={14} />}>
            <p className="text-xs">
              合计 <strong>{fmtInt(usage?.totals.calls ?? 0)}</strong> 次调用，
              <strong> {fmtInt(usage?.totals.tokens ?? 0)}</strong> tokens
            </p>
            <div className="mt-2 max-h-40 overflow-auto">
              <table className="w-full text-left text-xs" data-testid="admin-usage-days">
                <thead className="text-surface-500">
                  <tr>
                    <th className="py-1">日期(UTC)</th>
                    <th className="py-1 text-right">次数</th>
                    <th className="py-1 text-right">Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {(usage?.byDay ?? []).slice().reverse().map((row) => (
                    <tr key={row.date} className="border-t border-surface-100 dark:border-surface-700">
                      <td className="py-1">{row.date}</td>
                      <td className="py-1 text-right">{fmtInt(row.calls)}</td>
                      <td className="py-1 text-right">{fmtInt(row.tokens)}</td>
                    </tr>
                  ))}
                  {(usage?.byDay ?? []).length === 0 && (
                    <tr>
                      <td colSpan={3} className="py-2 text-surface-400">该区间还没有调用记录</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        </div>

        <Card title="用户列表" icon={<Users size={14} />}>
          <div className="overflow-auto">
            <table className="w-full text-left text-xs" data-testid="admin-users">
              <thead className="text-surface-500">
                <tr>
                  <th className="py-1">用户名</th>
                  <th className="py-1">角色</th>
                  <th className="py-1 text-right">项目</th>
                  <th className="py-1 text-right">章节</th>
                  <th className="py-1 text-right">今日次数</th>
                  <th className="py-1 text-right">今日 tokens</th>
                  <th className="py-1 text-right">近 N 天次数</th>
                  <th className="py-1 text-right">近 N 天 tokens</th>
                  <th className="py-1">注册时间</th>
                </tr>
              </thead>
              <tbody>
                {users.map((row) => (
                  <tr key={row.id} className="border-t border-surface-100 dark:border-surface-700">
                    <td className="py-1">{row.username}</td>
                    <td className="py-1">{row.isAdmin ? '管理员' : '普通用户'}</td>
                    <td className="py-1 text-right">{fmtInt(row.projectCount)}</td>
                    <td className="py-1 text-right">{fmtInt(row.chapterCount)}</td>
                    <td className="py-1 text-right">{fmtInt(row.todayCalls)}</td>
                    <td className="py-1 text-right">{fmtInt(row.todayTokens)}</td>
                    <td className="py-1 text-right">{fmtInt(row.windowCalls)}</td>
                    <td className="py-1 text-right">{fmtInt(row.windowTokens)}</td>
                    <td className="py-1 whitespace-nowrap">{row.createdAt?.slice(0, 10)}</td>
                  </tr>
                ))}
                {users.length === 0 && (
                  <tr>
                    <td colSpan={9} className="py-2 text-surface-400">暂无用户</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="项目用量排行（Top 10）" icon={<Database size={14} />}>
          <table className="w-full text-left text-xs" data-testid="admin-usage-projects">
            <thead className="text-surface-500">
              <tr>
                <th className="py-1">项目</th>
                <th className="py-1">归属用户</th>
                <th className="py-1 text-right">次数</th>
                <th className="py-1 text-right">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {(usage?.byProject ?? []).map((row) => (
                <tr key={row.projectId} className="border-t border-surface-100 dark:border-surface-700">
                  <td className="py-1">{row.name || row.projectId}</td>
                  <td className="py-1">{row.ownerId || '-'}</td>
                  <td className="py-1 text-right">{fmtInt(row.calls)}</td>
                  <td className="py-1 text-right">{fmtInt(row.tokens)}</td>
                </tr>
              ))}
              {(usage?.byProject ?? []).length === 0 && (
                <tr>
                  <td colSpan={4} className="py-2 text-surface-400">该区间还没有项目用量</td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>

        <div id="admin-invites-section" />
        <Card title="邀请码" icon={<ShieldAlert size={14} />}>
          <div className="rounded border border-warning/40 bg-warning-soft px-3 py-2 text-xs text-warning">
            明文只显示一次，库里只存哈希。生成后请立即复制；丢了只能停用重发。
          </div>

          <div className="mt-2 flex items-center gap-2 text-xs">
            <span className="text-surface-500">筛选</span>
            <select
              value={inviteFilter}
              onChange={(event) => setInviteFilter(event.target.value as 'all' | 'pending' | 'bound' | 'disabled')}
              className="rounded border border-surface-300 bg-white px-2 py-1 dark:border-surface-600 dark:bg-surface-700"
              data-testid="admin-invite-filter"
            >
              <option value="all">全部</option>
              <option value="pending">未使用</option>
              <option value="bound">已绑定</option>
              <option value="disabled">已停用</option>
            </select>
          </div>

          <div className="mt-3 flex flex-wrap items-end gap-2 text-xs">
            <label className="flex flex-col gap-1">
              <span className="text-surface-500">备注</span>
              <input
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                placeholder="如：10月测试批"
                className="w-40 rounded border border-surface-300 px-2 py-1 dark:border-surface-600 dark:bg-surface-700"
                data-testid="admin-invite-label"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-surface-500">数量</span>
              <input
                type="number"
                min={1}
                max={20}
                value={count}
                onChange={(event) => setCount(Number(event.target.value))}
                className="w-20 rounded border border-surface-300 px-2 py-1 dark:border-surface-600 dark:bg-surface-700"
                data-testid="admin-invite-count"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-surface-500">可用次数</span>
              <input
                type="number"
                min={1}
                max={100}
                value={maxUses}
                onChange={(event) => setMaxUses(Number(event.target.value))}
                className="w-20 rounded border border-surface-300 px-2 py-1 dark:border-surface-600 dark:bg-surface-700"
                data-testid="admin-invite-uses"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-surface-500">有效天数</span>
              <input
                type="number"
                min={1}
                max={365}
                value={expiresInDays}
                onChange={(event) => setExpiresInDays(Number(event.target.value))}
                className="w-20 rounded border border-surface-300 px-2 py-1 dark:border-surface-600 dark:bg-surface-700"
                data-testid="admin-invite-days"
              />
            </label>
            <button
              type="button"
              onClick={() => void handleCreateInvite()}
              disabled={busy}
              className="rounded bg-primary-600 px-3 py-1.5 font-medium text-white hover:bg-primary-700 disabled:opacity-50"
              data-testid="admin-invite-create"
            >
              生成邀请码
            </button>
          </div>

          {createdCodes.length > 0 && (
            <div ref={createdRef} className="mt-3 rounded border border-success/40 bg-success-soft px-3 py-2" data-testid="admin-invite-code">
              <p className="text-xs text-surface-500">
                新邀请码（只显示这一次，共 {createdCodes.length} 个）：
              </p>
              <ul className="mt-1 space-y-0.5">
                {createdCodes.map((code) => (
                  <li
                    key={code}
                    className="select-all font-mono text-base font-semibold tracking-wider text-surface-800 dark:text-surface-100"
                  >
                    {code}
                  </li>
                ))}
              </ul>
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void handleCopy(createdCodes.join('\n'), '已复制全部邀请码')}
                  className="rounded bg-primary-600 px-2 py-1 text-xs text-white hover:bg-primary-700"
                  data-testid="admin-copy-codes"
                >
                  复制全部邀请码
                </button>
                <button
                  type="button"
                  onClick={() =>
                    void handleCopy(
                      buildInviteMessage(createdCodes, maxUses, createdExpiresAt),
                      '已复制发送话术',
                    )
                  }
                  className="rounded border border-primary-500 px-2 py-1 text-xs text-primary-700 hover:bg-primary-50 dark:text-primary-300 dark:hover:bg-surface-700"
                  data-testid="admin-copy-message"
                >
                  复制发送话术（含地址）
                </button>
              </div>
            </div>
          )}

          <div className="mt-3 overflow-auto">
            <table className="w-full text-left text-xs" data-testid="admin-invites">
              <thead className="text-surface-500">
                <tr>
                  <th className="py-1">标识</th>
                  <th className="py-1">备注</th>
                  <th className="py-1">状态</th>
                  <th className="py-1 text-right">已用/上限</th>
                  <th className="py-1">有效期</th>
                  <th className="py-1">绑定账号</th>
                  <th className="py-1">操作</th>
                </tr>
              </thead>
              <tbody>
                {visibleInvites.map((row) => (
                  <tr key={row.id} className="border-t border-surface-100 dark:border-surface-700">
                    <td className="py-1 font-mono">****{row.hint}</td>
                    <td className="py-1">{row.label || '-'}</td>
                    <td className="py-1">{STATUS_LABELS[row.status] || row.status}</td>
                    <td className="py-1 text-right">{row.usedCount}/{row.maxUses}</td>
                    <td className="py-1 whitespace-nowrap">{row.expiresAt?.slice(0, 10) || '永不'}</td>
                    <td className="py-1">{row.boundUsername || row.boundUserId || '-'}</td>
                    <td className="py-1">
                      {row.status === 'disabled' ? (
                        <span className="text-surface-400">—</span>
                      ) : (
                        <button
                          type="button"
                          onClick={() => void handleDisable(row)}
                          disabled={busy}
                          className="rounded border border-error/50 px-2 py-0.5 text-error hover:bg-error-soft disabled:opacity-50"
                        >
                          停用
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {visibleInvites.length === 0 && (
                  <tr>
                    <td colSpan={7} className="py-2 text-surface-400">还没有邀请码</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  );
}
