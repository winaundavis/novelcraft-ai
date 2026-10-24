import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  buildFallbackOutline,
  normalizeOutline,
  looksGarbled,
  normalizeChatReply,
  normalizeProject,
  normalizeCharacter,
  normalizePersonality,
  normalizeStyleProfile,
  normalizeStyleTransition,
  normalizeForeshadow,
  normalizeForeshadowSuggestion,
  normalizeForeshadowListData,
} from '../src/services/normalize.ts';
import { canRewriteSelection } from '../src/components/chat/copilotGuards.ts';
import { chatFailureMessage, outlineFailureMessage } from '../src/components/chat/outlineFailureMessage.ts';
import { summarizeOutlineDiff } from '../src/components/chat/outlineCandidateDiff.ts';

function createProject(overrides = {}) {
  return {
    id: 'project-1',
    name: '星界守夜人',
    subtitle: '一部关于记忆与星图的小说',
    genre: 'scifi',
    targetWordCount: 100000,
    currentWordCount: 0,
    status: 'planning',
    createdAt: '2026-08-08',
    updatedAt: '2026-08-08',
    volumes: [],
    characters: [],
    locations: [],
    worldRules: [],
    styleConfig: {
      id: 'style-1',
      name: 'Epic',
      avgSentenceLength: 24,
      dialogueRatio: 0.3,
      descriptionDensity: 0.6,
      paragraphLength: 'long',
      tone: 'grand',
      customPrompt: '',
    },
    outline: null,
    tags: ['群像', '星图'],
    isStarred: false,
    premise: '一名档案员追查被改写的过去。',
    targetChapters: 12,
    importAnalysis: null,
    inspirationFragments: [],
    ...overrides,
  };
}

test('outline failure message explains safe stage and trace id', () => {
  assert.equal(
    outlineFailureMessage('outline_json_missing', 'audit-123'),
    '模型已返回，但没有形成结构化大纲修改。诊断编号：audit-123',
  );
  assert.equal(
    outlineFailureMessage('node_reference_ambiguous', ''),
    '模型返回的卷或剧情弧无法唯一对应当前大纲，本次未应用任何修改。',
  );
});

test('outline provider timeout is not mislabeled as a safety validation failure', () => {
  assert.equal(
    chatFailureMessage({
      intent: 'outline_edit',
      mode: 'failed',
      error_code: 'provider_timeout',
      trace_id: '6b0c38015a67',
      reply: '模型服务响应超时，本次未自动重试，请稍后手动重试。',
    }),
    '模型服务响应超时，本次未自动重试，请稍后手动重试。诊断编号：6b0c38015a67',
  );
});

test('outline candidate diff exposes added arcs, removed arcs, edits, and chapter moves', () => {
  const sections = summarizeOutlineDiff({
    added: [{ id: 'a-new', type: 'arc', title: '怪异早餐', order: 1 }],
    removed: [{ id: 'a-old', type: 'arc', title: '旧主线弧', order: 1 }],
    changed: [{ id: 'v1', type: 'volume', title: '第一卷', fields: ['summary'] }],
    moved: [{ id: 'c1', type: 'chapter', title: '第1章', from: { arcId: 'a-old' }, to: { arcId: 'a-new' } }],
  });

  assert.deepEqual(sections.map((section) => [section.key, section.count]), [
    ['added', 1], ['removed', 1], ['changed', 1], ['moved', 1],
  ]);
  assert.match(sections[0].items[0], /新增剧情弧.*怪异早餐/);
  assert.match(sections[1].items[0], /移除剧情弧.*旧主线弧/);
  assert.match(sections[2].items[0], /修改卷.*简介/);
  assert.match(sections[3].items[0], /调整章节归属.*第1章/);
});

test('looksGarbled only flags clearly broken text', () => {
  assert.equal(looksGarbled('伏线调试'), false);
  assert.equal(looksGarbled('导师留下了未寄出的信。'), false);
  assert.equal(looksGarbled('debug-hook'), false);

  assert.equal(looksGarbled('????'), true);
  assert.equal(looksGarbled('浣犲ソ'), true);
  assert.equal(looksGarbled('Ã¨Â¯Â·Ã¦Â±Â'), true);
});

