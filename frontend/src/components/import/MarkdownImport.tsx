import { useEffect, useState, type InputHTMLAttributes } from 'react';
import {
  BookOpen,
  CheckCircle,
  Clock,
  Eye,
  FileText,
  FolderOpen,
  Loader2,
  MapPin,
  Trash2,
  Upload,
  Users,
  X,
} from 'lucide-react';

import { importApi } from '../../services/api';
import { useEditorStore, useProjectStore } from '../../stores';
import { notifyProjectRefresh } from '../editor/chapterEvents';
import { matchesEditorSnapshot } from '../editor/editorResultGuard';
import StyleAnalysisPanel from './StyleAnalysisPanel';
import {
  EMPTY_DISCARDED,
  IMPORT_MERGE_MODES,
  beginMarkdownRun,
  changeMarkdownFlow,
  hasMarkdownProgress,
  isCurrentMarkdownRun,
  resetMarkdownFlow,
  useMarkdownFlow,
  type AnalysisTabKey,
} from './importStore';

export default function MarkdownImport({ projectId, isEmpty }: { projectId: string; isEmpty?: boolean }) {
  // 状态放在 store 里（按 projectId 分开存）：切到别的页签再回来，界面保持原样。
  // 已选文件、扫描结果、舍弃标记都在，只有点「清空」才回到第一步。
  const flow = useMarkdownFlow(projectId);
  const {
    step, mode, folderPath, pickedFiles, skippedCount, isLoading, isIngesting,
    error, successMessage, result, activeAnalysisTab, mergeMode, mergeCleaned, discarded,
  } = flow;
  // 清空要按两次（一次误点就得重新选文件、重等几十秒扫描），但确认条只在组件内即可
  const [confirmingClear, setConfirmingClear] = useState(false);
  const update = changeMarkdownFlow.bind(null, projectId);
  const canClear = hasMarkdownProgress(flow);

  const updateProject = useProjectStore((state) => state.updateProject);
  const setCurrentChapter = useEditorStore((state) => state.setCurrentChapter);
  const setChapterContent = useEditorStore((state) => state.setChapterContent);
  const markClean = useEditorStore((state) => state.markClean);

  // `webkitdirectory` 是非标准属性，React 无对应类型；只有 Chromium 系浏览器支持，
  // 不支持的浏览器里整个入口隐藏，避免给出点了没反应的按钮。
  const supportsDirectoryPicker =
    typeof document !== 'undefined' && 'webkitdirectory' in document.createElement('input');
  const directoryInputProps = { webkitdirectory: '', directory: '' } as unknown as InputHTMLAttributes<HTMLInputElement>;

  /** 取上传文件用于排序/展示的相对路径；选文件夹时浏览器会给 webkitRelativePath。 */
  const toRelativePaths = (files: File[]) => files.map((file) => {
    const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
    return relative && relative.trim() ? relative : file.name;
  });

  const sourceLabel = mode === 'upload'
    ? (pickedFiles.length ? `已选 ${pickedFiles.length} 个 Markdown 文件` : '尚未选择文件')
    : folderPath.trim();

  const canScan = mode === 'upload' ? pickedFiles.length > 0 : Boolean(folderPath.trim());

  // 第 3 步的渲染条件是 `step === 3 && result?.success`：两者一旦不同步，
  // 三个分支都不命中，中栏就会变成一片空白。这里主动对齐回第 1 步，
  // 保证导入面板永远至少有一个可见步骤，而不是留白。
  useEffect(() => {
    if (step === 3 && !result?.success) {
      changeMarkdownFlow(projectId, { step: 1 });
    }
  }, [projectId, step, result]);

  const handleFilesPicked = (list: FileList | null) => {
    const all = Array.from(list || []);
    const markdown = all.filter((file) => /\.(md|markdown)$/i.test(file.name));
    update({
      pickedFiles: markdown,
      skippedCount: all.length - markdown.length,
      error: '',
      successMessage: '',
    });
  };

  // 四类都按「值」过滤：识别是启发式的，误判同样存在于地点/时间线/伏笔里，
  // 而入库时后端会重跑一次分析，按下标对齐太脆弱。
  const visibleCharacters = (result?.analysis?.characters ?? []).filter(
    (item) => !discarded.characters.includes(item.name),
  );
  const visibleLocations = (result?.analysis?.locations ?? []).filter(
    (item) => !discarded.locations.includes(item.name),
  );
  const visibleTimeline = (result?.analysis?.timeline ?? []).filter(
    (item) => !discarded.timeline.includes(item.text),
  );
  const visibleForeshadowing = (result?.analysis?.foreshadowing ?? []).filter(
    (item) => !discarded.foreshadowing.includes(item.text),
  );

  const handleDiscard = (key: AnalysisTabKey, value: string) =>
    update((previous) => ({
      discarded: { ...previous.discarded, [key]: [...previous.discarded[key], value] },
    }));

  const handleRestore = (key: AnalysisTabKey) =>
    update((previous) => ({ discarded: { ...previous.discarded, [key]: [] } }));

  /** 条目右侧的叉号：导入成功后禁用（结果已落盘，再改列表就会与库里不一致）。 */
  const discardButton = (key: AnalysisTabKey, value: string, label: string) => (
    <button
      type="button"
      onClick={() => handleDiscard(key, value)}
      disabled={Boolean(successMessage)}
      title={successMessage ? '已导入，如需调整请重新扫描' : `舍弃${label}「${value}」`}
      aria-label={`舍弃${label} ${value}`}
      className="shrink-0 rounded-md p-1 text-surface-400 transition-colors hover:bg-error-soft hover:text-error disabled:cursor-not-allowed disabled:opacity-40"
      data-testid={`analysis-discard-${key}-${value}`}
    >
      <X size={14} />
    </button>
  );

  /** 本类已舍弃条目提示 + 一键撤销（按分类恢复，不动其它分类）。 */
  const discardNotice = (key: AnalysisTabKey, label: string) =>
    discarded[key].length > 0 ? (
      <div
        className="flex items-center gap-2 rounded-lg bg-surface-50 px-3 py-1.5 text-xs text-surface-500 dark:bg-surface-800"
        data-testid={`analysis-discard-notice-${key}`}
      >
        <span>
          已舍弃 {discarded[key].length} 项{label}，导入时不会再计入。
        </span>
        <button
          type="button"
          onClick={() => handleRestore(key)}
          className="ml-auto text-primary-600 hover:underline"
          data-testid={`analysis-restore-${key}`}
        >
          恢复
        </button>
      </div>
    ) : null;

  const handleScan = async () => {
    if (!canScan) return;

    // 记下这一次的身份：清空（或又发起一次扫描）之后，本次响应就不能再写回界面了
    const token = beginMarkdownRun(projectId);
    update({
      isLoading: true,
      error: '',
      successMessage: '',
      // 重新扫描得到的是新一批识别结果，旧的舍弃名单与清理明细都不再适用
      discarded: EMPTY_DISCARDED,
      mergeCleaned: [],
      step: 2,
    });

    try {
      const json = mode === 'upload'
        ? await importApi.markdownFiles(pickedFiles, toRelativePaths(pickedFiles))
        : await importApi.markdownFolder(folderPath.trim());
      if (!isCurrentMarkdownRun(projectId, token)) return;
      if (!json.data?.success) {
        throw new Error(json.data?.error || json.error || 'Markdown 扫描失败。');
      }

      update({ result: json.data, step: 3 });
    } catch (scanError) {
      if (!isCurrentMarkdownRun(projectId, token)) return;
      update({
        error: scanError instanceof Error ? scanError.message : 'Markdown 扫描失败。',
        step: 1,
      });
    } finally {
      if (isCurrentMarkdownRun(projectId, token)) update({ isLoading: false });
    }
  };

  const handleIngest = async () => {
    if (!result?.success || !canScan) return;

    const token = beginMarkdownRun(projectId);
    const editorBeforeImport = useEditorStore.getState();
    update({ isIngesting: true, error: '', successMessage: '' });

    try {
      const json = mode === 'upload'
        ? await importApi.ingestMarkdownFiles(
            pickedFiles,
            toRelativePaths(pickedFiles),
            projectId || undefined,
            undefined,
            discarded,
            mergeMode,
          )
        : await importApi.ingestMarkdownFolder(folderPath.trim(), projectId || undefined, undefined, mergeMode);
      const project = json.data?.project;
      if (!project) {
        throw new Error('导入完成，但未能获取项目数据。');
      }

      // 章节已经落库，编辑器/作品数据必须跟着更新 —— 这几步不受「面板是否被清空」影响
      updateProject(project.id, project);
      // 导入会回收并重建章节，章节 id 与大纲都变了。必须让工作台重新拉一次项目，
      // 否则左侧章节导航仍用导入前的大纲，点进去就是一个不存在的章节（404）。
      notifyProjectRefresh(project.id);

      const firstChapter = json.data?.project?.chapters?.[0] || json.data?.chapters?.[0] || result.chapters?.[0];
      const editorNow = useEditorStore.getState();
      const canApplyToEditor = editorBeforeImport.activeProjectId === projectId
        && matchesEditorSnapshot(editorBeforeImport, editorNow)
        && !editorNow.isDirty;
      if (firstChapter && canApplyToEditor) {
        setCurrentChapter(firstChapter.id);
        setChapterContent(firstChapter.content || '');
        markClean();
      }

      const importedCount = json.data?.importedChapters || result.chapters?.length || 0;
      if (!isCurrentMarkdownRun(projectId, token)) return;
      // 合并模式下「导入了几章」不是重点：真正发生的是新增/替换/保留各多少。
      // 对齐依据与清理明细一并如实回报 —— 它们直接决定了哪几章被改。
      const added = json.data?.addedChapters ?? 0;
      const updated = json.data?.updatedChapters ?? 0;
      const kept = json.data?.keptChapters ?? 0;
      const cleaned = json.data?.cleanedChapters ?? 0;
      const cleanedDetails = json.data?.cleanedDetails ?? [];
      const charactersDetected = json.data?.charactersDetected ?? 0;
      // 这些补充说明与写入方式无关（回收与对齐只影响合并）
      const extraNotes = [
        !canApplyToEditor ? '编辑器已切换或正文有新修改，已保留当前草稿；导入的章节可从导航重新打开。' : '',
        json.data?.mergeKey === 'index' ? '文件名里没有可用的章节号，本次按导入顺序对齐。' : '',
        cleaned > 0 ? `顺带回收了 ${cleaned} 章旧编号残留，可在回收站查看。` : '',
        // 人物**没有**自动入档：识别是词频/词性启发式，误判要作者自己挑，
        // 所以这里只说「有几条候选、去哪儿挑」，不说「已补进」。
        charactersDetected > 0
          ? `识别到 ${charactersDetected} 位人物候选，已列在「人物」页，请挑选后加入档案。`
          : '',
      ].join('');
      update({
        mergeCleaned: cleanedDetails,
        successMessage: (mergeMode === 'merge'
          ? `增量合并完成：新增 ${added} 章、替换 ${updated} 章、保留 ${kept} 章。`
          : `已导入 ${importedCount} 章，并同步到当前项目。`) + extraNotes,
      });
    } catch (ingestError) {
      if (!isCurrentMarkdownRun(projectId, token)) return;
      update({ error: ingestError instanceof Error ? ingestError.message : 'Markdown 导入失败。' });
    } finally {
      if (isCurrentMarkdownRun(projectId, token)) update({ isIngesting: false });
    }
  };

  /** 清空：回到第一步，并作废在途的扫描/入库响应（否则刚清空的面板会被旧结果复活）。 */
  const handleClear = () => {
    resetMarkdownFlow(projectId);
    setConfirmingClear(false);
  };

  return (
    <div className="flex-1 overflow-auto" data-testid="markdown-import-panel">
      <div className="mx-auto max-w-4xl p-8">
        <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-2">
          <h2 className="flex items-center gap-2 text-xl font-bold">
            <FolderOpen size={22} className="text-primary-600" />
            Markdown 章节导入
          </h2>

          {/* 切换页签不再丢掉进度，所以需要一个显式的「清空」：只有点它才回到第一步 */}
          {canClear && !confirmingClear && (
            <button
              type="button"
              onClick={() => setConfirmingClear(true)}
              className="btn-ghost ml-auto flex items-center gap-1.5 text-sm"
              data-testid="markdown-import-clear"
            >
              <Trash2 size={15} />
              清空
            </button>
          )}

          {confirmingClear && (
            <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="markdown-import-clear-bar">
              <span className="text-xs text-surface-500" data-testid="markdown-import-clear-hint">
                清空后回到第一步，本次扫描结果与已舍弃的条目标记都不再保留。
              </span>
              <button type="button" onClick={handleClear} className="btn-primary text-sm" data-testid="markdown-import-clear-confirm">
                确认清空
              </button>
              <button type="button" onClick={() => setConfirmingClear(false)} className="btn-secondary text-sm" data-testid="markdown-import-clear-cancel">
                取消
              </button>
            </div>
          )}
        </div>

        {step === 1 && (
          <div className="card space-y-6">
            <div className="flex gap-2" data-testid="markdown-import-mode">
              <button
                type="button"
                onClick={() => update({ mode: 'upload' })}
                className={mode === 'upload' ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
                data-testid="markdown-mode-upload"
              >
                上传文件
              </button>
              <button
                type="button"
                onClick={() => update({ mode: 'folder' })}
                className={mode === 'folder' ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
                data-testid="markdown-mode-folder"
              >
                服务器文件夹
              </button>
            </div>

            {mode === 'upload' ? (
              <>
                <div className="flex items-center gap-4 rounded-xl border-2 border-dashed border-surface-300 p-6 dark:border-surface-600">
                  <Upload size={40} className="text-surface-400" />
                  <div>
                    <h3 className="font-medium">选择章节文件</h3>
                    <p className="mt-1 text-sm text-surface-500">
                      一次选中所有 `.md` 章节文件，章节顺序会按文件名里的章节号自动排好；也可以直接选整个文件夹。
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap gap-2">
                  <label className="btn-primary cursor-pointer text-sm">
                    选择 .md 文件（可多选）
                    <input
                      type="file"
                      multiple
                      accept=".md,.markdown,text/markdown"
                      className="hidden"
                      onChange={(event) => {
                        handleFilesPicked(event.target.files);
                        event.target.value = '';
                      }}
                      data-testid="markdown-file-input"
                    />
                  </label>
                  {supportsDirectoryPicker && (
                    <label className="btn-secondary cursor-pointer text-sm">
                      选择整个文件夹
                      <input
                        type="file"
                        multiple
                        className="hidden"
                        onChange={(event) => {
                          handleFilesPicked(event.target.files);
                          event.target.value = '';
                        }}
                        data-testid="markdown-directory-input"
                        {...directoryInputProps}
                      />
                    </label>
                  )}
                </div>

                {pickedFiles.length > 0 && (
                  <div data-testid="markdown-picked-files">
                    <p className="text-sm font-medium">已选 {pickedFiles.length} 个文件</p>
                    <div className="mt-2 max-h-40 space-y-1 overflow-auto">
                      {pickedFiles.slice(0, 50).map((file, index) => (
                        <div key={`${file.name}-${index}`} className="flex justify-between gap-3 text-xs text-surface-500">
                          <span className="truncate">{toRelativePaths([file])[0]}</span>
                          <span className="shrink-0">{(file.size / 1024).toFixed(1)} KB</span>
                        </div>
                      ))}
                    </div>
                    {pickedFiles.length > 50 && (
                      <p className="mt-1 text-xs text-surface-400">仅显示前 50 个。</p>
                    )}
                  </div>
                )}

                {skippedCount > 0 && (
                  <p className="text-xs text-warning" data-testid="markdown-skipped-notice">
                    已忽略 {skippedCount} 个非 Markdown 文件。
                  </p>
                )}

                <button
                  onClick={() => void handleScan()}
                  disabled={!canScan || isLoading}
                  className="btn-primary"
                  data-testid="markdown-upload-scan-button"
                >
                  开始扫描
                </button>
              </>
            ) : (
              <>
                <div className="flex items-center gap-4 rounded-xl border-2 border-dashed border-surface-300 p-6 dark:border-surface-600">
                  <FolderOpen size={40} className="text-surface-400" />
                  <div>
                    <h3 className="font-medium">选择章节文件夹</h3>
                    <p className="mt-1 text-sm text-surface-500">
                      工作台会读取分章节 Markdown，梳理已有剧情，并提取人物、地点、时间线和伏笔信息，为后续续写做铺垫。
                    </p>
                  </div>
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium">文件夹路径</label>
                  <div className="flex gap-2">
                    <input
                      className="input-field flex-1"
                      placeholder="例如：F:\\Novel\\MyLongStory"
                      value={folderPath}
                      onChange={(event) => update({ folderPath: event.target.value })}
                      data-testid="markdown-folder-input"
                    />
                    <button
                      onClick={() => void handleScan()}
                      disabled={!canScan || isLoading}
                      className="btn-primary"
                      data-testid="markdown-scan-button"
                    >
                      开始扫描
                    </button>
                  </div>
                  <p className="mt-2 text-xs text-warning" data-testid="markdown-folder-dev-only">
                    该模式会让服务端读取本机路径，仅在本地开发（`DEBUG=true`）下可用；联机部署请用「上传文件」。
                  </p>
                </div>
              </>
            )}

            {error && <p className="text-sm text-error">{error}</p>}

            <div className="space-y-1 text-xs text-surface-400">
              <p>输入要求：包含一个或多个 `.md` 章节文件。</p>
              <p>自动提取：标题、字数、人物、地点、时间线线索与伏笔。</p>
              <p>确认导入后，这些内容会写入当前项目，作为后续创作的上下文。</p>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="card py-16 text-center">
            <Loader2 size={48} className="mx-auto mb-4 animate-spin text-primary-500" />
            <h3 className="mb-2 text-lg font-semibold">正在扫描并分析章节</h3>
            <p className="text-sm text-surface-500">{sourceLabel}</p>
          </div>
        )}

        {step === 3 && result?.success && (
          <div className="space-y-6">
            <div className="card border-success bg-success-soft dark:border-success/40 dark:bg-success-soft-dark" data-testid="markdown-scan-success">
              <div className="flex items-center gap-3">
                <CheckCircle size={24} className="text-success" />
                <div>
                  <h3 className="font-semibold text-success dark:text-success-dark">扫描完成</h3>
                  <p className="text-sm text-success dark:text-success-dark">
                    共 {result.totalFiles} 章，约 {result.totalWords?.toLocaleString()} 字
                  </p>
                </div>
              </div>
            </div>

            {successMessage && (
              <div className="card border-success text-success dark:border-success/40 dark:text-success-dark" data-testid="markdown-ingest-success">
                <p>{successMessage}</p>
                {mergeCleaned.length > 0 && (
                  <p className="mt-2 text-xs text-surface-500" data-testid="markdown-merged-cleanup">
                    认定为旧编号残留并回收：{mergeCleaned.map((item) => `第 ${item.order} 章 ${item.title}`).join('、')}
                  </p>
                )}
              </div>
            )}

            {error && <div className="card border-error bg-error-soft text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark">{error}</div>}

            <div className="card">
              <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <FileText size={16} className="text-primary-600" />
                章节清单
              </h3>
              <div className="space-y-1">
                {result.chapters?.map((chapter) => (
                  <div key={chapter.id} className="flex items-center gap-3 rounded-lg px-3 py-2 hover:bg-surface-50 dark:hover:bg-surface-750">
                    <span className="w-6 text-xs text-surface-400">{chapter.order}.</span>
                    <span className="flex-1 text-sm font-medium">{chapter.title}</span>
                    <span className="text-xs text-surface-400">{chapter.fileName}</span>
                    <span className="w-24 text-right text-xs text-surface-400">{chapter.wordCount.toLocaleString()} 字</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="card">
              <h3 className="mb-3 text-sm font-semibold">剧情概览</h3>
              <p className="mb-3 text-xs text-surface-500">{result.analysis?.summary || '暂时没有可用的分析摘要。'}</p>

              <div className="mb-4 flex gap-2">
                {[
                  { key: 'characters', label: '人物', icon: Users, count: visibleCharacters.length },
                  { key: 'locations', label: '地点', icon: MapPin, count: visibleLocations.length },
                  { key: 'timeline', label: '时间线', icon: Clock, count: visibleTimeline.length },
                  { key: 'foreshadowing', label: '伏笔', icon: Eye, count: visibleForeshadowing.length },
                ].map((tab) => (
                  <button
                    key={tab.key}
                    onClick={() => update({ activeAnalysisTab: tab.key as typeof activeAnalysisTab })}
                    data-testid={`analysis-tab-${tab.key}`}
                    className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                      activeAnalysisTab === tab.key
                        ? 'bg-primary-600 text-white'
                        : 'bg-surface-100 text-surface-500 dark:bg-surface-700'
                    }`}
                  >
                    <tab.icon size={13} />
                    {tab.label}（{tab.count || 0}）
                  </button>
                ))}
              </div>

              {activeAnalysisTab === 'characters' && (
                <div className="space-y-2">
                  {discardNotice('characters', '人物')}
                  {visibleCharacters.length ? visibleCharacters.map((item) => (
                    <div key={item.name} className="flex items-center gap-3 rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
                      <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-100 text-sm font-bold text-primary-700 dark:bg-primary-900/40">
                        {item.name[0]}
                      </div>
                      <span className="flex-1 text-sm font-medium">{item.name}</span>
                      <span className="text-xs text-surface-400">出现 {item.frequency} 次</span>
                      {discardButton('characters', item.name, '人物')}
                    </div>
                  )) : <p className="text-sm text-surface-400">暂未在此批次章节中提取到稳定出现的人物。</p>}
                </div>
              )}

              {activeAnalysisTab === 'locations' && (
                <div className="space-y-2">
                  {discardNotice('locations', '地点')}
                  {visibleLocations.length ? visibleLocations.map((item) => (
                    <div
                      key={item.name}
                      className="flex items-center gap-3 rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800"
                    >
                      <span className="flex-1 text-sm">{item.name}</span>
                      <span className="text-xs text-surface-400">出现 {item.frequency} 次</span>
                      {discardButton('locations', item.name, '地点')}
                    </div>
                  )) : <p className="text-sm text-surface-400">暂未识别到稳定地点引用。</p>}
                </div>
              )}

              {activeAnalysisTab === 'timeline' && (
                <div className="space-y-2">
                  {discardNotice('timeline', '时间线')}
                  {visibleTimeline.length ? visibleTimeline.map((item, index) => (
                    <div
                      key={`${item.text}-${index}`}
                      className="flex items-center gap-3 rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800"
                    >
                      <span className="flex-1 text-sm">{item.text}</span>
                      <span className="text-xs text-surface-400">第 {item.chapter} 章</span>
                      {discardButton('timeline', item.text, '时间线')}
                    </div>
                  )) : <p className="text-sm text-surface-400">暂未提取到清晰时间线标记。</p>}
                </div>
              )}

              {activeAnalysisTab === 'foreshadowing' && (
                <div className="space-y-2">
                  {discardNotice('foreshadowing', '伏笔')}
                  {visibleForeshadowing.length ? visibleForeshadowing.map((item, index) => (
                    <div key={`${item.text}-${index}`} className="rounded-lg bg-warning-soft px-3 py-2 dark:bg-warning-soft-dark">
                      <div className="mb-1 flex items-center gap-2">
                        <Eye size={13} className="text-warning" />
                        <span className="text-xs font-medium text-warning dark:text-warning-dark">{item.category}</span>
                        <span className="ml-auto" />
                        {discardButton('foreshadowing', item.text, '伏笔')}
                      </div>
                      <p className="text-xs text-surface-500">{item.context}</p>
                    </div>
                  )) : <p className="text-sm text-surface-400">暂未识别到明确伏笔。</p>}
                </div>
              )}
            </div>

            {/* 已有项目才需要回答「既有章节怎么办」；空项目下两种方式完全等价，
                就不拿一个会删章节的选项去吓第一本书的作者 */}
            {!isEmpty && (
              <div className="card space-y-3" data-testid="markdown-merge-mode">
                <h3 className="text-sm font-semibold">写入方式</h3>
                <div className="grid gap-2 sm:grid-cols-2">
                  {IMPORT_MERGE_MODES.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      onClick={() => update({ mergeMode: option.id })}
                      disabled={Boolean(successMessage)}
                      aria-pressed={mergeMode === option.id}
                      data-testid={`markdown-merge-mode-${option.id}`}
                      className={`rounded-xl border p-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                        mergeMode === option.id
                          ? 'border-primary-500 bg-primary-50 dark:border-primary-500 dark:bg-primary-900/30'
                          : 'border-surface-200 hover:border-surface-400 dark:border-surface-700'
                      }`}
                    >
                      <span className="flex items-center gap-2 text-sm font-medium">
                        {mergeMode === option.id && <CheckCircle size={15} className="text-primary-600" />}
                        {option.name}
                      </span>
                      <span className="mt-1 block text-xs text-surface-500">{option.description}</span>
                    </button>
                  ))}
                </div>
                {mergeMode === 'replace' && (
                  <p className="text-xs text-error" data-testid="markdown-merge-mode-warning">
                    完全替换会把当前项目的全部章节移入回收站；旧正文在回收站里还能查到，
                    但章节导航里不再有这些章节。只想更新导入文件里的章节，请选「增量合并」。
                  </p>
                )}
              </div>
            )}

            <div className="flex justify-end gap-3">
              <button
                onClick={() => update({ step: 1, result: null, error: '', successMessage: '', mergeCleaned: [] })}
                className="btn-secondary"
                data-testid="markdown-repick-button"
              >
                重新选择
              </button>
              <button
                onClick={() => void handleIngest()}
                disabled={isIngesting}
                className="btn-primary flex items-center gap-2"
                data-testid="markdown-ingest-button"
              >
                {isIngesting ? <Loader2 size={18} className="animate-spin" /> : <BookOpen size={18} />}
                导入到当前项目
              </button>
            </div>

            {/* M4 深度文风分析：章节入库后启用，作为导入完成后的自然下一步 */}
            <StyleAnalysisPanel projectId={projectId} enabled={Boolean(successMessage)} />
          </div>
        )}
      </div>
    </div>
  );
}
