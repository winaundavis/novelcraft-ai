const INVITE_STORAGE_KEY = 'novelcraft-beta-invite';

export function getInviteToken(): string {
  return window.sessionStorage.getItem(INVITE_STORAGE_KEY)?.trim() || '';
}

export function setInviteToken(token: string): void {
  window.sessionStorage.setItem(INVITE_STORAGE_KEY, token.trim());
}

export function getInviteHeaders(): Record<string, string> {
  const token = getInviteToken();
  return token ? { 'X-NovelCraft-Invite': token } : {};
}

/** 清除本地邀请码（退出/切换账号时使用，避免遗留身份继续生效）。 */
export function clearInviteToken(): void {
  try {
    window.sessionStorage.removeItem(INVITE_STORAGE_KEY);
  } catch {
    // sessionStorage 不可用（隐私模式等）时静默忽略
  }
}
