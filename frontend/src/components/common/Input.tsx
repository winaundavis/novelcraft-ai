import { forwardRef, useId } from 'react';
import type { InputHTMLAttributes } from 'react';
import { cn } from './cn';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** 显示为关联的 label（自动 htmlFor 关联，符合无障碍规范） */
  label?: string;
  /** 错误提示文案；出现时输入框边框变红、并标记 aria-invalid */
  error?: string;
  /** 错误提示的 data-testid（可选，便于既有端到端用例定位字段级错误） */
  errorTestId?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, error, errorTestId, id, className, ...rest },
  ref,
) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const errorId = error ? `${inputId}-error` : undefined;

  return (
    <div className="w-full">
      {label && (
        <label htmlFor={inputId} className="mb-1.5 block text-sm font-medium text-surface-800 dark:text-surface-200">
          {label}
        </label>
      )}
      <input
        ref={ref}
        id={inputId}
        aria-invalid={error ? true : undefined}
        aria-describedby={errorId}
        className={cn('input-field', error && 'border-error focus:ring-error', className)}
        {...rest}
      />
      {/* role=alert 让读屏播报字段级错误；aria-describedby 把提示与输入框关联起来。 */}
      {error && (
        <p id={errorId} role="alert" data-testid={errorTestId} className="mt-1 text-xs text-error">
          {error}
        </p>
      )}
    </div>
  );
});
