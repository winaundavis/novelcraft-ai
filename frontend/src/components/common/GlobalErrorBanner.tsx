import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, X } from 'lucide-react';

interface Report {
  id: number;
  source: string;
  message: string;
}

const MAX_REPORTS = 5;

/**
 * 全局错误横幅：把「只在控制台里」的失败也摆到界面上。
 *
 * 错误边界只能捕获**渲染期**异常。脚本加载失败、事件回调抛错、未处理的 Promise
 * 拒绝都不会触发边界，表现却同样是「界面莫名其妙不动了/空了」，而且作者不开
 * DevTools 就完全看不到原因。这里把这几类失败统一收拢成一条可读横幅。
 *
 * 只收带 message 的错误：资源 4xx/5xx 会以空 message 的 ErrorEvent 冒泡上来，
 * 那些属于预期内的失败（如某个未分析过的接口 404），不在这里打扰作者。
 */
export default function GlobalErrorBanner() {
  const [reports, setReports] = useState<Report[]>([]);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let seq = 0;

    const push = (source: string, message: string) => {
      if (!message) return;
      seq += 1;
      setReports((previous) => [...previous.slice(-(MAX_REPORTS - 1)), { id: seq, source, message }]);
      setDismissed(false);
    };

    const handleError = (event: ErrorEvent) => {
      const message = event.message || (event.error instanceof Error ? event.error.message : '');
      push('脚本错误', message);
    };

    const handleRejection = (event: PromiseRejectionEvent) => {
      const reason: unknown = event.reason;
      push('未处理的异步错误', reason instanceof Error ? reason.message : String(reason ?? ''));
    };

    window.addEventListener('error', handleError);
    window.addEventListener('unhandledrejection', handleRejection);
    return () => {
      window.removeEventListener('error', handleError);
      window.removeEventListener('unhandledrejection', handleRejection);
    };
  }, []);

  const handleDismiss = useCallback(() => setDismissed(true), []);

  if (!reports.length || dismissed) return null;

  return (
    <div
      className="fixed bottom-3 right-3 z-50 max-w-md rounded-xl border border-warning/40 bg-warning-soft p-3 text-xs shadow-lg dark:bg-warning-soft-dark"
      role="alert"
      data-testid="global-error-banner"
    >
      <div className="mb-1.5 flex items-start gap-2">
        <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warning" aria-hidden />
        <span className="font-semibold text-warning dark:text-warning-dark">
          界面出现了未预期的错误（已记录，可继续使用）
        </span>
        <button
          type="button"
          onClick={handleDismiss}
          className="ml-auto shrink-0 text-surface-400 hover:text-surface-600"
          aria-label="关闭错误提示"
          data-testid="global-error-dismiss"
        >
          <X size={14} />
        </button>
      </div>
      <ul className="space-y-1 pl-5">
        {reports.map((report) => (
          <li key={report.id} className="break-words text-surface-700 dark:text-surface-200">
            <span className="text-surface-400">[{report.source}]</span>{' '}
            <span data-testid="global-error-message">{report.message}</span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-surface-500">
        如果是操作到某一步才出现，把这里的文字发给维护者即可定位。
      </p>
    </div>
  );
}
