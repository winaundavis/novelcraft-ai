/**
 * useNovelJobPoller
 *
 * 保持既有 hook 名称，内部实现改为 SSE 实时订阅，
 * 用于同步长篇创作任务进度而不再依赖固定间隔轮询。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { aiApi } from '../services/api';
import type { NovelJobPayload } from '../types';

interface UseNovelJobPollerReturn {
  job: NovelJobPayload | null;
  isPolling: boolean;
  error: string | null;
  startPolling: (jobId: string) => void;
  stopPolling: () => void;
}

export function useNovelJobPoller(): UseNovelJobPollerReturn {
  const [job, setJob] = useState<NovelJobPayload | null>(null);
  const [isPolling, setIsPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const jobIdRef = useRef<string | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  const stopPolling = useCallback(() => {
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
    jobIdRef.current = null;
    setIsPolling(false);
  }, []);

  const startPolling = useCallback((jobId: string) => {
    stopPolling();
    jobIdRef.current = jobId;
    setIsPolling(true);
    setError(null);

    unsubscribeRef.current = aiApi.novelJob.subscribe(jobId, {
      onMessage: (current) => {
        setJob(current);
        if (['completed', 'failed', 'cancelled'].includes(current.status)) {
          stopPolling();
        }
      },
      onError: async () => {
        if (!jobIdRef.current) return;
        try {
          const json = await aiApi.novelJob.get(jobIdRef.current);
          if (!json.data) return;
          setJob(json.data);
          if (['completed', 'failed', 'cancelled'].includes(json.data.status)) {
            stopPolling();
          }
        } catch (err) {
          setError(err instanceof Error ? err.message : '任务状态实时同步失败。');
        }
      },
    });
  }, [stopPolling]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  return { job, isPolling, error, startPolling, stopPolling };
}