test('normalizeProject keeps valid inspiration fragments', () => {
  const project = createProject({
    inspirationFragments: [
      {
        id: 'fragment-1',
        projectId: 'project-1',
        title: '旧车站的信',
        content: '主角在旧车站的储物柜里找到导师留下的信。',
        type: 'plot',
        tags: ['伏笔', '导师'],
        createdAt: '2026-08-08T00:00:00.000Z',
        isConverted: false,
        source: 'manual',
      },
    ],
  });

  const normalized = normalizeProject(project);

  assert.equal(normalized.name, '星界守夜人');
  assert.equal(normalized.inspirationFragments?.length, 1);
  assert.equal(normalized.inspirationFragments?.[0].title, '旧车站的信');
  assert.equal(normalized.inspirationFragments?.[0].content, '主角在旧车站的储物柜里找到导师留下的信。');
  assert.deepEqual(normalized.inspirationFragments?.[0].tags, ['伏笔', '导师']);
});

test('normalizeProject drops garbled inspiration fragments and tags', () => {
  const project = createProject({
    inspirationFragments: [
      {
        id: 'fragment-bad',
        projectId: 'project-1',
        title: '浣犲ソ',
        content: '浣犲ソ',
        type: 'plot',
        tags: ['浣犲ソ', '导师'],
        createdAt: '2026-08-08T00:00:00.000Z',
        isConverted: false,
        source: 'manual',
      },
      {
        id: 'fragment-good',
        projectId: 'project-1',
        title: '保留条目',
        content: '这里是一条正常的灵感碎片。',
        type: 'idea',
        tags: ['灵感', '场景'],
        createdAt: '2026-08-08T00:00:01.000Z',
        isConverted: false,
        source: 'brainstorm',
      },
    ],
    tags: ['群像', '浣犲ソ'],
  });

  const normalized = normalizeProject(project);

  assert.deepEqual(normalized.tags, ['群像']);
  assert.equal(normalized.inspirationFragments?.length, 1);
  assert.equal(normalized.inspirationFragments?.[0].title, '保留条目');
});

test('normalizeOutline replaces repeated volume summaries with distinct stage descriptions', () => {
  const repeated = '围绕主线冲突展开，逐步推进人物关系和剧情。';
  const outline = {
    id: 'outline-1',
    projectId: 'project-1',
    structure: 'three_act',
    premise: '祈悦死后，父母在悔恨中重新理解女儿。',
    mainSummary: '一个家庭在无法挽回的失去之后面对责任与迟来的爱。',
    nodes: [
      { id: 'v1', parentId: null, type: 'volume', title: '第一卷', summary: repeated, conflict: '', revelation: '', emotion: '', isKeyPoint: false, order: 1, targetWordCount: 10000, povCharacterId: null },
      { id: 'c1', parentId: 'v1', type: 'chapter', title: '第一章', summary: '严厉惩罚把祈悦推向死亡边缘。', conflict: '', revelation: '', emotion: '', isKeyPoint: false, order: 1, targetWordCount: 2000, povCharacterId: null },
      { id: 'v2', parentId: null, type: 'volume', title: '第二卷', summary: repeated, conflict: '', revelation: '', emotion: '', isKeyPoint: false, order: 2, targetWordCount: 10000, povCharacterId: null },
      { id: 'c2', parentId: 'v2', type: 'chapter', title: '第二章', summary: '父母在医院确认失去女儿，辩解开始崩塌。', conflict: '', revelation: '', emotion: '', isKeyPoint: false, order: 1, targetWordCount: 2000, povCharacterId: null },
      { id: 'v3', parentId: null, type: 'volume', title: '第三卷', summary: repeated, conflict: '', revelation: '', emotion: '', isKeyPoint: true, order: 3, targetWordCount: 10000, povCharacterId: null },
      { id: 'c3', parentId: 'v3', type: 'chapter', title: '第三章', summary: '遗留的信让父母面对祈悦真正的愿望。', conflict: '', revelation: '', emotion: '', isKeyPoint: true, order: 1, targetWordCount: 2000, povCharacterId: null },
    ],
  };

  const normalized = normalizeOutline(outline, {
    premise: outline.premise,
    structure: outline.structure,
    targetWordCount: 30000,
    targetChapters: 3,
  });
  const summaries = normalized.nodes.filter((node) => node.type === 'volume').map((node) => node.summary);

  assert.equal(new Set(summaries).size, 3);
  assert.match(summaries[0], /严厉惩罚/);
  assert.match(summaries[1], /医院确认/);
  assert.match(summaries[2], /遗留的信/);
});

