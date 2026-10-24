/**
 * 轻量 classNames 工具：过滤 falsy 值并拼接为字符串。
 * 为避免引入 clsx / tailwind-merge 等新依赖，使用最简实现。
 */
export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(' ');
}
