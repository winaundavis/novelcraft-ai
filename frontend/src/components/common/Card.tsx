import { forwardRef } from 'react';
import type { HTMLAttributes, KeyboardEvent } from 'react';
import { cn } from './cn';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** 可点击卡片：追加 hover 边框高亮与 cursor-pointer（配合 onClick 使用） */
  clickable?: boolean;
  /** 是否保留 .card 默认内边距（p-4）；设 false 后通过 className 自行控制 */
  padded?: boolean;
}

export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { clickable = false, padded = true, className, onClick, onKeyDown, ...rest },
  ref,
) {
  const handleKeyDown = clickable && onClick
    ? (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        // 键盘激活时复用 onClick（类型上 KeyboardEvent 与 MouseEvent 不兼容，做最小适配）
        (onClick as unknown as (event: KeyboardEvent<HTMLDivElement>) => void)(event);
      }
      onKeyDown?.(event);
    }
    : onKeyDown;

  return (
    <div
      ref={ref}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={onClick}
      onKeyDown={handleKeyDown}
      className={cn(
        'card',
        !padded && 'p-0',
        clickable && 'cursor-pointer transition-all hover:border-primary-300 hover:shadow-md dark:hover:border-primary-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500',
        className,
      )}
      {...rest}
    />
  );
});
