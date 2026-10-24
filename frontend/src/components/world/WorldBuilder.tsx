import { useEffect, useMemo, useState } from 'react';
import { BookOpen, Check, ChevronDown, Edit3, Globe, Plus, Sparkles, Trash2, X } from 'lucide-react';

import { projectApi } from '../../services/api';
import { useProjectStore } from '../../stores';
import type { WorldRule } from '../../types';

const CATEGORIES = [
  { key: 'magic', label: '力量体系', icon: 'A' },
  { key: 'technology', label: '科技水平', icon: 'T' },
  { key: 'society', label: '社会结构', icon: 'S' },
  { key: 'culture', label: '文化体系', icon: 'C' },
  { key: 'geography', label: '地理环境', icon: 'G' },
  { key: 'history', label: '历史背景', icon: 'H' },
];

export default function WorldBuilder({ projectId }: { projectId: string }) {
  const project = useProjectStore((state) => state.projects.find((item) => item.id === projectId));
  const updateProject = useProjectStore((state) => state.updateProject);
  const [rules, setRules] = useState<WorldRule[]>(project?.worldRules || []);
  const [expandedCat, setExpandedCat] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [addingToCat, setAddingToCat] = useState<string | null>(null);
  const [newRuleName, setNewRuleName] = useState('');
  const [newRuleDesc, setNewRuleDesc] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    setRules(project?.worldRules || []);
  }, [project]);

  const grouped = useMemo(
    () => CATEGORIES.map((category) => ({
      ...category,
      rules: rules.filter((rule) => rule.category === category.key),
    })),
    [rules],
  );

  const persistRules = async (nextRules: WorldRule[]) => {
    setRules(nextRules);
    if (!projectId) return;

    try {
      const json = await projectApi.update(projectId, { worldRules: nextRules });
      updateProject(projectId, json.data);
      setError('');
    } catch (persistError) {
      setError(persistError instanceof Error ? persistError.message : '世界观规则保存失败。');
    }
  };

  const startEdit = (rule: WorldRule) => {
    setEditingId(rule.id);
    setEditName(rule.name);
    setEditDesc(rule.description);
  };

  const saveEdit = async () => {
    if (!editingId || !editName.trim()) return;

    const nextRules = rules.map((rule) => (
      rule.id === editingId
        ? { ...rule, name: editName.trim(), description: editDesc.trim() }
        : rule
    ));

    await persistRules(nextRules);
    setEditingId(null);
    setEditName('');
    setEditDesc('');
  };

  const addRule = async (category: string) => {
    if (!newRuleName.trim()) return;

    const nextRules = [
      ...rules,
      {
        id: `wr-${Date.now()}`,
        projectId,
        category: category as WorldRule['category'],
        name: newRuleName.trim(),
        description: newRuleDesc.trim(),
      },
    ];

    await persistRules(nextRules);
    setNewRuleName('');
    setNewRuleDesc('');
    setAddingToCat(null);
  };

  const deleteRule = async (id: string) => {
    await persistRules(rules.filter((rule) => rule.id !== id));
  };

  return (
    <div className="flex-1 overflow-auto">
      <div className="mx-auto max-w-4xl p-8">
        <h2 className="mb-6 flex items-center gap-2 text-xl font-bold">
          <Globe size={22} className="text-primary-600" />
          世界构建工作台
        </h2>

        {error && <div className="card mb-6 border-error bg-error-soft text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark" data-testid="world-error-message">{error}</div>}

        {rules.length === 0 && (
          <div className="card mb-6 py-12 text-center text-surface-400" data-testid="world-empty-state">
            <Globe size={40} className="mx-auto mb-3 opacity-40" />
            <p className="text-sm">尚未创建世界规则。</p>
            <p className="mt-1 text-xs">点开下方分类卡片中的“添加规则”，开始搭建你的世界观。</p>
          </div>
        )}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {grouped.map((category) => (
            <div key={category.key} className="card">
              <button
                onClick={() => setExpandedCat(expandedCat === category.key ? null : category.key)}
                className="flex w-full items-center gap-2 text-left"
                data-testid={`world-category-${category.key}`}
              >
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-surface-100 text-xs font-semibold text-surface-600 dark:bg-surface-700 dark:text-surface-200">
                  {category.icon}
                </span>
                <span className="flex-1 text-sm font-medium">{category.label}</span>
                <span className="text-xs text-surface-400">{category.rules.length} 条</span>
                <ChevronDown size={14} className={`text-surface-400 transition-transform ${expandedCat === category.key ? 'rotate-180' : ''}`} />
              </button>

              {expandedCat === category.key && (
                <div className="mt-3 space-y-2 border-t border-surface-200 pt-3 dark:border-surface-700">
                  {category.rules.map((rule) => (
                    <div key={rule.id} className="group rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
                      {editingId === rule.id ? (
                        <div className="space-y-2">
                          <input className="input-field text-sm font-medium" value={editName} onChange={(event) => setEditName(event.target.value)} autoFocus />
                          <textarea className="input-field h-16 resize-none text-xs" value={editDesc} onChange={(event) => setEditDesc(event.target.value)} />
                          <div className="flex justify-end gap-1">
                            <button onClick={() => void saveEdit()} className="btn-ghost p-1 text-success" data-testid="world-edit-save"><Check size={14} /></button>
                            <button onClick={() => setEditingId(null)} className="btn-ghost p-1 text-surface-400" data-testid="world-edit-cancel"><X size={14} /></button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="mb-1 flex items-center justify-between">
                            <span className="text-sm font-medium">{rule.name}</span>
                            <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                              <button onClick={() => startEdit(rule)} className="btn-ghost p-1"><Edit3 size={12} /></button>
                              <button onClick={() => void deleteRule(rule.id)} className="btn-ghost p-1 text-error/80 hover:text-error" data-testid={`world-delete-rule-${rule.id}`}><Trash2 size={12} /></button>
                            </div>
                          </div>
                          <p className="text-xs text-surface-500">{rule.description || '还没有填写规则描述。'}</p>
                        </>
                      )}
                    </div>
                  ))}

                  {addingToCat === category.key ? (
                    <div className="space-y-2 rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
                      <input className="input-field text-sm" placeholder="规则名称" value={newRuleName} onChange={(event) => setNewRuleName(event.target.value)} autoFocus />
                      <textarea className="input-field h-14 resize-none text-xs" placeholder="规则描述" value={newRuleDesc} onChange={(event) => setNewRuleDesc(event.target.value)} />
                      <div className="flex justify-end gap-1">
                        <button onClick={() => void addRule(category.key)} disabled={!newRuleName.trim()} className="btn-ghost p-1 text-success" data-testid="world-edit-save"><Check size={14} /></button>
                        <button onClick={() => { setAddingToCat(null); setNewRuleName(''); setNewRuleDesc(''); }} className="btn-ghost p-1 text-surface-400"><X size={14} /></button>
                      </div>
                    </div>
                  ) : (
                    <button onClick={() => setAddingToCat(category.key)} className="flex w-full items-center gap-1 py-1.5 text-xs text-surface-400 hover:text-primary-500" data-testid={`world-add-rule-${category.key}`}>
                      <Plus size={12} />
                      添加规则
                    </button>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="card mt-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <BookOpen size={18} className="text-primary-600" />
            <div>
              <h3 className="text-sm font-semibold">一致性提示</h3>
              <p className="text-xs text-surface-500">这些规则将在写作、续写和审校时自动引用，帮助保持世界观一致性。</p>
            </div>
          </div>
          <button
            onClick={() => setExpandedCat(expandedCat ? null : CATEGORIES[0].key)}
            className="btn-primary flex items-center gap-1 text-sm"
            data-testid="world-manage-rules-button"
          >
            <Sparkles size={14} />
            管理规则
          </button>
        </div>
      </div>
    </div>
  );
}
