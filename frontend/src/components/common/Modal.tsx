import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import type { HTMLAttributes, ReactNode, RefObject } from 'react';
import { X } from 'lucide-react';
import { cn } from './cn';

export interface ModalProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title' | 'children' | 'className'> {
  open: boolean;
  onClose: () => void;
  /** 标题（可选）；传入后渲染标题栏与关闭按钮 */
  title?: ReactNode;
  /** 底部操作区（左次要右主要，如 [取消] [保存]） */
  footer?: ReactNode;
  /** sm：表单类（max-w-lg）；lg：列表/分析类（max-w-2xl） */
  width?: 'sm' | 'lg';
  className?: string;
  children?: ReactNode;
  /**
   * 是否允许用户关闭。默认 `true`。
   *
   * 置为 `false` 时**不渲染右上角关闭按钮**（避免出现“看得见但点了没反应”的死控件），
   * 且 Escape 与点击遮罩都不生效 —— 用于“必须在这里做完决定”的弹层（如登录）。
   */
  dismissible?: boolean;
  /** 打开时把焦点送到这里；不传则聚焦弹层内第一个可聚焦元素。 */
  initialFocusRef?: RefObject<HTMLElement>;
  /**
   * 打开期间把背景（`#root`）设为 `inert`，使 Tab 与读屏都不会跑到弹层后面。默认 `true`。
   *
   * 弹层通过 portal 挂在 `body` 下、不在 `#root` 内，所以自身不受影响。
   */
  inertBackground?: boolean;
}

const WIDTH_CLASSES: Record<NonNullable<ModalProps['width']>, string> = {
  sm: 'max-w-lg',
  lg: 'max-w-2xl',
};

/** 弹层内“可聚焦”的候选（排除 disabled 与 tabindex="-1"） */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusableWithin(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    // 只保留“真的可见”的：display:none / hidden 的元素不该进入 Tab 循环。
    (element) => element.offsetWidth > 0 || element.offsetHeight > 0 || element.getClientRects().length > 0,
  );
}

export function Modal({
  open,
  onClose,
  title,
  footer,
  width = 'sm',
  className,
  children,
  dismissible = true,
  initialFocusRef,
  inertBackground = true,
  ...rest
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  // 关掉弹层后要把焦点还给“打开它的那个元素”，否则键盘用户会被丢回页面开头。
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  // `onClose` / `dismissible` 每次渲染都可能是新身份（调用方习惯写内联箭头函数，
  // 例如 `onClose={() => undefined}`）。它们只能通过 ref 给事件处理器读取，
  // **绝不能进 effect 依赖** —— 否则每次按键都会重跑 effect 并把焦点抢走。
  const onCloseRef = useRef(onClose);
  const dismissibleRef = useRef(dismissible);
  useEffect(() => {
    onCloseRef.current = onClose;
    dismissibleRef.current = dismissible;
  });

  // 这个 effect 只在“打开状态”变化时跑一次：初始聚焦 / 背景 inert / 焦点还原。
  //
  // 依赖里**只能放 open**。任何其它依赖（尤其 onClose 这类每次渲染新建的函数）
  // 都会让它在每次输入时重跑，把焦点从输入框抢回第一个可聚焦元素，
  // 表现为“每打一个字就要重新点一下输入框”。
  // `initialFocusRef` / `inertBackground` 属于“打开那一刻”的配置，
  // 在打开时读一次即可（这也正是弹层的语义）。
  useEffect(() => {
    if (!open) return;

    // 只在"当前焦点不在弹层内"时才记下它。React StrictMode 在开发环境会双调用
    // effect：第一次已经把焦点送进弹层，若第二次再记录，还原时就会记成弹层内部元素
    // （关闭后焦点无处可还）。
    const activeOnOpen = document.activeElement as HTMLElement | null;
    if (activeOnOpen && !panelRef.current?.contains(activeOnOpen)) {
      previouslyFocusedRef.current = activeOnOpen;
    }

    // 背景 inert：Tab 与读屏都不该跑到弹层后面去。
    const root = inertBackground ? document.getElementById('root') : null;
    root?.setAttribute('inert', '');

    // 初始聚焦：优先调用方指定的元素，否则弹层里第一个可聚焦元素，最后兜底聚焦面板本身。
    const panel = panelRef.current;
    const preferred = initialFocusRef?.current;
    if (preferred) {
      preferred.focus();
    } else if (panel) {
      const [first] = focusableWithin(panel);
      if (first) {
        first.focus();
      } else {
        panel.focus();
      }
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (dismissibleRef.current) onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !panel) return;
      // 焦点陷阱：让 Tab / Shift+Tab 在弹层内部循环。
      const items = focusableWithin(panel);
      if (items.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey && (active === first || !panel.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      root?.removeAttribute('inert');
      const previous = previouslyFocusedRef.current;
      // 只在那个元素还在文档里时还原焦点，避免把焦点丢到已被卸载的节点上。
      if (previous && document.contains(previous)) previous.focus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={dismissible ? onClose : undefined}
      role="dialog"
      aria-modal="true"
      aria-label={typeof title === 'string' ? title : undefined}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        className={cn(
          'max-h-[85vh] w-full overflow-auto rounded-2xl bg-white shadow-2xl outline-none dark:bg-surface-800 animate-fade-in',
          WIDTH_CLASSES[width],
          className,
        )}
        onClick={(event) => event.stopPropagation()}
        {...rest}
      >
        {title && (
          <div className="flex items-start justify-between gap-4 border-b border-surface-200 px-6 pb-4 pt-5 dark:border-surface-700">
            <h2 className="text-xl font-bold text-surface-900 dark:text-surface-100">{title}</h2>
            {/* 不可关闭的弹层不渲染关闭按钮：一个点了没反应的“关闭”比没有更糟。 */}
            {dismissible && (
              <button
                type="button"
                onClick={onClose}
                className="btn-ghost shrink-0 p-1.5"
                aria-label="关闭"
                title="关闭"
              >
                <X size={18} />
              </button>
            )}
          </div>
        )}
        <div className="p-6">{children}</div>
        {footer && (
          <div className="flex justify-end gap-3 border-t border-surface-200 px-6 pb-5 pt-4 dark:border-surface-700">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
