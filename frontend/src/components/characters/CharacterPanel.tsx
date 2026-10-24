import { useEffect, useMemo, useRef, useState } from 'react';
import { Heart, Pencil, Plus, ScanSearch, Sparkles, User, Users, X } from 'lucide-react';

import { aiApi, importApi, projectApi } from '../../services/api';
import { normalizePersonality } from '../../services/normalize';
import { useProjectStore } from '../../stores';
import type {
  Character,
  CharacterAnalysisResult,
  CharacterPersonality,
  CharacterProfileAnalysis,
  CharacterRelation,
  Project,
} from '../../types';
import CharacterRelationGraph from './CharacterRelationGraph';

const ROLE_LABELS: Record<Character['role'], string> = {
  protagonist: '主角',
  antagonist: '反派',
  supporting: '配角',
  mentor: '导师',
  love_interest: '情感线角色',
  sidekick: '搭档',
  cameo: '客串角色',
};

const RELATION_LABELS: Record<CharacterRelation['type'], string> = {
  family: '家人',
  friend: '朋友',
  love: '爱慕',
  enemy: '敌对',
  interest: '共同利益',
  secret: '秘密关联',
  rival: '竞争者',
  master_student: '师徒',
};

const EMPTY_TEXT = '未填写';

// ---- 角色画像辅助（与后端 character_profiler 规则保持一致）----

function inferGenderFromName(name: string): string {
  const femaleChars = new Set('娜婷媛静丽娟霞萍红梅兰芳颖雪娇婉妍慧敏琳燕梦洁茜芸薇露汐玥琪');
  const maleChars = new Set('伟强磊军勇杰涛明辉建平毅超鹏亮龙辰昊宇轩皓天翔峰博睿霖阳泽');
  let female = 0;
  let male = 0;
  for (const ch of name) {
    if (femaleChars.has(ch)) female += 1;
    if (maleChars.has(ch)) male += 1;
  }
  if (female > male) return '女';
  if (male > female) return '男';
  return '未知';
}

// 档案字段只用纯文字（年龄除外）：不再有 MBTI，也不再有维度打分。
const EMPTY_PERSONALITY: CharacterPersonality = { description: '' };

// ── 角色档案字段清单 ────────────────────────────────────────────────
// 字段 key 必须与后端 `character_profile_service.PROFILE_FIELDS` 对齐；
// 后端是清单的唯一来源，这里只负责标签、长度上限与控件形态。
type ProfileFieldKey =
  | 'personality' | 'age' | 'gender' | 'appearance' | 'identity'
  | 'background' | 'coreTrauma' | 'surfaceMotivation' | 'deepDesire'
  | 'arc' | 'catchphrase' | 'speechStyle' | 'habits' | 'values';

interface ProfileFieldSpec {
  key: ProfileFieldKey;
  label: string;
  max: number;
  multiline?: boolean;
  placeholder?: string;
}

interface ProfileSectionSpec {
  title: string;
  span: 1 | 2;
  fields: ProfileFieldSpec[];
}

const PROFILE_SECTIONS: ProfileSectionSpec[] = [
  {
    title: '基础信息',
    span: 1,
    fields: [
      { key: 'age', label: '年龄', max: 3, placeholder: '例如：31' },
      { key: 'gender', label: '性别', max: 6, placeholder: '男 / 女 / 未知' },
      { key: 'appearance', label: '外貌', max: 80, placeholder: '例如：黑色长发，左脸有一道旧疤' },
      { key: 'identity', label: '身份', max: 40, placeholder: '例如：档案调查员' },
    ],
  },
  {
    title: '性格倾向',
    span: 1,
    fields: [
      {
        key: 'personality',
        label: '性格倾向',
        max: 200,
        multiline: true,
        placeholder: '例如：表面随和话少，习惯先观察再决定；被逼到墙角时会突然变得强硬，宁可自己扛也不愿求助。',
      },
    ],
  },
  {
    title: '背景与驱动力',
    span: 2,
    fields: [
      { key: 'background', label: '背景经历', max: 200, multiline: true },
      { key: 'coreTrauma', label: '核心创伤', max: 120, multiline: true },
      { key: 'surfaceMotivation', label: '表层动机', max: 120, multiline: true },
      { key: 'deepDesire', label: '深层欲望', max: 120, multiline: true },
    ],
  },
  {
    title: '写作参考',
    span: 2,
    fields: [
      { key: 'arc', label: '角色弧光', max: 150, multiline: true },
      { key: 'catchphrase', label: '口头禅', max: 40 },
      { key: 'speechStyle', label: '说话风格', max: 120 },
      { key: 'habits', label: '习惯动作', max: 120 },
      { key: 'values', label: '价值观', max: 120 },
    ],
  },
];

const PROFILE_FIELD_LABELS: Record<string, string> = Object.fromEntries(
  PROFILE_SECTIONS.flatMap((section) => section.fields.map((field) => [field.key, field.label])),
);

/** 只能推断、正文很少明写的字段：界面上标成「推断」。 */
const INFERRED_PROFILE_KEYS = new Set(['age', 'gender']);

/** 整卡编辑的草稿；所有值都用字符串，保存时再归一化成数字/枚举。 */
type CharacterDraft = Record<ProfileFieldKey, string> & {
  name: string;
  role: Character['role'];
};

function buildDraft(character: Character): CharacterDraft {
  return {
    name: character.name,
    role: character.role,
    personality: normalizePersonality(character.personality).description,
    age: character.basicInfo.age ? String(character.basicInfo.age) : '',
    gender: character.basicInfo.gender || '',
    appearance: character.basicInfo.appearance || '',
    identity: character.basicInfo.identity || '',
    background: character.background || '',
    coreTrauma: character.coreTrauma || '',
    surfaceMotivation: character.surfaceMotivation || '',
    deepDesire: character.deepDesire || '',
    arc: character.arc || '',
    catchphrase: character.catchphrase || '',
    speechStyle: character.speechStyle || '',
    habits: character.habits || '',
    values: character.values || '',
  };
}

function applyDraft(character: Character, draft: CharacterDraft): Character {
  const parsedAge = Number.parseInt(draft.age, 10);
  return {
    ...character,
    name: draft.name.trim() || character.name,
    role: draft.role,
    basicInfo: {
      ...character.basicInfo,
      age: Number.isFinite(parsedAge) && parsedAge >= 0 ? parsedAge : character.basicInfo.age,
      gender: draft.gender.trim() || '未知',
      appearance: draft.appearance.trim(),
      identity: draft.identity.trim(),
    },
    personality: { description: draft.personality.trim() },
    background: draft.background.trim(),
    coreTrauma: draft.coreTrauma.trim(),
    surfaceMotivation: draft.surfaceMotivation.trim(),
    deepDesire: draft.deepDesire.trim(),
    arc: draft.arc.trim(),
    catchphrase: draft.catchphrase.trim(),
    speechStyle: draft.speechStyle.trim(),
    habits: draft.habits.trim(),
    values: draft.values.trim(),
  };
}

