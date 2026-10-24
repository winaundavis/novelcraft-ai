/**
 * 独立 QA 证伪测试：角色档案（前端归一化 + 源码级不变量）。
 *
 * 与既有 normalize.test.mjs 的分工：既有用例验证正常/旧结构的主路径；
 * 本文件专门喂**畸形输入**（数组、对象、数字、原型污染…）并做
 * **源码级**不变量检查——AI 分析路径绝不允许写草稿、写角色卡或落盘。
 *
 * 运行：node --test tests/profile-falsify.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { normalizeCharacter, normalizePersonality, buildFallbackCharacter } from '../src/services/normalize.ts';

const PANEL_PATH = new URL('../src/components/characters/CharacterPanel.tsx', import.meta.url);
const panelSource = readFileSync(PANEL_PATH, 'utf8');

function sliceBetween(startMarker, endMarker) {
  const start = panelSource.indexOf(startMarker);
  assert.notEqual(start, -1, `源码里找不到标记：${startMarker}`);
  const end = panelSource.indexOf(endMarker, start);
  assert.notEqual(end, -1, `源码里找不到结束标记：${endMarker}`);
  return panelSource.slice(start, end);
}

function legacyCharacter(overrides = {}) {
  return {
    id: 'char-1',
    projectId: 'project-1',
    name: '林晚',
    aliases: [],
    role: 'protagonist',
    basicInfo: { age: 24, gender: '女', appearance: '黑色长发', identity: '调查员', occupation: '调查员' },
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

// ── 旧数据不让界面崩、不露打分 ────────────────────────────────

test('every legacy personality shape collapses to {description:""} with no extra keys', () => {
  const shapes = [
    { openness: 8, conscientiousness: 7, extraversion: 3, agreeableness: 5, neuroticism: 6, mbti: 'INTJ' },
    { mbti: 'INTJ' },
    { openness: 9 },
    { description: '她话少', mbti: 'INTJ', openness: 9 },
    { description: '  她话少  ' },
    'INTJ',
    42,
    true,
    ['INTJ'],
    [],
    { description: { text: '嵌套对象' } },
    { description: ['数组'] },
    { description: null },
    { description: undefined },
    { nested: { description: '嵌套里的描述' } },
    Object.create({ description: '原型上的描述' }),
  ];

  for (const shape of shapes) {
    const result = normalizePersonality(shape);
    assert.deepEqual(Object.keys(result), ['description'], `多余字段泄漏到界面：${JSON.stringify(Object.keys(result))}`);
    assert.equal(typeof result.description, 'string');
    assert.doesNotMatch(result.description, /INTJ|openness|外向度/, `打分/MBTI 内容泄漏：${result.description}`);
  }
});

test('legacy personality object is never spread back into the character', () => {
  const character = normalizeCharacter(legacyCharacter(), '');

  assert.deepEqual(Object.keys(character.personality), ['description']);
  assert.equal(character.personality.description, '');
  // 旧分数/MBTI 不允许以任何形式（含 JSON 序列化后）留在对象里
  assert.doesNotMatch(JSON.stringify(character.personality), /mbti|openness|neuroticism|INTJ/i);
});

test('garbled fallback path does not fabricate a personality description', () => {
  const character = normalizeCharacter(
    legacyCharacter({ name: '濂冲', background: '浣犲ソ', basicInfo: { age: 24, gender: '女', appearance: '', identity: '濂冲', occupation: '' } }),
    '林晚',
  );

  // 其它字段有占位兜底，性格倾向必须保持为空（不得由规则引擎拼造）
  assert.equal(character.personality.description, '');
  assert.ok(character.background.length > 0, '其它字段仍应有兜底文案');
});

test('buildFallbackCharacter never invents a personality description', () => {
  const fallback = buildFallbackCharacter({ projectId: 'p-1', role: 'protagonist', hints: '雨夜的调查员' });

  assert.equal(fallback.personality.description, '');
  assert.deepEqual(Object.keys(fallback.personality), ['description']);
});

test('normalizePersonality is total: exotic shapes never throw or fabricate', () => {
  const values = [
    Symbol.iterator,
    () => 'x',
    new Date(),
    new Map([['description', '映射描述']]),
    new Set(['描述']),
    { description: { toString: () => '她话少但记性极好' } },
    { description: Symbol('desc') },
  ];

  for (const value of values) {
    const result = normalizePersonality(value);
    assert.equal(typeof result.description, 'string');
    // Map/Set/Symbol 里的内容不得被字符串化后当成描述
    assert.ok(!result.description.includes('[object'), `对象被字符串化成描述：${result.description}`);
  }
});

// ── 源码级不变量：AI 分析不许写草稿 / 不许落盘 ────────────────

test('the analyze handler never touches the draft, the character list, or the server', () => {
  const body = sliceBetween('const handleAnalyzeProfile', 'const writeSuggestionToDraft');

  assert.doesNotMatch(body, /setDraftField/, '分析路径直接改了草稿');
  assert.doesNotMatch(body, /writeSuggestionToDraft/, '分析路径直接把建议写进了草稿');
  assert.doesNotMatch(body, /setCharacters/, '分析路径直接改了角色列表');
  assert.doesNotMatch(body, /persistCharacters|projectApi\.update/, '分析路径直接落了盘');
  // 它只允许写「建议态」与错误态
  assert.match(body, /setAiSuggestions\(/);
  assert.match(body, /setAiErrors\(/);
});

test('only the explicit accept handler may pipe AI values into the draft', () => {
  const acceptBody = sliceBetween('const handleAcceptSuggestion', 'const handleAcceptAllSuggestions');

  assert.match(acceptBody, /writeSuggestionToDraft\(key, value\)/);
  assert.doesNotMatch(acceptBody, /persistCharacters|projectApi\.update/, '「采用」步骤就落盘了，缺一次显式保存');
});

test('ignore / cancel paths never persist and never touch the draft', () => {
  const ignoreBody = sliceBetween('const handleIgnoreSuggestion', 'const handleSaveProfile');
  assert.doesNotMatch(ignoreBody, /persistCharacters|projectApi\.update|setDraftField/);

  const cancelBody = sliceBetween('const handleCancelProfileEdit', 'const handleAnalyzeProfile');
  assert.doesNotMatch(cancelBody, /persistCharacters|projectApi\.update/, '取消编辑时落盘了');

  const cancelIndex = panelSource.indexOf('data-testid="character-cancel"');
  assert.notEqual(cancelIndex, -1);
  assert.match(panelSource.slice(cancelIndex - 300, cancelIndex), /onClick=\{handleCancelProfileEdit\}/);
});

test('the save handler is the only place that persists the draft', () => {
  const saveBody = sliceBetween('const handleSaveProfile', 'const previewCharacters');

  assert.match(saveBody, /applyDraft\(item, draft\)/);
  assert.match(saveBody, /persistCharacters\(nextCharacters\)/);
  assert.match(saveBody, /setCharacters\(previousCharacters\)/, '保存失败必须回滚本地状态');
});

test('draft writers are the expected set (tripwire for new write paths)', () => {
  const writers = panelSource.match(/setDraftField\(/g) || [];
  // 已人工复核的 6 个写入点：writeSuggestionToDraft ×2（年龄只取数字、其余字段）、
  // 姓名输入、角色定位选择、多行字段输入、单行字段输入。
  assert.equal(writers.length, 6, `setDraftField 调用点数量变化（当前 ${writers.length}），需人工复核是否新增了写入路径`);
});

test('the panel renders plain values with no score widgets or MBTI labels', () => {
  // 只看代码，不看注释：注释里出现「不再有 MBTI」是合理的
  const panelCode = panelSource
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

  assert.doesNotMatch(panelCode, /(openness|conscientiousness|extraversion|agreeableness|neuroticism|mbti)/i);
  assert.doesNotMatch(panelCode, /Progress|<Bar|scoreBar/);
  assert.match(panelCode, /profile-value-/);
});

// ── 前后端字段清单漂移检查 ────────────────────────────────────

test('frontend field list stays in sync with the backend PROFILE_FIELDS', () => {
  const backendSource = readFileSync(
    new URL('../../backend/app/services/character_profile_service.py', import.meta.url),
    'utf8',
  );

  const panelKeys = [...panelSource.matchAll(/key: '([a-zA-Z]+)'/g)].map((match) => match[1]);
  const unique = [...new Set(panelKeys)];

  assert.equal(unique.length, 14, `前端字段数量变化（当前 ${unique.length}）：${unique.join(', ')}`);
  for (const key of unique) {
    assert.match(
      backendSource,
      new RegExp(`"${key}"`),
      `后端 character_profile_service.PROFILE_FIELDS 缺少字段 ${key}`,
    );
  }
});
