import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

interface ErrorBoundaryProps {
  children: ReactNode;
  /** page：整页兜底（入口用）；panel：栏内兜底，保住左侧导航与项目列表。 */
  variant?: 'page' | 'panel';
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * 全局错误边界。
 *
 * React 在渲染期抛错时会直接卸载整棵树，没有边界时用户看到的是一片纯白：
 * 没有报错、没有入口、也没有任何线索，只能被理解为「前端打不开了」。
 * 这里兜住异常，给出可读原因和两个可执行的恢复动作。
 *
 * 注意：错误边界只能捕获**渲染期**异常，捕获不到事件回调与异步代码里的异常，
 * 所以它只是兜底，不能替代对具体崩溃点的修复。
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 完整堆栈只进控制台，界面上只给可读信息，避免把内部细节甩给作者。
    console.error('[ErrorBoundary] 渲染异常：', error, info.componentStack);
  }

  private handleRetry = () => {
    this.setState({ error: null });
  };

  private handleReload = () => {
    window.location.reload();
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    const isPanel = this.props.variant === 'panel';

    return (
      <div
        className={
          isPanel
            ? 'flex h-full items-center justify-center p-6'
            : 'flex min-h-screen items-center justify-center bg-surface-50 p-6 dark:bg-surface-900'
        }
        data-testid="app-error-boundary"
      >
        <div className="card w-full max-w-lg space-y-4 text-center">
          <AlertTriangle size={40} className="mx-auto text-warning" aria-hidden />
          <h1 className="text-lg font-semibold">界面出错了，已停止本次渲染</h1>
          <p className="text-sm text-surface-500 dark:text-surface-400">
            作品数据保存在本地数据库里，没有丢失。可以先「再试一次」，或重新载入页面继续写作。
          </p>
          <pre
            className="max-h-40 overflow-auto rounded-lg bg-surface-100 p-3 text-left text-xs text-surface-600 dark:bg-surface-800 dark:text-surface-300"
            data-testid="app-error-boundary-message"
          >
            {error.message || String(error)}
          </pre>
          <div className="flex justify-center gap-2">
            <button
              type="button"
              className="btn-secondary text-sm"
              onClick={this.handleRetry}
              data-testid="app-error-boundary-retry"
            >
              再试一次
            </button>
            <button
              type="button"
              className="btn-primary inline-flex items-center gap-1.5 text-sm"
              onClick={this.handleReload}
              data-testid="app-error-boundary-reload"
            >
              <RefreshCw size={14} aria-hidden />
              重新载入
            </button>
          </div>
        </div>
      </div>
    );
  }
}