/** 只读展示：把角色卡里的字段值取出来（统一成字符串）。 */
function readProfileValue(character: Character, key: ProfileFieldKey): string {
  if (key === 'personality') return normalizePersonality(character.personality).description;
  if (key === 'age') return character.basicInfo.age ? String(character.basicInfo.age) : '';
  if (key === 'gender') return character.basicInfo.gender || '';
  if (key === 'appearance') return character.basicInfo.appearance || '';
  if (key === 'identity') return character.basicInfo.identity || '';
  const raw = (character as unknown as Record<string, unknown>)[key];
  return typeof raw === 'string' ? raw : '';
}

/** 前端持有的 AI 建议状态（分桶存到角色 id 上）。 */
interface ProfileSuggestionState {
  fields: Record<string, string>;
  inferred: string[];
  rejected: Record<string, string>;
  omitted: string[];
  basis: CharacterProfileAnalysis['basis'];
}

function toSuggestionState(result: CharacterProfileAnalysis): ProfileSuggestionState {
  const fields: Record<string, string> = {};
  for (const [key, value] of Object.entries(result.suggestions || {})) {
    fields[key] = String(value);
  }
  return {
    fields,
    inferred: result.inferred || [],
    rejected: result.rejected || {},
    omitted: result.omitted || [],
    basis: result.basis || null,
  };
}

function fieldLabels(keys: string[]): string {
  return keys.map((key) => PROFILE_FIELD_LABELS[key] || key).join('、');
}