test('fallback outline gives each volume a visibly distinct purpose without repeating the premise', () => {
  const premise = '梧渡发现所有人都把越来越荒诞的世界当成正常现象。';
  const outline = normalizeOutline(buildFallbackOutline({
    premise,
    structure: 'three_act',
    targetWordCount: 90000,
    targetChapters: 18,
  }), {
    premise,
    structure: 'three_act',
    targetWordCount: 90000,
    targetChapters: 18,
  });
  const summaries = outline.nodes.filter((node) => node.type === 'volume').map((node) => node.summary);

  assert.match(summaries[0], /开端/);
  assert.match(summaries[1], /深化/);
  assert.match(summaries[2], /收束/);
  assert.equal(summaries.filter((summary) => summary.includes(premise)).length, 1);
});

test('fallback outline never repeats the same conflict/revelation/emotion on every volume', () => {
  const build = (targetChapters) => buildFallbackOutline({
    premise: '梧渡发现所有人都把越来越荒诞的世界当成正常现象。',
    structure: 'three_act',
    targetWordCount: 90000,
    targetChapters,
  }).nodes.filter((node) => node.type === 'volume');

  for (const targetChapters of [18, 6]) {
    const volumes = build(targetChapters);
    assert.ok(volumes.length >= 2);
    for (const key of ['conflict', 'revelation', 'emotion']) {
      const values = volumes.map((volume) => volume[key]);
      assert.ok(values.every((value) => typeof value === 'string' && value.trim()), `${key} 不能为空`);
      assert.equal(new Set(values).size, values.length, `${key} 每卷都必须不同：${values.join(' | ')}`);
    }
    const flattened = volumes.flatMap((volume) => [volume.conflict, volume.revelation, volume.emotion]);
    assert.ok(!flattened.includes('主线冲突持续升级。'), '不能回到旧的模板句');
    assert.ok(!flattened.includes('张力上升'), '不能回到旧的模板句');
  }
});

test('normalizeOutline replaces volume summaries that repeat the same long premise', () => {
  const premise = '梧渡发现所有人都把越来越荒诞的世界当成正常现象，只有他记得原来的规则。';
  const outline = buildFallbackOutline({
    premise,
    structure: 'three_act',
    targetWordCount: 90000,
    targetChapters: 18,
  });
  outline.nodes.filter((node) => node.type === 'volume').forEach((node, index) => {
    node.summary = `本卷围绕“${premise}”展开，阶段目标${index + 1}。其余说明保持相同。`;
  });

  const normalized = normalizeOutline(outline, {
    premise,
    structure: 'three_act',
    targetWordCount: 90000,
    targetChapters: 18,
  });
  const summaries = normalized.nodes.filter((node) => node.type === 'volume').map((node) => node.summary);

  assert.match(summaries[0], /开端/);
  assert.match(summaries[1], /深化/);
  assert.match(summaries[2], /收束/);
  assert.equal(summaries.filter((summary) => summary.includes(premise)).length, 1);
});

test('normalizeOutline repairs JSON fragments and later-volume titles inside chapter summaries', () => {
  const outline = buildFallbackOutline({
    premise: '梧渡发现世界规则正在崩坏。',
    structure: 'three_act',
    targetWordCount: 90000,
    targetChapters: 18,
  });
  const volumes = outline.nodes.filter((node) => node.type === 'volume');
  const firstChapters = outline.nodes.filter((node) => node.type === 'chapter' && node.parentId === volumes[0].id);
  firstChapters[0].summary = '"volumes": [';
  firstChapters[1].summary = '"title": "第二卷：迷雾追踪",';
  firstChapters[2].summary = '}';

  const normalized = normalizeOutline(outline, {
    premise: outline.premise,
    structure: outline.structure,
    targetWordCount: 90000,
    targetChapters: 18,
  });
  const repaired = normalized.nodes.filter((node) => node.type === 'chapter' && node.parentId === volumes[0].id);

  assert.equal(repaired.some((node) => /第二卷|"volumes"|"title"|^[{}]$/.test(node.summary)), false);
  assert.equal(repaired.slice(0, 3).every((node) => node.summary.includes(volumes[0].title)), true);
});

