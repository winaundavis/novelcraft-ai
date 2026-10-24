import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';
import { cn } from './cn';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastItem {
  id: number;
  type: ToastType;
  title: string;
  message?: string;
}

export interface ToastContextValue {
  toast: (type: ToastType, title: string, message?: string) => void;
  success: (title: string, message?: string) => void;
  error: (title: string, message?: string) => void;
  warning: (title: string, message?: string) => void;
  info: (title: string, message?: string) => void;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const DEFAULT_DURATION_MS = 5000;

const TYPE_STYLES: Record<ToastType, { container: string; icon: ReactNode }> = {
  success: {
    container: 'border-success/40 bg-success-soft text-success dark:border-success/40 dark:bg-success-soft-dark dark:text-success-dark',
    icon: <CheckCircle2 size={18} className="shrink-0 text-success dark:text-success-dark" />,
  },
  error: {
    container: 'border-error/40 bg-error-soft text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark',
    icon: <XCircle size={18} className="shrink-0 text-error dark:text-error-dark" />,
  },
  warning: {
    container: 'border-warning/40 bg-warning-soft text-warning dark:border-warning/40 dark:bg-warning-soft-dark dark:text-warning-dark',
    icon: <AlertTriangle size={18} className="shrink-0 text-warning dark:text-warning-dark" />,
  },
  info: {
    container: 'border-info/40 bg-info-soft text-info dark:border-info/40 dark:bg-info-soft-dark dark:text-info-dark',
    icon: <Info size={18} className="shrink-0 text-info dark:text-info-dark" />,
  },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const idRef = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const push = useCallback((type: ToastType, title: string, message?: string) => {
    const id = ++idRef.current;
    setToasts((prev) => [...prev, { id, type, title, message }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((item) => item.id !== id));
    }, DEFAULT_DURATION_MS);
  }, []);

  const value = useMemo<ToastContextValue>(() => ({
    toast: push,
    success: (title, message) => push('success', title, message),
    error: (title, message) => push('error', title, message),
    warning: (title, message) => push('warning', title, message),
    info: (title, message) => push('info', title, message),
    dismiss,
  }), [push, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed right-4 top-4 z-[60] flex w-full max-w-sm flex-col gap-2"
        role="region"
        aria-live="polite"
        aria-label="通知"
      >
        {toasts.map((item) => (
          <div
            key={item.id}
            role="status"
            className={cn(
              'pointer-events-auto flex items-start gap-3 rounded-lg border px-4 py-3 text-sm shadow-lg animate-fade-in',
              TYPE_STYLES[item.type].container,
            )}
          >
            {TYPE_STYLES[item.type].icon}
            <div className="min-w-0 flex-1">
              <p className="font-semibold">{item.title}</p>
              {item.message && <p className="mt-0.5 text-xs leading-relaxed opacity-90">{item.message}</p>}
            </div>
            <button
              onClick={() => dismiss(item.id)}
              className="shrink-0 rounded p-1 transition-colors hover:bg-black/5 dark:hover:bg-white/10"
              aria-label="关闭提示"
              title="关闭提示"
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast 必须在 <ToastProvider> 内使用');
  }
  return context;
}
