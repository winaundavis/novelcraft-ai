import { useMemo, useState } from 'react';
import {
  CheckCircle,
  Clock,
  FileText,
  ListChecks,
  Loader2,
  Play,
  Sparkles,
  Trash2,
  Upload,
  X,
} from 'lucide-react';

import { importApi } from '../../services/api';
import { useEditorStore } from '../../stores';
import { matchesEditorSnapshot } from '../editor/editorResultGuard';
import {
  beginWordRun,
  changeWordFlow,
  hasWordProgress,
  isCurrentWordRun,
  resetWordFlow,
  useWordFlow,
  wordFlowSnapshot,
} from './importStore';

const ACTION_LABELS: Record<string, string> = {
  add: '新增',
  modify: '修改',
  delete: '删除',
  enhance: '增强',
  compress: '压缩',
  restructure: '重构',
  style: '风格调整',
  character: '角色修正',
};

const PRIORITY_COLORS: Record<string, string> = {
  high: 'text-error bg-error-soft dark:bg-error-soft-dark',
  medium: 'text-warning bg-warning-soft dark:bg-warning-soft-dark',
  low: 'text-info bg-info-soft dark:bg-info-soft-dark',
};

const PRIORITY_LABELS: Record<string, string> = {
  high: '高优先级',
  medium: '中优先级',
  low: '低优先级',
};

