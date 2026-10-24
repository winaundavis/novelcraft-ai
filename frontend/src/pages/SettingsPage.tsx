import { useEffect, useMemo, useState } from 'react';
import { Cpu, Loader2, RefreshCw, ShieldAlert, ShieldCheck, Star, Trash2, Wand2, Zap } from 'lucide-react';

import { settingsApi } from '../services/api';
import { useSettingsStore } from '../stores';
import { Button, Card, Input, Select, useToast } from '../components/common';
import type { LLMConfig, LLMHealth, LLMProvider, WritingTier } from '../types';

const PROVIDER_OPTIONS: { id: LLMProvider; name: string; models: string[] }[] = [
  { id: 'openai', name: 'OpenAI', models: ['gpt-5.2', 'gpt-5.4', 'gpt-5.5', 'gpt-5.6'] },
  { id: 'claude', name: 'Anthropic Claude', models: ['claude-3-5-sonnet-latest', 'claude-3-opus-latest'] },
  { id: 'deepseek', name: 'DeepSeek', models: ['deepseek-v4-flash', 'deepseek-chat', 'deepseek-coder'] },
  { id: 'qwen', name: 'Qwen', models: ['qwen-max', 'qwen-plus', 'qwen-turbo'] },
  { id: 'wenxin', name: 'Wenxin', models: ['ernie-4.0', 'ernie-3.5'] },
  { id: 'kimi', name: 'Kimi', models: ['kimi-k2.6', 'kimi-k3', 'kimi-k2.7-code'] },
  { id: 'glm', name: 'GLM', models: ['glm-4', 'glm-4-flash'] },
  { id: 'local', name: '本地模型（Ollama）', models: ['llama3', 'qwen2', 'mistral'] },
  { id: 'arts', name: 'ARTS', models: ['deepseek-v4-flash'] },
];

// 建议1 写作档位：各 provider 的「性价比 / 高质量」模型映射（选择后写入 writing 角色覆盖）
// 性价比=便宜快（DeepSeek 类）、高质量=文笔好（Claude / GPT-5 旗舰 类）；local 无模型映射，仅调温度。
const TIER_MODEL_MAP: Partial<Record<LLMProvider, { value: string; high: string }>> = {
  openai: { value: 'gpt-5.2', high: 'gpt-5.6' },
  claude: { value: 'claude-3-5-haiku-latest', high: 'claude-3-5-sonnet-latest' },
  deepseek: { value: 'deepseek-v4-flash', high: 'deepseek-v4-flash' },
  qwen: { value: 'qwen-turbo', high: 'qwen-max' },
  wenxin: { value: 'ernie-3.5', high: 'ernie-4.0' },
  kimi: { value: 'kimi-k2.6', high: 'kimi-k3' },
  glm: { value: 'glm-4-flash', high: 'glm-4' },
  local: { value: '', high: '' },
};

