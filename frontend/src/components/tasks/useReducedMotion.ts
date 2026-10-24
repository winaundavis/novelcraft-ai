/**
 * prefers-reduced-motion 检测 hook：减少动效偏好下禁用 spinner/脉冲/骨架动画。
 * 设计系统 §7.2-6：`prefers-reduced-motion` 下禁用 animate-spin/pulse/ping 与进度条过渡。
 */
import { useEffect, useState } from 'react';

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => (
    typeof window !== 'undefined'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  ));

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const mql = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = () => setReduced(mql.matches);
    mql.addEventListener?.('change', onChange);
    return () => mql.removeEventListener?.('change', onChange);
  }, []);

  return reduced;
}