export default function WordImport({ projectId }: { projectId: string }) {
  // 与 Markdown 导入同理：状态存在 store 里，切页签不丢（逐条应用/跳过的进度也保留）
  const flow = useWordFlow(projectId);
  const { step, filePath, selectedFile, isLoading, error, result, suggestions } = flow;
  const [confirmingClear, setConfirmingClear] = useState(false);
  const update = changeWordFlow.bind(null, projectId);
  const canClear = hasWordProgress(flow);

  const pendingCount = useMemo(
    () => suggestions.filter((item) => item.status === 'pending').length,
    [suggestions],
  );
  const appliedCount = useMemo(
    () => suggestions.filter((item) => item.status === 'applied').length,
    [suggestions],
  );
  const skippedCount = useMemo(
    () => suggestions.filter((item) => item.status === 'skipped').length,
    [suggestions],
  );

  const handleImport = async () => {
    if (!filePath.trim() && !selectedFile) return;

    const token = beginWordRun(projectId);
    update({ isLoading: true, error: '', step: 2 });

    try {
      const json = selectedFile
        ? await importApi.suggestionDocxFile(selectedFile)
        : await importApi.suggestionDocxPath(filePath.trim());

      if (!isCurrentWordRun(projectId, token)) return;
      if (!json.data?.success) {
        throw new Error(json.data?.error || json.error || '建议书解析失败。');
      }

      update({ result: json.data, suggestions: json.data.suggestions || [], step: 3 });
    } catch (importError) {
      if (!isCurrentWordRun(projectId, token)) return;
      update({
        error: importError instanceof Error ? importError.message : '建议书解析失败。',
        step: 1,
      });
    } finally {
      if (isCurrentWordRun(projectId, token)) update({ isLoading: false });
    }
  };

  /** 清空：回到第一步，并作废在途的解析响应。 */
  const handleClear = () => {
    resetWordFlow(projectId);
    setConfirmingClear(false);
  };

  const applySuggestion = async (id: string) => {
    const sourceFlow = wordFlowSnapshot(projectId);
    const targetSuggestion = sourceFlow.suggestions.find((item) => item.id === id);
    if (!targetSuggestion || targetSuggestion.status !== 'pending') return false;
    const editorBeforeApply = useEditorStore.getState();
    if (editorBeforeApply.activeProjectId !== projectId || !editorBeforeApply.currentChapterId) {
      update({ error: '请先打开当前项目的目标章节，再应用建议。' });
      return false;
    }

    update((previous) => ({
      suggestions: previous.suggestions.map((item) => (
        item.id === id ? { ...item, status: 'applying' } : item
      )),
    }));

    try {
      const json = await importApi.applySuggestion({
        projectId,
        suggestionId: targetSuggestion.id,
        action: targetSuggestion.action,
        target: targetSuggestion.target,
        context: editorBeforeApply.chapterContent,
      });

      const resultText = json.data?.result || '';
      // Clearing/reparsing the document invalidates results from its old suggestions.
      if (wordFlowSnapshot(projectId).result !== sourceFlow.result) return false;
      if (!matchesEditorSnapshot(editorBeforeApply, useEditorStore.getState())) {
        update((previous) => ({
          error: '章节或正文已变化，建议结果已保留但未覆盖当前正文；请确认目标章节后重新应用。',
          suggestions: previous.suggestions.map((item) => item.id === id
            ? { ...item, status: 'pending', result: resultText || '建议请求已完成，当前正文未改动。' }
            : item),
        }));
        return false;
      }
      if (resultText) {
        useEditorStore.getState().setChapterContent(resultText);
      }

      update((previous) => ({
        suggestions: previous.suggestions.map((item) => (
          item.id === id
            ? { ...item, status: 'applied', result: resultText || '建议已应用。' }
            : item
        )),
      }));
      return true;
    } catch (applyError) {
      if (wordFlowSnapshot(projectId).result !== sourceFlow.result) return false;
      const message = applyError instanceof Error ? applyError.message : '建议应用失败。';
      update((previous) => ({
        suggestions: previous.suggestions.map((item) => (
          item.id === id
            ? { ...item, status: 'pending', result: `应用失败：${message}` }
            : item
        )),
      }));
      update({ error: message });
      return false;
    }
  };

  const skipSuggestion = (id: string) => {
    update((previous) => ({
      suggestions: previous.suggestions.map((item) => (
        item.id === id ? { ...item, status: 'skipped' } : item
      )),
    }));
  };

  const applyAll = async () => {
    for (const suggestion of suggestions.filter((item) => item.status === 'pending')) {
       
      if (!await applySuggestion(suggestion.id)) break;
    }
  };

  const progressPercent = suggestions.length > 0 ? (appliedCount / suggestions.length) * 100 : 0;

  return (
    <div className="flex-1 overflow-auto" data-testid="word-import-panel">
      <div className="mx-auto max-w-4xl p-8">
        <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-2">
          <h2 className="flex items-center gap-2 text-xl font-bold">
            <FileText size={22} className="text-primary-600" />
            Word 建议书导入
          </h2>

          {canClear && !confirmingClear && (
            <button
              type="button"
              onClick={() => setConfirmingClear(true)}
              className="btn-ghost ml-auto flex items-center gap-1.5 text-sm"
              data-testid="word-import-clear"
            >
              <Trash2 size={15} />
              清空
            </button>
          )}

          {confirmingClear && (
            <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="word-import-clear-bar">
              <span className="text-xs text-surface-500" data-testid="word-import-clear-hint">
                清空后回到第一步，解析结果与逐条应用/跳过的进度都不再保留。
              </span>
              <button type="button" onClick={handleClear} className="btn-primary text-sm" data-testid="word-import-clear-confirm">
                确认清空
              </button>
              <button type="button" onClick={() => setConfirmingClear(false)} className="btn-secondary text-sm" data-testid="word-import-clear-cancel">
                取消
              </button>
            </div>
          )}
        </div>

        {step === 1 && (
          <div className="card space-y-6">
            <div className="flex items-center justify-center gap-4 rounded-xl border-2 border-dashed border-surface-300 p-8 dark:border-surface-600">
              <Upload size={36} className="text-surface-400" />
              <div>
                <h3 className="font-medium">导入建议书</h3>
                <p className="mt-1 text-sm text-surface-500">
                  支持 `.docx` 文件。你可以直接选择文件，也可以粘贴本机路径。解析后，工作台会把建议拆成可执行动作，你可以逐条应用，也可以批量处理。
                </p>
              </div>
            </div>

            <div className="space-y-4">
              <div>
                <label className="mb-2 block text-sm font-medium">直接选择文件</label>
                <input
                  className="input-field w-full"
                  type="file"
                  accept=".docx"
                  onChange={(event) => {
                    const file = event.target.files?.[0] || null;
                    update({ selectedFile: file, error: '' });
                  }}
                  data-testid="word-file-input"
                />
                {selectedFile && <p className="mt-2 text-xs text-surface-500">已选择：{selectedFile.name}</p>}
              </div>

              <div className="flex items-center gap-2 text-xs text-surface-400">
                <span className="h-px flex-1 bg-surface-200 dark:bg-surface-700" />
                或使用本机路径
                <span className="h-px flex-1 bg-surface-200 dark:bg-surface-700" />
              </div>

              <div className="flex gap-2">
                <input
                  className="input-field flex-1"
                  placeholder="例如：F:\\Docs\\editor-notes.docx"
                  value={filePath}
                  onChange={(event) => {
                    // 选了本机路径就清掉先前选的文件，避免两个来源同时存在时不知道用哪个
                    update(event.target.value.trim()
                      ? { filePath: event.target.value, selectedFile: null }
                      : { filePath: event.target.value });
                  }}
                  data-testid="word-file-path-input"
                />
                <button
                  onClick={() => void handleImport()}
                  disabled={(!filePath.trim() && !selectedFile) || isLoading}
                  className="btn-primary"
                  data-testid="word-parse-button"
                >
                  解析建议书
                </button>
              </div>
            </div>

            {error && <p className="text-sm text-error">{error}</p>}

            <div className="space-y-1 text-xs text-surface-400">
              <p>支持的建议类型：新增、修改、增强、压缩、结构调整、人物一致性修正。</p>
              <p>建议可按顺序批量应用，也可以只挑选其中几条。</p>
              <p>默认作用对象是当前编辑器中打开的章节内容。</p>
            </div>
          </div>
        )}

        {(step === 2 || isLoading) && (
          <div className="card py-16 text-center">
            <Loader2 size={48} className="mx-auto mb-4 animate-spin text-primary-500" />
            <h3 className="text-lg font-semibold">正在解析建议书</h3>
            <p className="mt-1 text-sm text-surface-500">{selectedFile?.name || filePath}</p>
          </div>
        )}

        {step === 3 && result?.success && (
          <div className="space-y-6">
            <div className="card border-success bg-success-soft dark:border-success/40 dark:bg-success-soft-dark" data-testid="word-parse-success">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <CheckCircle size={24} className="text-success" />
                  <div>
                    <h3 className="font-semibold text-success dark:text-success-dark">解析完成</h3>
                    <p className="text-sm text-success dark:text-success-dark">
                      {result.fileName}，共识别 {result.totalSuggestions} 条建议
                    </p>
                  </div>
                </div>

                {pendingCount > 0 && (
                  <button onClick={() => void applyAll()} className="btn-primary flex items-center gap-1 text-sm" data-testid="word-apply-all">
                    <Play size={14} />
                    全部应用（{pendingCount}）
                  </button>
                )}
              </div>
            </div>

            {error && <div className="card border-error bg-error-soft text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark">{error}</div>}

            <div className="space-y-2 text-sm">
              <div className="flex items-center gap-4">
                <span className="text-surface-500">
                  <ListChecks size={16} className="mr-1 inline" />
                  已应用 {appliedCount}/{suggestions.length}
                </span>
                <span className="text-surface-400">已跳过 {skippedCount}</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-surface-200 dark:bg-surface-700">
                <div className="h-full rounded-full bg-success transition-all" style={{ width: `${progressPercent}%` }} />
              </div>
            </div>

            <div className="space-y-2">
              {suggestions.map((suggestion) => (
                <div
                  key={suggestion.id}
                  className={`card border transition-all ${
                    suggestion.status === 'applied'
                      ? 'border-success opacity-80 dark:border-success/40'
                      : suggestion.status === 'skipped'
                        ? 'border-surface-200 opacity-50'
                        : 'border-surface-200 dark:border-surface-700'
                  }`}
                >
                  <div className="flex items-start gap-3">
                    {suggestion.status === 'applied' && <CheckCircle size={16} className="text-success" />}
                    {suggestion.status === 'applying' && <Loader2 size={16} className="animate-spin text-primary-500" />}
                    {suggestion.status === 'pending' && <Clock size={16} className="text-surface-400" />}
                    {suggestion.status === 'skipped' && <X size={16} className="text-surface-400" />}

                    <div className="min-w-0 flex-1">
                      <div className="mb-1 flex items-center gap-2">
                        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${PRIORITY_COLORS[suggestion.priority]}`}>
                          {PRIORITY_LABELS[suggestion.priority]}
                        </span>
                        <span className="rounded bg-surface-100 px-1.5 py-0.5 text-xs dark:bg-surface-700">
                          {ACTION_LABELS[suggestion.action] || suggestion.action}
                        </span>
                        <span className="text-xs text-surface-400">{suggestion.target}</span>
                      </div>

                      <p className="text-sm">{suggestion.description}</p>

                      {suggestion.dependencies.length > 0 && (
                        <p className="mt-1 text-xs text-surface-400">
                          依赖建议：{suggestion.dependencies.join('、')}
                        </p>
                      )}

                      {suggestion.result && (
                        <div className="mt-2 border-t border-surface-200 pt-2 dark:border-surface-700">
                          <p className={`text-xs ${suggestion.status === 'applied' ? 'text-success dark:text-success-dark' : 'text-surface-500'}`}>
                            {suggestion.result}
                          </p>
                        </div>
                      )}
                    </div>

                    {suggestion.status === 'pending' && (
                      <div className="shrink-0 items-center gap-1">
                        <button onClick={() => void applySuggestion(suggestion.id)} className="btn-primary px-3 py-1 text-xs" data-testid={`word-suggestion-apply-${suggestion.id}`}>
                          <Sparkles size={12} className="mr-1 inline" />
                          应用
                        </button>
                        <button onClick={() => skipSuggestion(suggestion.id)} className="btn-ghost text-xs" data-testid={`word-suggestion-skip-${suggestion.id}`}>
                          跳过
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>

            {!projectId && (
              <p className="text-xs text-surface-400">
                当前没有检测到明确项目上下文。建议先打开具体项目，再批量应用建议书内容，会更稳妥。
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
