import type { HTMLAttributes } from 'react';
import { cn } from './cn';

export type BadgeVariant = 'neutral' | 'success' | 'warning' | 'error' | 'info';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  /**
   * 语义变体；不传时仅提供胶囊形状，颜色由 className 提供
   * （用于 Dashboard 项目状态等「数据分类色」场景）
   */
  variant?: BadgeVariant;
}

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  neutral: 'bg-surface-100 text-surface-500 dark:bg-surface-700 dark:text-surface-300',
  success: 'bg-success-soft text-success dark:bg-success-soft-dark dark:text-success-dark',
  warning: 'bg-warning-soft text-warning dark:bg-warning-soft-dark dark:text-warning-dark',
  error: 'bg-error-soft text-error dark:bg-error-soft-dark dark:text-error-dark',
  info: 'bg-primary-50 text-primary-700 dark:bg-primary-900/30 dark:text-primary-400',
};

export function Badge({ variant, className, ...rest }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium leading-none',
        variant && VARIANT_CLASSES[variant],
        className,
      )}
      {...rest}
    />
  );
}