function buildEmptyCharacter(projectId: string, form: {
  role: Character['role'];
  name: string;
  gender: string;
  age: number;
  identity: string;
}): Character {
  const displayName = form.name.trim() || '未命名角色';
  const identity = form.identity.trim() || displayName;

  return {
    id: `char-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    projectId,
    name: displayName,
    aliases: [],
    role: form.role,
    basicInfo: {
      age: form.age,
      gender: form.gender === '未知' ? inferGenderFromName(displayName) : form.gender,
      appearance: '',
      identity,
      occupation: form.identity.trim(),
    },
    personality: { ...EMPTY_PERSONALITY },
    background: '',
    coreTrauma: '',
    surfaceMotivation: '',
    deepDesire: '',
    arc: '',
    catchphrase: '',
    speechStyle: '',
    habits: '',
    values: '',
    relations: [],
  };
}

function normalizeCharacter(character: Partial<Character>, projectId: string): Character {
  const role = character.role || 'supporting';
  const basicInfo = character.basicInfo || { age: 0, gender: '未知', appearance: '', identity: '', occupation: '' };
  return {
    ...buildEmptyCharacter(projectId, { role, name: character.name || '未命名角色', gender: basicInfo.gender || '未知', age: basicInfo.age || 0, identity: basicInfo.identity || '' }),
    ...character,
    projectId: character.projectId || projectId,
    aliases: character.aliases || [],
    basicInfo: { ...basicInfo, age: basicInfo.age || 0, gender: basicInfo.gender || '未知', appearance: basicInfo.appearance || '', identity: basicInfo.identity || '', occupation: basicInfo.occupation || '' },
    // 旧数据里的五维分数/MBTI 在此被抛弃，只保留文字描述
    personality: normalizePersonality(character.personality),
    background: character.background || '',
    coreTrauma: character.coreTrauma || '',
    surfaceMotivation: character.surfaceMotivation || '',
    deepDesire: character.deepDesire || '',
    arc: character.arc || '',
    catchphrase: character.catchphrase || '',
    speechStyle: character.speechStyle || '',
    habits: character.habits || '',
    values: character.values || '',
    relations: character.relations || [],
  };
}

// ── 导入候选人物：导入不写档案，候选留给作者自己挑 ──────────────────
//
// 背景：导入扫描识别到的人物**不再自动写入** `projects.characters` —— 识别是词频/词性
// 启发式，二十个候选里混进「毛巾」「安静」这种误判是常态，自动建档等于把垃圾数据变成
// 作者必须清理的负担。候选留在项目的 `importAnalysis` 里，在这里列出来，勾选后才由
// 后端按「只搬事实」的规则入档（规则只有一份：`_merge_analysis_characters`）。

interface ImportCandidate {
  name: string;
  frequency: number;
}

function importDismissalKey(projectId: string): string {
  return `novelcraft:import-character-candidates:${projectId}`;
}

/** 记住「这批候选我已经看过了」，免得每次打开人物页都问同一批名字。 */
function readImportDismissal(projectId: string): string {
  try {
    return window.localStorage.getItem(importDismissalKey(projectId)) || '';
  } catch {
    return '';
  }
}

function writeImportDismissal(projectId: string, signature: string): void {
  try {
    window.localStorage.setItem(importDismissalKey(projectId), signature);
  } catch {
    // 隐私模式 / 配额清空：忽略。最坏结果是下次打开再问一遍，不影响数据。
  }
}

function ImportCharacterCandidates({
  candidates,
  picked,
  busy,
  error,
  onToggle,
  onToggleAll,
  onAccept,
  onDismiss,
}: {
  candidates: ImportCandidate[];
  picked: Set<string>;
  busy: boolean;
  error: string;
  onToggle: (name: string) => void;
  onToggleAll: () => void;
  onAccept: () => void;
  onDismiss: () => void;
}) {
  const allPicked = candidates.length > 0 && picked.size === candidates.length;

  return (
    <div
      className="shrink-0 border-b border-primary-200 bg-primary-50/70 p-3 dark:border-primary-900/40 dark:bg-primary-900/20"
      data-testid="import-character-candidates"
    >
      <h4 className="text-xs font-semibold text-primary-800 dark:text-primary-200">
        导入识别到 {candidates.length} 位人物
      </h4>
      <p className="mt-1 text-[11px] leading-relaxed text-surface-500">
        勾选后才会加入档案，没勾的不会写进去。识别是词频/词性启发式，可能把不是人名的词也算进来，请自己过一眼。
      </p>
      <div className="mt-2 max-h-56 space-y-0.5 overflow-auto">
        {candidates.map((candidate) => (
          <label
            key={candidate.name}
            className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-white/70 dark:hover:bg-surface-800"
            data-testid={`import-candidate-${candidate.name}`}
          >
            <input
              type="checkbox"
              className="h-3.5 w-3.5 shrink-0"
              checked={picked.has(candidate.name)}
              disabled={busy}
              onChange={() => onToggle(candidate.name)}
              data-testid={`import-candidate-checkbox-${candidate.name}`}
            />
            <span className="min-w-0 flex-1 truncate">{candidate.name}</span>
            <span className="shrink-0 text-surface-400">{candidate.frequency} 次</span>
          </label>
        ))}
      </div>
      {error && (
        <p className="mt-2 text-[11px] text-error" data-testid="import-candidates-error">
          {error}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-primary px-2 py-1 text-[11px]"
          disabled={busy || picked.size === 0}
          onClick={onAccept}
          data-testid="import-candidates-accept"
        >
          {busy ? '加入中...' : `加入选中（${picked.size}）`}
        </button>
        <button
          type="button"
          className="btn-ghost px-2 py-1 text-[11px]"
          disabled={busy}
          onClick={onToggleAll}
          data-testid="import-candidates-toggle-all"
        >
          {allPicked ? '全部取消' : '全选'}
        </button>
        <button
          type="button"
          className="btn-ghost px-2 py-1 text-[11px] text-surface-500"
          disabled={busy}
          onClick={onDismiss}
          data-testid="import-candidates-dismiss"
        >
          都不是，别再提示
        </button>
      </div>
    </div>
  );
}

export default function CharacterPanel({
  projectId,
  isEmpty = false,
  project: projectProp = null,
}: {
  projectId: string;
  isEmpty?: boolean;
  project?: Project | null;
}) {
  const storeProject = useProjectStore((state) => state.projects.find((item) => item.id === projectId));
  const addProject = useProjectStore((state) => state.addProject);
  const updateProject = useProjectStore((state) => state.updateProject);
  const project = storeProject || projectProp;

  const [characters, setCharacters] = useState<Character[]>(
    (project?.characters || []).map((character) => normalizeCharacter(character, projectId)),
  );

  /**
   * 上面的 state 只在挂载时读一次 `project`，而项目里的人物可能在挂载之后才变 ——
   * 最典型的是**导入**：扫描到的人物由服务端写进项目，作者再切回本页时就该看到它们。
   * 这里**只补不覆盖**：把项目里还没在本页出现的角色追加进来，
   * 绝不改动作者正在编辑的档案（同名视为同一个人，跳过）。
   */
  useEffect(() => {
    const incoming = (project?.characters || []).map((character) => normalizeCharacter(character, projectId));
    if (!incoming.length) return;
    setCharacters((current) => {
      const knownIds = new Set(current.map((item) => item.id));
      const knownNames = new Set(current.map((item) => item.name));
      const missing = incoming.filter((item) => !knownIds.has(item.id) && !knownNames.has(item.name));
      return missing.length ? [...current, ...missing] : current;
    });
  }, [project?.characters, projectId]);

  const [selectedId, setSelectedId] = useState('');
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState('');

  // 导入候选人物的勾选状态（与档案编辑无关，单独一份）
  const [importPicked, setImportPicked] = useState<Set<string>>(new Set());
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState('');
  const [importDismissed, setImportDismissed] = useState(() => readImportDismissal(projectId));

  useEffect(() => {
    // 换项目就重新读一遍「已忽略」记录，别把上个项目的判断用到这个项目上
    setImportDismissed(readImportDismissal(projectId));
  }, [projectId]);

  /**
   * 导入候选 = 项目里 `importAnalysis.characters` 减去已经在档案里的名字。
   * 导入刻意不写档案，所以这里是「可以进来、还没进来」的那一批。
   */
  const importCandidates = useMemo<ImportCandidate[]>(() => {
    const raw = project?.importAnalysis?.characters || [];
    const known = new Set(characters.map((item) => (item.name || '').trim()));
    const seen = new Set<string>();
    const result: ImportCandidate[] = [];
    raw.forEach((item) => {
      const name = (item?.name || '').trim();
      if (!name || known.has(name) || seen.has(name)) return;
      seen.add(name);
      result.push({ name, frequency: Number(item?.frequency) || 0 });
    });
    return result;
  }, [project?.importAnalysis, characters]);

  // 换了一批候选（重新导入 / 已加入）签名就会变，这时「已忽略」不再适用，面板重新出现
  const importSignature = useMemo(
    () => importCandidates.map((item) => item.name).sort().join('\u0000'),
    [importCandidates],
  );
  const showImportCandidates = importCandidates.length > 0 && importSignature !== importDismissed;

  const toggleImportCandidate = (name: string) => {
    setImportPicked((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const toggleAllImportCandidates = () => {
    setImportPicked((current) =>
      current.size === importCandidates.length
        ? new Set<string>()
        : new Set(importCandidates.map((item) => item.name)),
    );
  };

  const dismissImportCandidates = () => {
    writeImportDismissal(projectId, importSignature);
    setImportDismissed(importSignature);
  };

  const acceptImportCandidates = async () => {
    const names = Array.from(importPicked);
    if (!names.length || importBusy) return;
    setImportBusy(true);
    setImportError('');
    try {
      const json = await importApi.acceptAnalysisCharacters(projectId, names);
      const updated = json.data?.project;
      if (updated) {
        // 以服务端返回的档案为准（只增不改，作者已有的手写内容不会因此丢失）
        setCharacters((updated.characters || []).map((item) => normalizeCharacter(item, projectId)));
        const hasProjectInStore = useProjectStore.getState().projects.some((item) => item.id === projectId);
        if (hasProjectInStore) updateProject(projectId, updated);
        else addProject(updated);
      }
      setImportPicked(new Set());
    } catch (acceptError) {
      setImportError(acceptError instanceof Error ? acceptError.message : '候选人物加入失败，请重试。');
    } finally {
      setImportBusy(false);
    }
  };

  const importCandidateProps = {
    candidates: importCandidates,
    picked: importPicked,
    busy: importBusy,
    error: importError,
    onToggle: toggleImportCandidate,
    onToggleAll: toggleAllImportCandidates,
    onAccept: () => void acceptImportCandidates(),
    onDismiss: dismissImportCandidates,
  };

  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisResult, setAnalysisResult] = useState<CharacterAnalysisResult | null>(null);
  const [analysisChecked, setAnalysisChecked] = useState<Set<string>>(new Set());
  const [analysisError, setAnalysisError] = useState('');

  // 角色档案：作者手写与 AI 分析并存，AI 建议**不自动覆盖**作者内容。
  //
  // 所有临时状态都**按角色 id 分桶**：作者写到一半去点别的角色看看，再切回来时
  // 草稿、AI 建议与报错都还在。早期版本在切角色时直接重置，会静默吞掉作者手写的内容。
  const [editingCharacterId, setEditingCharacterId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, CharacterDraft>>({});
  const [aiSuggestions, setAiSuggestions] = useState<Record<string, ProfileSuggestionState>>({});
  const [aiErrors, setAiErrors] = useState<Record<string, string>>({});
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const [analyzingId, setAnalyzingId] = useState<string | null>(null);
  // 每次分析递增；结果回来后要核对归属，避免把 A 角色的分析写到 B 角色头上
  const profileRequestSeq = useRef(0);

  const [form, setForm] = useState<{
    role: Character['role'];
    hints: string;
    name: string;
    gender: string;
    age: number;
    identity: string;
  }>({
    role: 'supporting',
    hints: '',
    name: '',
    gender: '未知',
    age: 25,
    identity: '',
  });

  const selected = useMemo(() => {
    const raw = characters.find((item) => item.id === selectedId) || characters[0] || null;
    return raw ? normalizeCharacter(raw, projectId) : null;
  }, [characters, selectedId, projectId]);

  // 当前选中角色的档案编辑视图（分桶读取）
  const profileKey = selected?.id || '';
  const isEditingProfile = Boolean(profileKey) && editingCharacterId === profileKey;
  const draft = drafts[profileKey] || null;
  const suggestions = aiSuggestions[profileKey] || null;
  const aiError = aiErrors[profileKey] || '';
  const saveError = saveErrors[profileKey] || '';
  const isAnalyzingProfile = Boolean(profileKey) && analyzingId === profileKey;
  const pendingSuggestionCount = suggestions ? Object.keys(suggestions.fields).length : 0;

  const persistCharacters = async (nextCharacters: Character[]) => {
    setCharacters(nextCharacters);
    if (!projectId) return;

    const json = await projectApi.update(projectId, { characters: nextCharacters });
    const hasProjectInStore = useProjectStore.getState().projects.some((item) => item.id === projectId);

    if (json.data) {
      if (hasProjectInStore) {
        updateProject(projectId, json.data);
      } else {
        addProject(json.data);
      }
    } else if (project) {
      if (hasProjectInStore) {
        updateProject(projectId, { characters: nextCharacters });
      } else {
        addProject({ ...project, characters: nextCharacters });
      }
    }
  };

  const resetForm = () => {
    setForm({
      role: 'supporting',
      hints: '',
      name: '',
      gender: '未知',
      age: 25,
      identity: '',
    });
    setError('');
  };

  const handleCreateCharacter = async () => {
    setError('');

    try {
      let nextCharacter: Character;

      if (form.hints.trim()) {
        setIsGenerating(true);
        const json = await aiApi.generateCharacter({
          projectId,
          role: form.role,
          hints: form.hints.trim(),
          genre: project?.genre || 'fantasy',
        });
        nextCharacter = json.data;
      } else {
        nextCharacter = buildEmptyCharacter(projectId, form);
      }

      const nextCharacters = [...characters, nextCharacter];
      await persistCharacters(nextCharacters);
      setSelectedId(nextCharacter.id);
      setShowCreateForm(false);
      resetForm();
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : '角色创建失败。');
    } finally {
      setIsGenerating(false);
    }
  };

  const handleAnalyzeCharacters = async () => {
    setAnalysisError('');
    setIsAnalyzing(true);

    try {
      const json = await projectApi.analyzeCharacters(projectId);
      setAnalysisResult(json.data);
      setAnalysisChecked(new Set((json.data?.characters || []).map((item) => item.name)));
    } catch (analyzeError) {
      setAnalysisResult(null);
      setAnalysisError(analyzeError instanceof Error ? analyzeError.message : '人物分析失败。');
    } finally {
      setIsAnalyzing(false);
    }
  };

  const toggleAnalysisCharacter = (name: string) => {
    setAnalysisChecked((previous) => {
      const next = new Set(previous);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const closeAnalysisPanel = () => {
    setAnalysisResult(null);
    setAnalysisChecked(new Set());
    setAnalysisError('');
    setIsAnalyzing(false);
  };

  // ---- 角色档案：手写 / AI 分析 ----

  const setDraftField = (key: keyof CharacterDraft, value: string) => {
    if (!profileKey) return;
    setDrafts((previous) => {
      const current = previous[profileKey];
      if (!current) return previous;
      return { ...previous, [profileKey]: { ...current, [key]: value } };
    });
  };

  /** 更新某个角色的 AI 建议状态；返回 null 表示整块建议收起。 */
  const patchSuggestions = (
    characterId: string,
    updater: (state: ProfileSuggestionState | null) => ProfileSuggestionState | null,
  ) => {
    setAiSuggestions((previous) => {
      const next = updater(previous[characterId] || null);
      if (next) return { ...previous, [characterId]: next };
      const copy = { ...previous };
      delete copy[characterId];
      return copy;
    });
  };

  const clearProfileTransient = (characterId: string) => {
    patchSuggestions(characterId, () => null);
    const withoutKey = <T,>(previous: Record<string, T>): Record<string, T> => {
      const next = { ...previous };
      delete next[characterId];
      return next;
    };
    setAiErrors(withoutKey);
    setSaveErrors(withoutKey);
  };

  const handleStartProfileEdit = () => {
    if (!selected) return;
    setDrafts((previous) => ({ ...previous, [selected.id]: buildDraft(selected) }));
    clearProfileTransient(selected.id);
    setEditingCharacterId(selected.id);
  };

  /** 取消：只丢弃当前角色未保存的编辑，不影响别的角色。 */
  const handleCancelProfileEdit = () => {
    const characterId = profileKey;
    profileRequestSeq.current += 1;
    setEditingCharacterId((current) => (current === characterId ? null : current));
    setAnalyzingId((current) => (current === characterId ? null : current));
    if (characterId) clearProfileTransient(characterId);
  };

  const handleAnalyzeProfile = async () => {
    if (!selected) return;
    const targetId = selected.id;
    // 始终用「已保存」的名字去找角色：草稿里改了名还没保存时，后端按旧名字查得到
    const characterName = selected.name;
    const seq = profileRequestSeq.current + 1;
    profileRequestSeq.current = seq;

    setAnalyzingId(targetId);
    setAiErrors((previous) => ({ ...previous, [targetId]: '' }));
    patchSuggestions(targetId, () => null);

    try {
      const json = await projectApi.analyzeCharacterProfile(projectId, characterName);
      if (profileRequestSeq.current !== seq) return;
      const data = json.data;
      if (!data || data.degraded) {
        setAiErrors((previous) => ({
          ...previous,
          [targetId]: data?.reason || 'AI 分析没有返回可用结果，请稍后重试。',
        }));
        return;
      }
      const next = toSuggestionState(data);
      if (Object.keys(next.fields).length === 0) {
        setAiErrors((previous) => ({
          ...previous,
          [targetId]: data.reason || 'AI 没有给出任何字段建议，请稍后重试。',
        }));
        return;
      }
      // 建议始终记在目标角色名下：中途切到别的角色也只会写回它自己的条目
      setAiSuggestions((previous) => ({ ...previous, [targetId]: next }));
    } catch (analyzeError) {
      if (profileRequestSeq.current !== seq) return;
      setAiErrors((previous) => ({
        ...previous,
        [targetId]: analyzeError instanceof Error ? analyzeError.message : 'AI 分析失败。',
      }));
    } finally {
      if (profileRequestSeq.current === seq) {
        setAnalyzingId((current) => (current === targetId ? null : current));
      }
    }
  };

  /** 把一条建议写进草稿（不落盘）。年龄只保留数字。 */
  const writeSuggestionToDraft = (key: string, value: string) => {
    if (key === 'age') {
      setDraftField('age', value.replace(/[^\d]/g, '').slice(0, 3));
      return;
    }
    setDraftField(key as keyof CharacterDraft, value);
  };

  /** 采用某条建议：只改草稿对应字段，仍需点「保存」才落盘。 */
  const handleAcceptSuggestion = (key: string) => {
    const value = suggestions?.fields[key];
    if (value === undefined) return;
    writeSuggestionToDraft(key, value);
    patchSuggestions(profileKey, (state) => {
      if (!state) return null;
      const fields = { ...state.fields };
      delete fields[key];
      return Object.keys(fields).length > 0 ? { ...state, fields } : null;
    });
  };

  const handleAcceptAllSuggestions = () => {
    if (!suggestions) return;
    for (const [key, value] of Object.entries(suggestions.fields)) {
      writeSuggestionToDraft(key, value);
    }
    patchSuggestions(profileKey, () => null);
  };

  /** 忽略某条建议：草稿一字不动。 */
  const handleIgnoreSuggestion = (key: string) => {
    patchSuggestions(profileKey, (state) => {
      if (!state) return null;
      const fields = { ...state.fields };
      delete fields[key];
      return Object.keys(fields).length > 0 ? { ...state, fields } : null;
    });
  };

  const handleSaveProfile = async () => {
    if (!selected || !draft) return;
    const targetId = selected.id;
    setSaveErrors((previous) => ({ ...previous, [targetId]: '' }));

    const previousCharacters = characters;
    const nextCharacters = characters.map((item) => (
      item.id === targetId ? applyDraft(item, draft) : item
    ));

    try {
      await persistCharacters(nextCharacters);
      profileRequestSeq.current += 1;
      setEditingCharacterId((current) => (current === targetId ? null : current));
      setAnalyzingId((current) => (current === targetId ? null : current));
      clearProfileTransient(targetId);
    } catch (saveError) {
      // 保存失败时回滚本地状态，不让界面假装已经写进去了
      setCharacters(previousCharacters);
      setSaveErrors((previous) => ({
        ...previous,
        [targetId]: saveError instanceof Error ? saveError.message : '保存失败，请稍后重试。',
      }));
    }
  };

  const previewCharacters = useMemo<Character[]>(() => {
    if (!analysisResult) return [];

    const chosen = analysisResult.characters.filter((item) => analysisChecked.has(item.name));
    const previews: Character[] = chosen.map((item, index): Character => ({
      id: `preview-${index}`,
      projectId,
      name: item.name,
      aliases: [],
      role: item.role,
      basicInfo: {
        age: 25,
        gender: '未知',
        appearance: '',
        identity: item.name,
        occupation: '',
      },
      personality: normalizePersonality(item.personality),
      background: item.summary || '',
      coreTrauma: '',
      surfaceMotivation: '',
      deepDesire: '',
      arc: '',
      catchphrase: '',
      speechStyle: '',
      habits: '',
      values: '',
      relations: [],
    }));

    const idByName = new Map(previews.map((item) => [item.name, item.id]));

    for (const relation of analysisResult.relations) {
      const sourceId = idByName.get(relation.source);
      const targetId = idByName.get(relation.target);
      if (!sourceId || !targetId || sourceId === targetId) continue;

      const source = previews.find((item) => item.id === sourceId);
      if (!source) continue;

      if (source.relations.some((item) => item.targetCharacterId === targetId && item.type === relation.type)) {
        continue;
      }

      source.relations.push({
        targetCharacterId: targetId,
        type: relation.type,
        description: relation.description,
        sinceChapter: relation.sinceChapter,
      });
    }

    return previews;
  }, [analysisChecked, analysisResult, projectId]);

  const handleImportAnalysis = async () => {
    if (!analysisResult) return;

    const existingByName = new Map(characters.map((item) => [item.name, item]));
    const newCharacters = analysisResult.characters
      .filter((item) => analysisChecked.has(item.name))
      .filter((item) => !existingByName.has(item.name))
      .map((item) => {
        const base = buildEmptyCharacter(projectId, {
          role: item.role,
          name: item.name,
          gender: item.gender || '未知',
          age: 25,
          identity: item.name,
        });
        // 使用后端从正文推断的画像：性别、外貌、一句话简介与文字性格描述
        const personality = item.personality ? normalizePersonality(item.personality) : base.personality;
        return {
          ...base,
          basicInfo: {
            ...base.basicInfo,
            gender: (item.gender && item.gender !== '未知') ? item.gender : base.basicInfo.gender,
            appearance: item.appearance || base.basicInfo.appearance,
          },
          personality,
          background: item.summary || base.background,
          surfaceMotivation: item.summary || base.surfaceMotivation,
        };
      });

    const idByName = new Map<string, string>();
    characters.forEach((item) => idByName.set(item.name, item.id));
    newCharacters.forEach((item) => idByName.set(item.name, item.id));

    const merged = [...characters, ...newCharacters].map((character) => {
      const relations = [...(character.relations || [])];
      const relationKeys = new Set(relations.map((item) => `${item.targetCharacterId}-${item.type}`));

      for (const relation of analysisResult.relations) {
        if (!analysisChecked.has(relation.source) || !analysisChecked.has(relation.target)) continue;

        // 正向：当前角色是关系的发起方
        if (relation.source === character.name) {
          const targetId = idByName.get(relation.target);
          if (targetId && targetId !== character.id) {
            const key = `${targetId}-${relation.type}`;
            if (!relationKeys.has(key)) {
              relations.push({
                targetCharacterId: targetId,
                type: relation.type,
                description: relation.description,
                sinceChapter: relation.sinceChapter,
              });
              relationKeys.add(key);
            }
          }
        }

        // 反向：当前角色是关系的接受方，也补一条，让双方详情都能看到彼此
        if (relation.target === character.name) {
          const sourceId = idByName.get(relation.source);
          if (sourceId && sourceId !== character.id) {
            const key = `${sourceId}-${relation.type}`;
            if (!relationKeys.has(key)) {
              relations.push({
                targetCharacterId: sourceId,
                type: relation.type,
                description: relation.description,
                sinceChapter: relation.sinceChapter,
              });
              relationKeys.add(key);
            }
          }
        }
      }

      return { ...character, relations };
    });

    await persistCharacters(merged);
    closeAnalysisPanel();
    if (newCharacters[0]) setSelectedId(newCharacters[0].id);
  };

  const analysisPanel = (isAnalyzing || analysisResult || analysisError) ? (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={closeAnalysisPanel}
    >
      <div
        className="card max-h-[90vh] w-full max-w-2xl overflow-auto"
        onClick={(event) => event.stopPropagation()}
      >
        {isAnalyzing && (
          <div className="flex flex-col items-center justify-center py-16">
            <ScanSearch size={32} className="mb-3 animate-pulse text-primary-500" />
            <p className="text-sm font-medium">AI 正在通读正文并分析人物关系...</p>
            <p className="mt-1 text-xs text-surface-400">作品较长时可能需要 10 到 40 秒，请稍候。</p>
          </div>
        )}

        {!isAnalyzing && analysisError && !analysisResult && (
          <div className="py-10 text-center">
            <p className="mb-1 text-sm font-medium text-error">分析失败</p>
            <p className="mb-4 text-xs text-surface-400">{analysisError}</p>
            <button onClick={closeAnalysisPanel} className="btn-secondary text-sm">关闭</button>
          </div>
        )}

        {!isAnalyzing && analysisResult && (
          <>
            <div className="mb-4 flex items-start justify-between border-b border-surface-200 pb-3 dark:border-surface-700">
              <div>
                <h3 className="flex items-center gap-2 text-lg font-semibold">
                  <ScanSearch size={18} className="text-primary-600" />
                  一键分析作品人物
                </h3>
                <p className="mt-1 text-xs text-surface-400">{analysisResult.summary || '已生成角色识别结果。'}</p>
              </div>
              <button onClick={closeAnalysisPanel} className="btn-ghost p-1.5" aria-label="关闭">
                <X size={18} />
              </button>
            </div>

            <div className="mb-4">
              <div className="mb-2 flex items-center justify-between">
                <h4 className="text-sm font-semibold">识别出的人物（{analysisResult.characters.length}）</h4>
                <button
                  type="button"
                  className="text-xs text-primary-600 hover:underline"
                  onClick={() => {
                    setAnalysisChecked((previous) => (
                      previous.size === analysisResult.characters.length
                        ? new Set()
                        : new Set(analysisResult.characters.map((item) => item.name))
                    ));
                  }}
                >
                  {analysisChecked.size === analysisResult.characters.length ? '全部取消' : '全部选中'}
                </button>
              </div>

              <div className="max-h-56 space-y-2 overflow-auto pr-1">
                {analysisResult.characters.map((item) => {
                  const checked = analysisChecked.has(item.name);
                  const exists = characters.some((character) => character.name === item.name);

                  return (
                    <label
                      key={item.name}
                      className={`flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2 ${
                        checked
                          ? 'border-primary-400 bg-primary-50 dark:bg-primary-900/20'
                          : 'border-surface-200 dark:border-surface-700'
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleAnalysisCharacter(item.name)}
                        className="mt-1"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium">{item.name}</span>
                          <span className="rounded bg-surface-100 px-1.5 py-0.5 text-[10px] text-surface-500 dark:bg-surface-800">
                            {ROLE_LABELS[item.role] || item.role}
                          </span>
                          {exists && (
                            <span className="rounded bg-success-soft px-1.5 py-0.5 text-[11px] text-success dark:bg-success-soft-dark dark:text-success-dark">
                              已存在，不覆盖
                            </span>
                          )}
                        </div>
                        <p className="mt-0.5 line-clamp-2 text-xs text-surface-500">{item.summary || EMPTY_TEXT}</p>
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>

            {previewCharacters.length >= 2 && (
              <div className="mb-4">
                <h4 className="mb-2 text-sm font-semibold">人物关系预览</h4>
                <div className="h-56 overflow-hidden rounded-lg border border-surface-200 dark:border-surface-700">
                  <CharacterRelationGraph
                    characters={previewCharacters}
                    selectedId={previewCharacters[0]?.id || ''}
                    onSelectCharacter={() => undefined}
                  />
                </div>
              </div>
            )}

            {analysisResult.relations.length > 0 && (
              <div className="mb-4">
                <h4 className="mb-2 text-sm font-semibold">人物关系（{analysisResult.relations.length} 条）</h4>
                <div className="max-h-44 space-y-1.5 overflow-auto pr-1">
                  {analysisResult.relations.map((relation, index) => {
                    const included = analysisChecked.has(relation.source) && analysisChecked.has(relation.target);
                    return (
                      <div
                        key={`${relation.source}-${relation.target}-${index}`}
                        className={`flex items-center gap-2 rounded-lg bg-surface-50 px-3 py-1.5 text-sm dark:bg-surface-800 ${
                          included ? '' : 'opacity-40'
                        }`}
                      >
                        <span className="font-medium">{relation.source}</span>
                        <span className="text-xs text-surface-400">→ {RELATION_LABELS[relation.type] || relation.type} →</span>
                        <span className="font-medium">{relation.target}</span>
                        <span className="ml-auto truncate pl-2 text-xs text-surface-400" title={relation.description}>
                          {relation.description || `第 ${relation.sinceChapter} 章起`}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {analysisError && <p className="mb-3 text-sm text-error">{analysisError}</p>}

            <div className="flex items-center justify-end gap-2 border-t border-surface-200 pt-3 dark:border-surface-700">
              <button onClick={closeAnalysisPanel} className="btn-secondary text-sm">取消</button>
              <button
                onClick={() => void handleImportAnalysis()}
                disabled={analysisChecked.size === 0}
                className="btn-primary text-sm"
                data-testid="import-analysis-button"
              >
                确认导入（{analysisChecked.size}）
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  ) : null;

  if (showCreateForm) {
    return (
      <>
        <div className="flex flex-1 items-center justify-center">
          <div className="card mx-4 w-full max-w-md">
            <h3 className="mb-4 flex items-center gap-2 text-lg font-semibold" data-testid="create-character-heading">
              <Sparkles size={18} className="text-primary-600" />
              新建角色
            </h3>

            <div className="space-y-3">
              <div>
                <label className="text-xs text-surface-500">角色定位</label>
                <select
                  className="input-field mt-1 w-full"
                  value={form.role}
                  onChange={(event) => setForm((previous) => ({ ...previous, role: event.target.value as Character['role'] }))}
                  data-testid="character-role-select"
                >
                  {Object.entries(ROLE_LABELS).map(([key, label]) => (
                    <option key={key} value={key}>{label}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="text-xs text-surface-500">AI 生成人设提示</label>
                <textarea
                  className="input-field mt-1 h-24 resize-none"
                  placeholder="例如：克制寡言的港口调查员，和主角有一段旧案牵连。"
                  value={form.hints}
                  onChange={(event) => setForm((previous) => ({ ...previous, hints: event.target.value }))}
                  data-testid="character-hints-input"
                />
              </div>

              <div className="text-center text-xs text-surface-400">
                不填提示词也可以，工作台会创建一张可手动编辑的空白角色卡。
              </div>

              <div>
                <label className="text-xs text-surface-500">姓名</label>
                <input
                  className="input-field mt-1 w-full"
                  value={form.name}
                  onChange={(event) => setForm((previous) => ({ ...previous, name: event.target.value }))}
                  data-testid="character-name-input"
                  placeholder="手动录入"
                />
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="text-xs text-surface-500">性别</label>
                  <input
                    className="input-field mt-1 w-full"
                    value={form.gender}
                    onChange={(event) => setForm((previous) => ({ ...previous, gender: event.target.value }))}
                    data-testid="character-gender-input"
                  />
                </div>
                <div>
                  <label className="text-xs text-surface-500">年龄</label>
                  <input
                    className="input-field mt-1 w-full"
                    type="number"
                    value={form.age}
                    onChange={(event) => setForm((previous) => ({ ...previous, age: Number(event.target.value) || 0 }))}
                    data-testid="character-age-input"
                  />
                </div>
                <div>
                  <label className="text-xs text-surface-500">身份</label>
                  <input
                    className="input-field mt-1 w-full"
                    value={form.identity}
                    onChange={(event) => setForm((previous) => ({ ...previous, identity: event.target.value }))}
                    data-testid="character-identity-input"
                  />
                </div>
              </div>

              {error && <p className="text-sm text-error">{error}</p>}
            </div>

            <div className="mt-4 flex gap-2">
              <button
                onClick={() => {
                  setShowCreateForm(false);
                  resetForm();
                }}
                className="btn-secondary flex-1 text-sm"
              >
                取消
              </button>
              <button
                onClick={() => void handleCreateCharacter()}
                disabled={isGenerating}
                className="btn-primary flex-1 text-sm"
                data-testid="character-submit-button"
              >
                {isGenerating ? '生成中...' : '创建角色'}
              </button>
            </div>
          </div>
        </div>
        {analysisPanel}
      </>
    );
  }

  if (characters.length === 0) {
    return (
      <>
        <div className="flex h-full flex-col">
          {showImportCandidates && <ImportCharacterCandidates {...importCandidateProps} />}
          <div className="flex min-h-0 flex-1 items-center justify-center">
          <div className="text-center">
            <Users size={48} className="mx-auto mb-4 text-surface-300" />
            <h3 className="mb-2 text-lg font-semibold">还没有角色</h3>
            <p className="mb-4 text-sm text-surface-500">
              在这里搭建人物阵容，后续大纲生成、正文续写和修订都会直接引用这些角色资料。
            </p>
            {!isEmpty && (
              <p className="mb-4 text-xs text-surface-400">
                你可以用 AI 提示快速生成角色，也可以先建立空白角色卡，后面慢慢补全。
              </p>
            )}
            <div className="flex flex-col items-center gap-2">
              <button
                onClick={() => setShowCreateForm(true)}
                className="btn-primary mx-auto flex items-center gap-1 text-sm"
                data-testid="create-character-button"
              >
                <Sparkles size={14} />
                创建角色
              </button>
              <button
                onClick={() => void handleAnalyzeCharacters()}
                disabled={isAnalyzing}
                className="btn-secondary mx-auto flex items-center gap-1 text-sm"
                data-testid="analyze-characters-empty-button"
              >
                <ScanSearch size={14} />
                {isAnalyzing ? 'AI 正在通读正文...' : '从正文一键分析人物'}
              </button>
            </div>
          </div>
          </div>
        </div>
        {analysisPanel}
      </>
    );
  }

  return (
    <>
      <div className="flex h-full flex-col">
        {showImportCandidates && <ImportCharacterCandidates {...importCandidateProps} />}
        <div className="flex min-h-0 flex-1">
          <div className="w-64 shrink-0 overflow-auto border-r border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-850">
          <div className="flex items-center justify-between border-b border-surface-200 p-3 dark:border-surface-700">
            <h3 className="text-sm font-semibold" data-testid="character-list-heading">角色列表</h3>
            <button
              onClick={() => setShowCreateForm(true)}
              className="btn-ghost p-1"
              data-testid="create-character-inline-button"
            >
              <Plus size={16} />
            </button>
          </div>

          <div className="py-1">
            {characters.map((character) => (
              <button
                key={character.id}
                onClick={() => setSelectedId(character.id)}
                className={`w-full border-l-2 px-3 py-2.5 text-left transition-colors ${
                  selected?.id === character.id
                    ? 'border-primary-600 bg-primary-50 dark:bg-primary-900/20'
                    : 'border-transparent hover:bg-surface-100 dark:hover:bg-surface-750'
                }`}
                data-testid={`character-card-${character.id}`}
              >
                <div className="flex items-center gap-3">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary-100 text-sm font-bold text-primary-700 dark:bg-primary-900/40 dark:text-primary-400">
                    {character.name[0]}
                  </div>
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{character.name}</div>
                    <div className="text-xs text-surface-400">{ROLE_LABELS[character.role]}</div>
                  </div>
                </div>
              </button>
            ))}
          </div>
        </div>

        {selected && (
          <div className="flex-1 overflow-auto">
            <div className="mx-auto max-w-2xl p-8">
              <div className="mb-6 flex items-start justify-between gap-4">
                <div className="flex items-center gap-4">
                  <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-full bg-primary-100 text-2xl font-bold text-primary-700 dark:bg-primary-900/40 dark:text-primary-400">
                    {selected.name[0]}
                  </div>
                  {isEditingProfile && draft ? (
                    <div className="space-y-1">
                      <input
                        className="input-field w-56 text-lg font-semibold"
                        maxLength={20}
                        value={draft.name}
                        onChange={(event) => setDraftField('name', event.target.value)}
                        data-testid="profile-field-name"
                      />
                      <select
                        className="input-field w-56 text-xs"
                        value={draft.role}
                        onChange={(event) => setDraftField('role', event.target.value as Character['role'])}
                        data-testid="profile-field-role"
                      >
                        {Object.entries(ROLE_LABELS).map(([key, label]) => (
                          <option key={key} value={key}>{label}</option>
                        ))}
                      </select>
                    </div>
                  ) : (
                    <div>
                      <h2 className="text-2xl font-bold" data-testid="character-detail-name">{selected.name}</h2>
                      <p className="text-sm text-surface-500" data-testid="character-detail-identity">
                        {ROLE_LABELS[selected.role]} / {selected.basicInfo.identity || EMPTY_TEXT}
                      </p>
                      {(selected.aliases || []).length > 0 && (
                        <p className="mt-0.5 text-xs text-surface-400">别名：{(selected.aliases || []).join('、')}</p>
                      )}
                    </div>
                  )}
                </div>

                <div className="flex flex-wrap items-center justify-end gap-2">
                  {isEditingProfile ? (
                    <>
                      <button
                        type="button"
                        onClick={() => void handleAnalyzeProfile()}
                        disabled={isAnalyzingProfile}
                        className="btn-secondary flex items-center gap-1 text-sm"
                        data-testid="character-ai-analyze"
                      >
                        <ScanSearch size={14} />
                        {isAnalyzingProfile ? '分析中...' : 'AI 分析正文与大纲'}
                      </button>
                      {pendingSuggestionCount > 1 && (
                        <button
                          type="button"
                          onClick={handleAcceptAllSuggestions}
                          className="btn-ghost text-sm"
                          data-testid="character-ai-accept-all"
                        >
                          全部采用（{pendingSuggestionCount}）
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => void handleSaveProfile()}
                        className="btn-primary text-sm"
                        data-testid="character-save"
                      >
                        保存
                      </button>
                      <button
                        type="button"
                        onClick={handleCancelProfileEdit}
                        className="btn-secondary text-sm"
                        data-testid="character-cancel"
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={handleStartProfileEdit}
                        className="btn-primary flex items-center gap-1 text-sm"
                        data-testid="character-edit-button"
                      >
                        <Pencil size={14} />
                        编辑角色
                      </button>
                      <button
                        onClick={() => void handleAnalyzeCharacters()}
                        disabled={isAnalyzing}
                        className="btn-secondary flex items-center gap-1 text-sm"
                        data-testid="analyze-characters-button"
                      >
                        <ScanSearch size={14} />
                        {isAnalyzing ? '分析中...' : '一键分析作品人物'}
                      </button>
                      <button onClick={() => setShowCreateForm(true)} className="btn-secondary flex items-center gap-1 text-sm">
                        <Sparkles size={14} />
                        新建角色
                      </button>
                    </>
                  )}
                </div>
              </div>

              {isEditingProfile && (
                <div className="mb-6 space-y-2">
                  {isAnalyzingProfile && (
                    <p className="text-xs text-surface-400" data-testid="character-ai-status">
                      AI 正在通读正文与大纲，作品较长时可能需要 20 到 60 秒，请稍候。
                    </p>
                  )}

                  {aiError && (
                    <div
                      className="rounded-lg border border-error/40 bg-error-soft/40 px-3 py-2 text-xs text-error"
                      data-testid="character-ai-error"
                    >
                      <p>{aiError}</p>
                      <button
                        type="button"
                        onClick={() => void handleAnalyzeProfile()}
                        className="mt-1 underline"
                        data-testid="character-ai-retry"
                      >
                        重试
                      </button>
                    </div>
                  )}

                  {suggestions && (
                    <div
                      className="rounded-lg border border-primary-200 bg-primary-50/60 px-3 py-2 text-[11px] text-surface-600 dark:border-primary-900 dark:bg-primary-900/20 dark:text-surface-300"
                      data-testid="character-ai-summary"
                    >
                      <p>
                        AI 给出 {pendingSuggestionCount} 项建议
                        {suggestions.basis && (
                          <>
                            （依据：{suggestions.basis.chapterCount} 章正文
                            {suggestions.basis.hasOutline ? ' · 含大纲' : ' · 暂无大纲'}）
                          </>
                        )}
                        。建议不会自动覆盖你写的内容，需要逐项「采用」后再保存。
                      </p>
                      {Object.keys(suggestions.rejected).length > 0 && (
                        <p className="mt-1" data-testid="character-ai-rejected">
                          以下字段的建议无法使用（格式不合法），已忽略：{fieldLabels(Object.keys(suggestions.rejected))}
                        </p>
                      )}
                      {suggestions.omitted.length > 0 && (
                        <p className="mt-1" data-testid="character-ai-omitted">
                          正文与大纲证据不足、AI 未给建议：{fieldLabels(suggestions.omitted)}
                        </p>
                      )}
                      <button
                        type="button"
                        onClick={() => patchSuggestions(profileKey, () => null)}
                        className="mt-1 underline"
                        data-testid="character-ai-dismiss-all"
                      >
                        全部忽略
                      </button>
                    </div>
                  )}

                  {saveError && (
                    <p className="text-xs text-error" data-testid="character-save-error">{saveError}</p>
                  )}
                </div>
              )}

              <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
                {PROFILE_SECTIONS.map((section) => (
                  <section
                    key={section.title}
                    className={`card ${section.span === 2 ? 'md:col-span-2' : ''}`}
                    data-testid={`profile-section-${section.fields[0].key}`}
                  >
                    <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                      {section.title === '基础信息' && <User size={16} className="text-primary-600" />}
                      {section.title === '性格倾向' && <Heart size={16} className="text-primary-600" />}
                      {section.title}
                    </h3>

                    {!isEditingProfile || !draft ? (
                      <div className={section.span === 2 ? 'grid grid-cols-1 gap-3 text-sm md:grid-cols-2' : 'space-y-2 text-sm'}>
                        {section.fields.map((field) => (
                          <div key={field.key}>
                            <span className="text-xs font-medium text-surface-400">{field.label}</span>
                            <p className="mt-1 whitespace-pre-wrap" data-testid={`profile-value-${field.key}`}>
                              {readProfileValue(selected, field.key) || EMPTY_TEXT}
                            </p>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className={section.span === 2 ? 'grid grid-cols-1 gap-3 text-sm md:grid-cols-2' : 'space-y-3 text-sm'}>
                        {section.fields.map((field) => {
                          const value = (draft[field.key] as string) || '';
                          const suggestion = suggestions?.fields[field.key];
                          const inputId = `profile-input-${selected.id}-${field.key}`;
                          return (
                            <div key={field.key}>
                              <label htmlFor={inputId} className="text-xs font-medium text-surface-400">
                                {field.label}
                              </label>
                              {field.multiline ? (
                                <textarea
                                  id={inputId}
                                  className="input-field mt-1 h-20 w-full resize-none text-sm"
                                  maxLength={field.max}
                                  placeholder={field.placeholder}
                                  value={value}
                                  onChange={(event) => setDraftField(field.key as keyof CharacterDraft, event.target.value)}
                                  data-testid={`profile-field-${field.key}`}
                                />
                              ) : (
                                <input
                                  id={inputId}
                                  className="input-field mt-1 w-full text-sm"
                                  maxLength={field.max}
                                  placeholder={field.placeholder}
                                  value={value}
                                  onChange={(event) => setDraftField(field.key as keyof CharacterDraft, event.target.value)}
                                  data-testid={`profile-field-${field.key}`}
                                />
                              )}
                              <div className="mt-0.5 text-[11px] text-surface-400" data-testid={`profile-count-${field.key}`}>
                                {value.length}/{field.max}
                              </div>

                              {suggestion !== undefined && (
                                <div
                                  className="mt-1.5 rounded-lg border border-primary-200 bg-primary-50/60 p-2 dark:border-primary-900 dark:bg-primary-900/20"
                                  data-testid={`profile-suggestion-${field.key}`}
                                >
                                  <div className="mb-1 flex items-center gap-2">
                                    <span className="text-[11px] font-semibold text-primary-700 dark:text-primary-400">
                                      AI 建议
                                    </span>
                                    {INFERRED_PROFILE_KEYS.has(field.key) && (
                                      <span className="rounded bg-surface-200 px-1 py-0.5 text-[10px] text-surface-600 dark:bg-surface-700 dark:text-surface-200">
                                        推断
                                      </span>
                                    )}
                                  </div>
                                  <p className="text-sm leading-relaxed" data-testid={`profile-suggestion-${field.key}-text`}>
                                    {suggestion}
                                  </p>
                                  <div className="mt-1.5 flex gap-2">
                                    <button
                                      type="button"
                                      onClick={() => handleAcceptSuggestion(field.key)}
                                      className="btn-primary text-xs"
                                      data-testid={`profile-suggestion-${field.key}-accept`}
                                    >
                                      采用
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => handleIgnoreSuggestion(field.key)}
                                      className="btn-secondary text-xs"
                                      data-testid={`profile-suggestion-${field.key}-ignore`}
                                    >
                                      忽略
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </section>
                ))}

                {characters.length >= 2 && (
                  <section className="card md:col-span-2">
                    <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                      <Users size={16} className="text-primary-600" />
                      人物关系网络
                    </h3>
                    <CharacterRelationGraph
                      characters={characters}
                      selectedId={selectedId}
                      onSelectCharacter={(id) => setSelectedId(id)}
                    />
                  </section>
                )}

                {(selected.relations || []).length > 0 && (
                  <section className="card md:col-span-2">
                    <h3 className="mb-3 text-sm font-semibold">人物关系</h3>
                    <div className="space-y-2">
                      {(selected.relations || []).map((relation, index) => {
                        const target = characters.find((item) => item.id === relation.targetCharacterId);
                        return (
                          <div key={`${relation.targetCharacterId}-${index}`} className="flex items-center gap-3 rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
                            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-surface-200 text-xs font-bold dark:bg-surface-700">
                              {target?.name?.[0] || '?'}
                            </div>
                            <div className="flex-1">
                              <span className="text-sm font-medium">{target?.name || '未知角色'}</span>
                              <span className="ml-2 text-xs text-surface-400">{RELATION_LABELS[relation.type]}</span>
                            </div>
                            <span className="text-xs text-surface-400">{relation.description || EMPTY_TEXT}</span>
                          </div>
                        );
                      })}
                    </div>
                  </section>
                )}
              </div>
            </div>
          </div>
        )}
        </div>
      </div>
      {analysisPanel}
    </>
  );
}