test('normalizeOutline preserves legacy chapter volume ownership without inventing plot arcs', () => {
  const legacy = buildFallbackOutline({
    premise: '梧渡发现世界规则正在崩坏。',
    structure: 'three_act',
    targetWordCount: 90000,
    targetChapters: 20,
  });
  legacy.nodes = legacy.nodes.filter((node) => node.type !== 'arc').map((node) => {
    if (node.type !== 'chapter') return node;
    const { arcId: _arcId, ...chapter } = node;
    return chapter;
  });

  const normalized = normalizeOutline(legacy, {
    premise: legacy.premise,
    structure: legacy.structure,
    targetWordCount: 90000,
    targetChapters: 20,
  });
  const arcs = normalized.nodes.filter((node) => node.type === 'arc');
  const chapters = normalized.nodes.filter((node) => node.type === 'chapter');

  assert.equal(arcs.length, 0);
  assert.equal(chapters.every((chapter) => chapter.parentId?.startsWith('volume-')), true);
  assert.equal(chapters.every((chapter) => !chapter.arcId), true);
});

test('normalizeOutline keeps legacy volume chapters flat instead of inventing a generic main arc', () => {
  const legacy = buildFallbackOutline({
    premise: '祈悦死后，父母必须面对自己曾经施加的伤害。',
    structure: 'three_act',
    targetWordCount: 30000,
    targetChapters: 12,
  });
  legacy.nodes = legacy.nodes.filter((node) => node.type !== 'arc').map((node) => {
    if (node.type !== 'chapter') return node;
    const { arcId: _arcId, ...chapter } = node;
    return chapter;
  });
  const originalChapterIds = legacy.nodes
    .filter((node) => node.type === 'chapter')
    .map((node) => node.id);

  const normalized = normalizeOutline(legacy, {
    premise: legacy.premise,
    structure: legacy.structure,
    targetWordCount: 30000,
    targetChapters: 12,
  });

  assert.equal(normalized.nodes.some((node) => node.title === '本卷主线弧'), false);
  assert.deepEqual(
    normalized.nodes.filter((node) => node.type === 'chapter').map((node) => node.id),
    originalChapterIds,
  );
});

test('fallback outline leaves chapters directly under their volumes', () => {
  const outline = normalizeOutline(buildFallbackOutline({
    premise: '祈悦死后，父母必须面对自己曾经施加的伤害。',
    structure: 'three_act',
    targetWordCount: 30000,
    targetChapters: 12,
  }), {
    premise: '祈悦死后，父母必须面对自己曾经施加的伤害。',
    structure: 'three_act',
    targetWordCount: 30000,
    targetChapters: 12,
  });
  const volumes = outline.nodes.filter((node) => node.type === 'volume');
  const arcs = outline.nodes.filter((node) => node.type === 'arc');

  const volumeIds = new Set(volumes.map((volume) => volume.id));
  const chapters = outline.nodes.filter((node) => node.type === 'chapter');

  assert.equal(arcs.length, 0);
  assert.equal(chapters.every((chapter) => volumeIds.has(chapter.parentId)), true);
  assert.equal(chapters.every((chapter) => !chapter.arcId), true);
});

test('outline UI presents collapsible arcs and does not render chapter summaries', () => {
  const generatorSource = readFileSync(new URL('../src/components/outline/OutlineGenerator.tsx', import.meta.url), 'utf8');
  assert.match(generatorSource, /expandedArcs/);
  assert.match(generatorSource, /arc\.summary/);
  assert.doesNotMatch(generatorSource, /chapter\.summary/);
});