export default function SettingsPage() {
  const { llmConfigs, setLLMConfigs, activeLLMConfigId, setActiveLLMConfig } = useSettingsStore();
  const [showAddForm, setShowAddForm] = useState(false);
  const [newProvider, setNewProvider] = useState<LLMProvider>('openai');
  const [newApiKey, setNewApiKey] = useState('');
  const [newApiBase, setNewApiBase] = useState('');
  const [newModel, setNewModel] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState('');
  const [health, setHealth] = useState<LLMHealth | null>(null);
  const [isCheckingHealth, setIsCheckingHealth] = useState(false);
  // 建议1 写作档位：性价比（默认）/ 高质量，持久化到激活配置的 roleOverrides.writing
  const [writingTier, setWritingTier] = useState<WritingTier>('value');
  const [isSavingTier, setIsSavingTier] = useState(false);
  const toast = useToast();

  useEffect(() => {
    let active = true;

    settingsApi.listLLMConfigs()
      .then((json) => {
        if (!active) return;
        const configs = json.data || [];
        setLLMConfigs(configs);
        const activeConfig = configs.find((item) => item.isActive);
        if (activeConfig) setActiveLLMConfig(activeConfig.id);
      })
      .catch((loadError) => {
        if (!active) return;
        setError(loadError instanceof Error ? loadError.message : '模型配置加载失败。');
      });

    return () => {
      active = false;
    };
  }, [setLLMConfigs, setActiveLLMConfig]);

  const checkLLMHealth = async (probe = false) => {
    setIsCheckingHealth(true);
    try {
      const json = await (probe ? settingsApi.checkLLMHealth() : settingsApi.getLLMHealth());
      setHealth(json.data);
    } catch (healthError) {
      setHealth({
        configured: false,
        provider: '',
        model: '',
        ok: false,
        checked: probe,
        error: healthError instanceof Error ? healthError.message : '模型连通性检测失败。',
      });
    } finally {
      setIsCheckingHealth(false);
    }
  };

  useEffect(() => {
    void checkLLMHealth();
  }, []);

  const providerModels = useMemo(
    () => PROVIDER_OPTIONS.find((item) => item.id === newProvider)?.models || [],
    [newProvider],
  );

  const handleAdd = async () => {
    if (!newApiKey.trim()) return;
    setIsSaving(true);
    setError('');

    try {
      const payload: Omit<LLMConfig, 'id'> = {
        provider: newProvider,
        apiKey: newApiKey.trim(),
        apiBase: newApiBase.trim() || getDefaultBase(newProvider),
        model: newModel || providerModels[0] || '',
        temperature: 0.8,
        maxTokens: 4096,
        isActive: true,
      };

      const json = await settingsApi.createLLMConfig(payload);
      const created = json.data;
      const nextConfigs: LLMConfig[] = llmConfigs
        .map((item) => ({ ...item, isActive: false }))
        .filter((item) => item.id !== created.id);

      nextConfigs.unshift(created);
      setLLMConfigs(nextConfigs);
      setActiveLLMConfig(created.id);
      setNewApiKey('');
      setNewApiBase('');
      setNewModel('');
      setShowAddForm(false);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '模型配置保存失败。');
    } finally {
      setIsSaving(false);
    }
  };

  const handleActivate = async (id: string) => {
    try {
      const json = await settingsApi.activateLLMConfig(id);
      const activated = json.data;
      setLLMConfigs(llmConfigs.map((item) => ({ ...item, isActive: item.id === activated.id })));
      setActiveLLMConfig(activated.id);
    } catch (activateError) {
      setError(activateError instanceof Error ? activateError.message : '启用配置失败。');
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await settingsApi.deleteLLMConfig(id);
      const nextConfigs = llmConfigs.filter((item) => item.id !== id);
      setLLMConfigs(nextConfigs);
      const nextActive = nextConfigs.find((item) => item.isActive) || nextConfigs[0] || null;
      setActiveLLMConfig(nextActive?.id || null);
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : '删除配置失败。');
    }
  };

  // 建议1 写作档位：档位作用于当前启用的模型配置
  const activeConfig = useMemo(
    () => llmConfigs.find((item) => item.isActive || item.id === activeLLMConfigId) || llmConfigs[0] || null,
    [activeLLMConfigId, llmConfigs],
  );

  // 从激活配置的 roleOverrides.writing 反推当前档位（无覆盖时默认性价比）
  useEffect(() => {
    const writing = activeConfig?.roleOverrides?.writing;
    if (!writing) {
      setWritingTier('value');
      return;
    }
    const map = activeConfig ? TIER_MODEL_MAP[activeConfig.provider] : undefined;
    if (map && writing.model === map.high) {
      setWritingTier('high');
      return;
    }
    if (typeof writing.temperature === 'number') {
      setWritingTier(writing.temperature <= 0.5 ? 'high' : 'value');
      return;
    }
    setWritingTier('value');
  }, [activeConfig]);

  const handleTierChange = async (tier: WritingTier) => {
    if (!activeConfig) {
      setError('请先启用一个模型配置。');
      return;
    }
    setIsSavingTier(true);
    setError('');
    try {
      const map = TIER_MODEL_MAP[activeConfig.provider];
      const writing: NonNullable<LLMConfig['roleOverrides']>['writing'] = {
        temperature: tier === 'high' ? 0.3 : 0.8,
      };
      if (map) {
        writing.provider = activeConfig.provider;
        writing.model = tier === 'high' ? map.high : map.value;
      }
      const json = await settingsApi.updateRoleOverrides(activeConfig.id, { writing });
      setLLMConfigs(llmConfigs.map((item) => (item.id === json.data.id ? json.data : item)));
      setWritingTier(tier);
      toast.success(
        '写作档位已保存',
        tier === 'high' ? '正文生成将优先使用高质量模型。' : '正文生成将优先使用性价比模型。',
      );
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '写作档位保存失败。');
    } finally {
      setIsSavingTier(false);
    }
  };

  return (
    <div className="flex-1 overflow-auto">
      <div className="mx-auto max-w-3xl px-8 py-8">
        <h1 className="mb-2 text-2xl font-bold">模型配置</h1>
        <p className="mb-8 text-sm text-surface-500">
          在这里管理真实 LLM 接口。即使暂时没有外部模型，当前工作台也保留了本地兜底能力，方便继续创作和测试。
        </p>

        <section className="mb-8">
          <div className="card flex flex-wrap items-center justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-1 flex items-center gap-2 text-sm font-medium">
                {health?.ok ? (
                  <ShieldCheck size={16} className="text-success" />
                ) : (
                  <ShieldAlert size={16} className="text-warning" />
                )}
                模型连通性
              </div>
              <p className="text-xs text-surface-500">
                {health
                  ? health.ok
                    ? `当前使用 ${health.provider} / ${health.model}，连通性正常。`
                    : health.configured
                      ? health.checked === false
                        ? `当前使用 ${health.provider} / ${health.model}，尚未检测连接。`
                        : `当前使用 ${health.provider} / ${health.model}，但连接失败：${health.error || '未知错误'}`
                      : '当前还没有可用的模型配置。'
                  : '正在读取模型配置...'}
              </p>
            </div>
            <Button variant="ghost" size="sm" onClick={() => void checkLLMHealth(true)} disabled={isCheckingHealth}>
              {isCheckingHealth ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              检测连接（可能计费）
            </Button>
          </div>
        </section>

        <section className="mb-10">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-lg font-semibold">
              <Cpu size={20} className="text-primary-600" />
              已接入模型
            </h2>
            <Button onClick={() => setShowAddForm(true)}>
              添加配置
            </Button>
          </div>

          {error && (
            <Card className="mb-4 border-error bg-error-soft text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark">
              {error}
            </Card>
          )}

          {llmConfigs.length === 0 && (
            <Card className="py-12 text-center text-surface-400">
              <Zap size={32} className="mx-auto mb-3 opacity-50" />
              <p className="text-sm">还没有接入任何模型。</p>
              <p className="mt-1 text-xs">你可以稍后再配，当前系统仍可使用本地兜底能力。</p>
            </Card>
          )}

          <div className="space-y-3">
            {llmConfigs.map((config) => (
              <Card key={config.id} className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-3">
                  <div className={`h-3 w-3 rounded-full ${(config.isActive || activeLLMConfigId === config.id) ? 'bg-success' : 'bg-surface-300'}`} />
                  <div className="min-w-0">
                    <p className="text-sm font-medium">
                      {PROVIDER_OPTIONS.find((item) => item.id === config.provider)?.name || config.provider}
                    </p>
                    <p className="truncate text-xs text-surface-400">{config.model}</p>
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-2">
                  {!(config.isActive || activeLLMConfigId === config.id) && (
                    <Button variant="ghost" size="sm" className="text-primary-600" onClick={() => void handleActivate(config.id)}>
                      设为启用
                    </Button>
                  )}
                  <Button variant="ghost" size="sm" className="text-error" onClick={() => void handleDelete(config.id)} aria-label="删除配置">
                    <Trash2 size={14} />
                  </Button>
                </div>
              </Card>
            ))}
          </div>

          {showAddForm && (
            <Card className="mt-4">
              <h3 className="mb-4 font-medium">添加模型配置</h3>
              <div className="space-y-3">
                <Select
                  label="提供方"
                  value={newProvider}
                  onChange={(event) => {
                    setNewProvider(event.target.value as LLMProvider);
                    setNewModel('');
                  }}
                >
                  {PROVIDER_OPTIONS.map((provider) => (
                    <option key={provider.id} value={provider.id}>{provider.name}</option>
                  ))}
                </Select>

                <Input
                  type="password"
                  label="API 密钥"
                  placeholder="sk-..."
                  value={newApiKey}
                  onChange={(event) => setNewApiKey(event.target.value)}
                />

                <Input
                  label="接口地址"
                  placeholder={getDefaultBase(newProvider)}
                  value={newApiBase}
                  onChange={(event) => setNewApiBase(event.target.value)}
                />

                <Select
                  label="模型"
                  value={newModel}
                  onChange={(event) => setNewModel(event.target.value)}
                >
                  <option value="">默认模型</option>
                  {providerModels.map((model) => (
                    <option key={model} value={model}>{model}</option>
                  ))}
                </Select>
              </div>

              <div className="mt-4 flex justify-end gap-2">
                <Button variant="secondary" onClick={() => setShowAddForm(false)}>取消</Button>
                <Button
                  onClick={() => void handleAdd()}
                  disabled={!newApiKey.trim() || isSaving}
                  loading={isSaving}
                >
                  保存
                </Button>
              </div>
            </Card>
          )}
        </section>

        <section className="mb-10">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-lg font-semibold">
              <Wand2 size={20} className="text-primary-600" />
              写作档位
            </h2>
          </div>

          <Card className="space-y-4">
            <p className="text-sm text-surface-500">
              选择正文生成（续写 / 扩写 / 润色 / 去 AI 味）使用的模型偏好，作用于当前启用的模型配置。
            </p>

            {activeConfig ? (
              <>
                <div className="flex flex-col gap-3 sm:flex-row">
                  <button
                    type="button"
                    disabled={isSavingTier}
                    onClick={() => void handleTierChange('value')}
                    data-testid="settings-tier-value"
                    className={`flex flex-1 items-center gap-2 rounded-lg border px-4 py-3 text-left text-sm transition-colors disabled:opacity-60 ${
                      writingTier === 'value'
                        ? 'border-primary-600 bg-primary-50 text-primary-700 dark:border-primary-500 dark:bg-primary-900/30 dark:text-primary-400'
                        : 'border-surface-200 hover:border-surface-300 dark:border-surface-700 dark:hover:border-surface-600'
                    }`}
                  >
                    <Zap size={16} className="shrink-0" />
                    <span>
                      <span className="block font-medium">性价比（默认）</span>
                      <span className="block text-xs text-surface-400">便宜快，如 DeepSeek / Qwen-turbo，适合草稿与批量续写</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    disabled={isSavingTier}
                    onClick={() => void handleTierChange('high')}
                    data-testid="settings-tier-high"
                    className={`flex flex-1 items-center gap-2 rounded-lg border px-4 py-3 text-left text-sm transition-colors disabled:opacity-60 ${
                      writingTier === 'high'
                        ? 'border-primary-600 bg-primary-50 text-primary-700 dark:border-primary-500 dark:bg-primary-900/30 dark:text-primary-400'
                        : 'border-surface-200 hover:border-surface-300 dark:border-surface-700 dark:hover:border-surface-600'
                    }`}
                  >
                    <Star size={16} className="shrink-0" />
                    <span>
                      <span className="block font-medium">高质量</span>
                      <span className="block text-xs text-surface-400">文笔好，如 Claude / GPT-4o，适合正式章节与打磨</span>
                    </span>
                  </button>
                </div>

                <p className="text-xs text-surface-400" data-testid="settings-tier-current">
                  当前配置：{PROVIDER_OPTIONS.find((item) => item.id === activeConfig.provider)?.name || activeConfig.provider} / {activeConfig.model}
                  {activeConfig.roleOverrides?.writing?.model
                    ? ` · 正文生成将使用 ${activeConfig.roleOverrides.writing.model}`
                    : ''}
                  {isSavingTier ? ' · 保存中...' : ''}
                </p>
              </>
            ) : (
              <p className="text-xs text-surface-400">请先在「已接入模型」中启用一个模型配置，再设置写作档位。</p>
            )}
          </Card>
        </section>

        <section>
          <h2 className="mb-4 text-lg font-semibold">说明</h2>
          <Card className="space-y-2 text-sm text-surface-500">
            <p>启用的模型配置会保存在后端数据库中，用于聊天、续写、大纲生成、人物生成和建议应用等场景。</p>
            <p>模型不可用时会显示失败或降级提示；请检查结果后再决定是否采用。</p>
            <p className="border-t border-surface-200 pt-3 text-xs text-surface-400 dark:border-surface-700">
              已保存作品保存在服务器，浏览器还会缓存草稿与界面状态；
              「账户」页仅导出浏览器缓存，不能代替完整作品导出或服务器备份。
            </p>
          </Card>
        </section>
      </div>
    </div>
  );
}

function getDefaultBase(provider: LLMProvider): string {
  const bases: Record<LLMProvider, string> = {
    openai: 'https://api.openai.com/v1',
    claude: 'https://api.anthropic.com/v1',
    deepseek: 'https://api.deepseek.com/v1',
    qwen: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    wenxin: 'https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop',
    kimi: 'https://api.moonshot.cn/v1',
    glm: 'https://open.bigmodel.cn/api/paas/v4',
    local: 'http://localhost:11434/v1',
    arts: 'https://ai.artsapi.com/v1',
  };

  return bases[provider] || '';
}
