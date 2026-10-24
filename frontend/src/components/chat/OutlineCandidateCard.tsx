import { useState } from 'react';
import type { OutlineCandidate } from '../../types';
import { summarizeOutlineDiff } from './outlineCandidateDiff';

export default function OutlineCandidateCard({ candidate, onApply, onReject }: { candidate: OutlineCandidate; onApply: (confirmed?: boolean) => void; onReject: () => void }) {
  const [confirmingRemoval, setConfirmingRemoval] = useState(false);
  const diff = candidate.diff || {};
  const sections = summarizeOutlineDiff(diff);
  const count = sections.reduce((total, section) => total + section.count, 0);
  return (
    <div className="rounded-lg border border-primary-200 bg-primary-50 p-3 text-sm" data-testid="outline-candidate">
      <div className="font-semibold">大纲修改候选</div>
      <div className="mt-1 text-xs text-surface-600">共 {count} 项结构变化；原大纲尚未修改</div>
      <div className="mt-2 space-y-2" data-testid="outline-candidate-diff">
        {sections.map((section) => (
          <details key={section.key} open={section.key === 'added' || section.key === 'removed'}>
            <summary className="cursor-pointer text-xs font-medium text-surface-700 dark:text-surface-200">
              {section.label}（{section.count}）
            </summary>
            <ul className="mt-1 max-h-40 list-disc space-y-0.5 overflow-auto pl-5 text-xs text-surface-600 dark:text-surface-300">
              {section.items.map((item, index) => <li key={`${section.key}-${index}`}>{item}</li>)}
            </ul>
          </details>
        ))}
      </div>
      {candidate.requiresConfirmation && (candidate.protectedRemovals?.length || 0) > 0 && (
        <div className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-900/20 dark:text-amber-100">
          <div className="font-semibold">此修改会从大纲中移除已有正文的章节</div>
          <div className="mt-1">这些章节的原稿会移入章节回收站，可从章节导航中的回收站预览和恢复：</div>
          <ul className="mt-1 list-disc pl-5">
            {candidate.protectedRemovals?.map((item) => <li key={item.id}>{item.title || item.id}</li>)}
          </ul>
        </div>
      )}
      <div className="mt-2 flex gap-2">
        {!confirmingRemoval ? (
          <button
            className="btn-primary px-2 py-1 text-xs"
            onClick={() => {
              if (candidate.requiresConfirmation) setConfirmingRemoval(true);
              else onApply(false);
            }}
          >应用修改</button>
        ) : (
          <>
            <button className="btn-primary bg-amber-600 px-2 py-1 text-xs hover:bg-amber-700" onClick={() => onApply(true)}>确认移除并应用</button>
            <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setConfirmingRemoval(false)}>取消</button>
          </>
        )}
        <button className="btn-ghost px-2 py-1 text-xs" onClick={onReject}>拒绝</button>
      </div>
    </div>
  );
}