test('chapter navigation does not render a second saved-chapters section', () => {
  const treeSource = readFileSync(new URL('../src/components/outline/ChapterTree.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(treeSource, /已保存章节/);
  assert.doesNotMatch(treeSource, /chaptersToTree/);
});

test('outline requests allow planning models longer than the generic fast timeout', () => {
  const apiSource = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
  assert.match(apiSource, /const OUTLINE_TIMEOUT_MS = 240_000/);
  assert.match(apiSource, /'\/ai\/generate-outline'[\s\S]*?OUTLINE_TIMEOUT_MS/);
});

test('degraded outline results cannot replace the saved project outline', () => {
  const generatorSource = readFileSync(new URL('../src/components/outline/OutlineGenerator.tsx', import.meta.url), 'utf8');
  const fallbackGuard = generatorSource.indexOf("json.meta?.mode !== 'llm'");
  const outlineMutation = generatorSource.indexOf('setOutline(nextOutline)');
  const projectMutation = generatorSource.indexOf('projectApi.update(projectId');

  assert.notEqual(fallbackGuard, -1);
  assert.ok(fallbackGuard < outlineMutation);
  assert.ok(fallbackGuard < projectMutation);
  assert.match(generatorSource, /json\.meta\?\.mode !== 'llm'[\s\S]*?return;/);
  assert.match(generatorSource, /json\.meta\?\.mode !== 'llm'[\s\S]*?setOutline\(project\?\.outline \|\| null\)[\s\S]*?return;/);
});

test('project workspace normalizes fetched outline before updating every workspace consumer', () => {
  const workspaceSource = readFileSync(new URL('../src/pages/ProjectWorkspace.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(
    workspaceSource,
    /if \(projects\.some\(\(project\) => project\.id === projectId\)\) \{[\s\S]*?return;[\s\S]*?\}/,
  );
  assert.match(workspaceSource, /const normalizedProject = normalizeProject\(json\.data\)/);
  assert.match(workspaceSource, /setOutlineData\(normalizedProject\.outline \|\| null\)/);
});

test('normalizeChatReply falls back when reply is mojibake', () => {
  const reply = normalizeChatReply('浣犲ソ');
  assert.notEqual(reply, '浣犲ソ');
  assert.match(reply, /本地|建议|下一章/);
});

test('copilot rewrite requires an explicit text selection', () => {
  assert.equal(canRewriteSelection(''), false);
  assert.equal(canRewriteSelection('   '), false);
  assert.equal(canRewriteSelection('一小段正文'), true);
});

test('outline assistant uses planning timeout, preserves API errors, and refreshes project navigation', () => {
  const apiSource = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
  const chatSource = readFileSync(new URL('../src/components/chat/AIChatPanel.tsx', import.meta.url), 'utf8');
  const workspaceSource = readFileSync(new URL('../src/pages/ProjectWorkspace.tsx', import.meta.url), 'utf8');

  assert.match(apiSource, /CHAT_PLANNING_TIMEOUT_MS/);
  assert.match(apiSource, /const CHAT_PLANNING_TIMEOUT_MS = 210_000/);
  assert.doesNotMatch(apiSource, /emitDegradedNotice\('chat'\)/);
  assert.match(chatSource, /outline_edit/);
  assert.match(workspaceSource, /novelcraft:project-refresh/);
});

test('outline assistant restores a proposed candidate after refresh', () => {
  const apiSource = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
  const chatSource = readFileSync(new URL('../src/components/chat/AIChatPanel.tsx', import.meta.url), 'utf8');

  assert.match(apiSource, /outlineCandidates:\s*\{[\s\S]*list:/);
  assert.match(chatSource, /outlineCandidates\.list\(projectId, 'proposed'\)/);
});

test('comprehensive review waits longer than two sequential check calls', () => {
  const apiSource = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');

  // 综合审校 = 语言审校 + 逻辑审校两次串行 LLM 调用；后端 check 角色单次上限 90s。
  // 前端截止时间必须高于 2×90s，否则浏览器先中断，接口还在成功返回。
  assert.match(apiSource, /const REVIEW_TIMEOUT_MS = 210_000/);
  const reviewBlock = apiSource.slice(apiSource.indexOf('export const reviewApi'));
  assert.match(reviewBlock, /REVIEW_TIMEOUT_MS/);
  assert.doesNotMatch(reviewBlock, /FAST_AI_TIMEOUT_MS/);
  // 失败必须区分原因，不能一律报"AI 服务降级"。
  assert.match(apiSource, /isRateLimitedError|status === 429/);
  assert.match(apiSource, /degradedReason/);
});

// ── 角色性格倾向：五维打分/MBTI → 纯文字描述 ────────────────

function createLegacyCharacter(overrides = {}) {
  return {
    id: 'char-1',
    projectId: 'project-1',
    name: '林晚',
    aliases: [],
    role: 'protagonist',
    basicInfo: { age: 24, gender: '女', appearance: '黑色长发', identity: '调查员', occupation: '调查员' },
    // 旧结构：五维分数 + MBTI
    personality: { openness: 8, conscientiousness: 7, extraversion: 3, agreeableness: 5, neuroticism: 6, mbti: 'INTJ' },
    background: '背景经历',
    coreTrauma: '核心创伤',
    surfaceMotivation: '表层动机',
    deepDesire: '深层欲望',
    arc: '角色弧光',
    catchphrase: '口头禅',
    speechStyle: '说话风格',
    habits: '习惯动作',
    values: '价值观',
    relations: [],
    ...overrides,
  };
}

test('legacy five-factor personality is dropped instead of re-rendered as scores', () => {
  const character = normalizeCharacter(createLegacyCharacter());

  assert.deepEqual(character.personality, { description: '' });
  assert.equal('mbti' in character.personality, false);
  assert.equal('openness' in character.personality, false);
  assert.equal('neuroticism' in character.personality, false);
});

test('hand-written personality description survives normalization', () => {
  const character = normalizeCharacter(createLegacyCharacter({ personality: { description: '她习惯先观察再开口。' } }));

  assert.deepEqual(character.personality, { description: '她习惯先观察再开口。' });
});

test('garbled characters also drop the legacy personality shape', () => {
  const character = normalizeCharacter(createLegacyCharacter({ name: '濂冲', background: '浣犲ソ', basicInfo: { age: 24, gender: '女', appearance: '', identity: '濂冲', occupation: '' } }), '林晚');

  assert.deepEqual(character.personality, { description: '' });
  assert.equal('mbti' in character.personality, false);
});

test('normalizePersonality never throws on unknown legacy shapes', () => {
  assert.deepEqual(normalizePersonality(undefined), { description: '' });
  assert.deepEqual(normalizePersonality(null), { description: '' });
  assert.deepEqual(normalizePersonality({ mbti: 'INTJ', openness: 9 }), { description: '' });
  assert.deepEqual(normalizePersonality({ description: '  她话少  ' }), { description: '她话少' });
  assert.deepEqual(normalizePersonality({ description: 42 }), { description: '' });
});

test('profile analysis deadline exceeds the backend planning deadline', () => {
  const apiSource = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');

  // 后端用 planning 角色（180s）通读正文与大纲；前端截止时间必须更高，
  // 否则浏览器先中断，而后端其实还在正常返回。
  assert.match(apiSource, /const CHARACTER_PROFILE_TIMEOUT_MS = 200_000/);
  assert.match(apiSource, /analyze-character-profile/);
  const methodBlock = apiSource.slice(apiSource.indexOf('analyzeCharacterProfile:'));
  assert.match(methodBlock, /CHARACTER_PROFILE_TIMEOUT_MS/);
  // 失败必须降级成可解释的原因，而不是静默返回空建议
  assert.match(methodBlock, /degraded: true/);
  assert.match(apiSource, /describeCharacterProfileFailure/);
  assert.match(apiSource, /isRateLimitedError|status === 429/);
});

test('续写与审校共用同一个角色上下文构建入口（避免漏改一边）', () => {
  const editorSource = readFileSync(new URL('../src/components/editor/MarkdownEditor.tsx', import.meta.url), 'utf8');
  const reviewSource = readFileSync(new URL('../src/components/review/ReviewPanel.tsx', import.meta.url), 'utf8');
  const contextSource = readFileSync(new URL('../src/services/characterContext.ts', import.meta.url), 'utf8');

  assert.match(editorSource, /buildCharacterContexts\(project\?\.characters\)/);
  assert.match(reviewSource, /characters: buildCharacterContexts\(project\?\.characters\)/);
  // 以前未进入任何 AI 链路的字段，现在必须出现在上下文里，否则「填了没人用」
  for (const field of ['personality', 'background', 'coreTrauma', 'catchphrase', 'appearance', 'age', 'gender']) {
    assert.match(contextSource, new RegExp(`${field}:`), `角色上下文缺少字段 ${field}`);
  }
});

test('空值不造假对象：normalize* 返回 null，列表里的脏元素被丢弃', () => {  // `{} as T` 是"看着像 T、实际一个字段都没有"的真值，会通过调用方的 `if (json.data)`
  // 进入 state，再在读 strengths / 规则列表时抛错；渲染期异常会掀掉整棵 React 树变成白屏。
  assert.equal(normalizeStyleProfile(null), null);
  assert.equal(normalizeStyleProfile(undefined), null);
  assert.equal(normalizeStyleTransition(null), null);
  assert.equal(normalizeForeshadow(null), null);
  assert.equal(normalizeForeshadowSuggestion(undefined), null);

  // 列表里遇到脏元素应当丢弃，而不是造一条「未命名伏笔」幽灵记录
  const list = normalizeForeshadowListData({ items: [null, { name: '真伏笔' }, 'bad'] });
  assert.deepEqual(list.items.map((item) => item.name), ['真伏笔']);
});

test('文风画像的非法维度保持未知，不成为 React 对象子节点或伪造的指标', () => {
  const { dimensions } = normalizeStyleProfile({ dimensions: {
    avgSentenceLength: '42',
    dialogueRatio: Number.NaN,
    descriptionDensity: 0.4,
    paragraphLength: 'unknown',
    tone: { label: '被污染的对象' },
  } });

  assert.deepEqual(dimensions, {
    avgSentenceLength: undefined,
    dialogueRatio: undefined,
    descriptionDensity: 0.4,
    paragraphLength: undefined,
    tone: '',
  });
});

test('没有采样维度的画像不显示虚构的零值或中段', () => {
  for (const dimensions of [undefined, null, {}, [], 'invalid']) {
    const profile = normalizeStyleProfile({ dimensions });
    assert.equal(profile.dimensions.avgSentenceLength, undefined);
    assert.equal(profile.dimensions.dialogueRatio, undefined);
    assert.equal(profile.dimensions.descriptionDensity, undefined);
    assert.equal(profile.dimensions.paragraphLength, undefined);
    assert.equal(profile.dimensions.tone, '');
  }
});

test('有效文风维度包括真实的零值均原样保留', () => {
  const dimensions = {
    avgSentenceLength: 24.5,
    dialogueRatio: 0,
    descriptionDensity: 0.4,
    paragraphLength: 'short',
    tone: '克制',
  };
  assert.deepEqual(normalizeStyleProfile({ dimensions }).dimensions, dimensions);
});

test('Markdown 全书导入的超时必须够大，且中断要说人话', () => {
  const apiSource = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
  const start = apiSource.indexOf('async function postMarkdownUpload');
  assert.ok(start > -1, '找不到 postMarkdownUpload');
  const block = apiSource.slice(start, apiSource.indexOf('export const importApi', start));

  // 扫描接口要对整本书跑 jieba 词性标注：实测 120 章约 47 秒。
  // 45 秒的上限会把「后端已经返回 200」的响应提前掐掉，作者只看到
  // `signal is aborted without reason`，看起来像导入失败。
  assert.match(apiSource, /const MARKDOWN_UPLOAD_TIMEOUT_MS = 300_000;/);
  assert.match(block, /MARKDOWN_UPLOAD_TIMEOUT_MS/);
  assert.doesNotMatch(block, /IMPORT_TIMEOUT_MS/);

  // 中断必须翻译成可执行的提示，而不是把 AbortError 的原文甩给作者
  assert.match(block, /AbortError/);
  assert.match(block, /导入分析超时/);
});

// ── 文风渐进过渡 v2 ───────────────────────────────────────
//
// `normalizeStyleTransition` 曾把 `plan` 重建为「只有 step/weight/steps」的旧结构。
// 后端开始下发 v2 计划（planId / revision / 源与目标快照 / 完成账本）后，
// 这些字段会在归一化时被静默丢掉 —— 前端于是看不到计划版本，
// 保存时也无法回传与服务端一致的 token。以下用例锁定这些字段必须存活。

function v2TransitionFixture(overrides = {}) {
  const snapshot = { profileId: 'profile-1', name: '原稿文风', status: 'completed', dimensions: {}, rules: {} };
  return {
    id: 'transition-1',
    projectId: 'p1',
    profileId: 'profile-1',
    pace: 'slow',
    progress: 0.06,
    intervalChapters: 10,
    totalSteps: 20,
    plan: {
      schemaVersion: 2,
      planId: 'plan-abc',
      revision: 3,
      targetChapters: 200,
      baselineChapterIds: ['c1'],
      excludedChapterIds: ['c9'],
      completedChapters: [{ chapterId: 'c2', weight: 0.005, commitId: 'x' }],
      sourceSnapshot: snapshot,
      targetSnapshot: { presetKey: 'epic', preset: {}, customPrompt: '更庄重' },
    },
    startedAt: '2026-09-28T00:00:00Z',
    updatedAt: '2026-09-28T00:00:00Z',
    schemaVersion: 2,
    legacyReadOnly: false,
    planId: 'plan-abc',
    revision: 3,
    targetChapters: 200,
    completedCount: 12,
    sourceSnapshot: snapshot,
    targetSnapshot: { presetKey: 'epic', preset: {}, customPrompt: '更庄重' },
    ...overrides,
  };
}

test('v2 计划不被重建成旧步骤结构，快照与完成账本原样保留', () => {
  const result = normalizeStyleTransition(v2TransitionFixture());

  assert.equal(result.planId, 'plan-abc');
  assert.equal(result.revision, 3);
  assert.equal(result.targetChapters, 200);
  assert.equal(result.completedCount, 12);
  assert.equal(result.schemaVersion, 2);
  assert.equal(result.legacyReadOnly, false);
  assert.deepEqual(result.plan.baselineChapterIds, ['c1']);
  assert.deepEqual(result.plan.excludedChapterIds, ['c9']);
  assert.equal(result.plan.completedChapters.length, 1);
  assert.equal(result.targetSnapshot.presetKey, 'epic');
  assert.equal(result.targetSnapshot.customPrompt, '更庄重');
  assert.equal(result.sourceSnapshot.name, '原稿文风');
  // 不得往 v2 计划里塞 step / steps 这类旧账本字段
  assert.equal(result.plan.step, undefined);
  assert.equal(result.plan.steps, undefined);
});

test('v1 旧计划保持只读，不虚构版本号或完成数', () => {
  const result = normalizeStyleTransition({
    id: 'transition-legacy',
    projectId: 'p1',
    pace: 'medium',
    progress: 0.4,
    intervalChapters: 6,
    totalSteps: 10,
    plan: { step: 4, weight: 0.4, start_chapter_count: 10, steps: [] },
    schemaVersion: 1,
    legacyReadOnly: true,
  });

  assert.equal(result.schemaVersion, 1);
  assert.equal(result.legacyReadOnly, true);
  assert.equal(result.planId, undefined);
  assert.equal(result.revision, undefined);
  assert.equal(result.completedCount, undefined);
  assert.equal(result.targetChapters, undefined);
  // 旧进度仍然可读，且不被折算成「已完成几章」
  assert.equal(result.progress, 0.4);
  assert.equal(result.plan.step, 4);
});

test('缺失 schemaVersion 的历史记录按 v1 处理，不猜成 v2', () => {
  const result = normalizeStyleTransition({ id: 't', projectId: 'p1', pace: 'fast', progress: 0 });
  assert.equal(result.schemaVersion, 1);
  assert.equal(result.legacyReadOnly, true);
  assert.equal(result.planId, undefined);
  assert.equal(result.revision, undefined);
});

test('pace 接受 custom，非法档位收敛为 medium 且不丢计划标识', () => {
  assert.equal(normalizeStyleTransition(v2TransitionFixture({ pace: 'custom' })).pace, 'custom');
  const fallback = normalizeStyleTransition(v2TransitionFixture({ pace: 'turbo' }));
  assert.equal(fallback.pace, 'medium');
  assert.equal(fallback.planId, 'plan-abc');
  assert.equal(fallback.revision, 3);
});

test('计划 token 必须成对下发，显式无计划与读取失败保持可区分', () => {
  const source = readFileSync(new URL('../src/services/stylePlanToken.ts', import.meta.url), 'utf8');
  const fields = source.slice(source.indexOf('export function stylePlanTokenFields'));
  // 后端 validate_plan_on_conn：只给 plan_id 或只给 revision 会被判为冲突（409）。
  assert.match(fields, /stylePlanId: token\.stylePlanId/);
  assert.match(fields, /stylePlanRevision: token\.stylePlanRevision/);
  // 只有没有可信 token 才省略字段；显式空 id/0 仍走成对字段路径。
  assert.match(fields, /if \(!token\) return \{\}/);
  assert.doesNotMatch(fields, /!token\.stylePlanId/);
});

test('续写 409 冲突不再注入本地兜底正文，且保留可执行提示', () => {
  const apiSource = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
  const start = apiSource.indexOf('continueWriting: (');
  assert.ok(start > -1, '找不到 continueWriting');
  const block = apiSource.slice(start, apiSource.indexOf('chat: (', start));

  // 冲突分支必须在注入降级文本之前 return
  const conflictReturn = block.indexOf('isStylePlanConflict(error)');
  const fallbackCall = block.indexOf('buildFallbackContinuation(');
  assert.ok(conflictReturn > -1, '缺少冲突分支');
  assert.ok(fallbackCall > -1, '不应移除其它错误的降级路径');
  assert.ok(conflictReturn < fallbackCall, '冲突分支必须先于降级注入返回');
  // 携带结构化错误码，面板据此显示可执行提示
  assert.match(block, /code: STYLE_PLAN_CONFLICT_CODE/);
  // SSE started 事件只走 onMetadata，不得当成正文
  assert.match(block, /parsed\.status === 'started'/);
});

test('文风过渡面板不把计划进度说成文学质量达标', () => {
  const panelSource = readFileSync(
    new URL('../src/components/import/StyleAnalysisPanel.tsx', import.meta.url),
    'utf8',
  );
  for (const banned of ['文风已达标', '已对齐目标风格', '已对齐目标预设']) {
    assert.ok(!panelSource.includes(banned), `面板出现夸大文案：${banned}`);
  }
  assert.match(panelSource, /计划已完成/);
});
