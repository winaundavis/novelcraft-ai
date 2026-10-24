import type {
  Character,
  CharacterPersonality,
  Chapter,
  Foreshadow,
  ForeshadowConsistency,
  ForeshadowConsistencyIssue,
  ForeshadowListData,
  ForeshadowSuggestion,
  Outline,
  OutlineNode,
  Project,
  StyleProfile,
  StyleRule,
  StyleDimensions,
  StylePlanLedger,
  StyleTransition,
  TransitionPace,
} from '../types';

const QUESTION_MARK_ONLY_PATTERN = /^[?？\s]+$/;
const REPLACEMENT_CHAR_PATTERN = /�/;
const SUSPICIOUS_LATIN_MOJIBAKE_PATTERN = /(?:Ã.|Â.|æ.|ç.|ä.|å.|é.){2,}/;
const SUSPICIOUS_CJK_MOJIBAKE_FRAGMENTS = [
  '浣犲ソ',
  '绔犺妭',
  '鐏垫劅',
  '鏈懡鍚',
  '澶辫触',
  '璇锋眰',
  '鍔╂墜',
  '寮€濮',
  '淇濆瓨',
  '瀵煎叆',
  '涓栫晫瑙',
  '鍙傝€冨垎鏋',
  '澶磋剳椋庢毚',
  '鍓ф儏',
  '瑙掕壊',
  '鍐茬獊',
  '绗',
];

export function looksGarbled(value: string | null | undefined): boolean {
  if (!value) return false;

  const cleaned = value.trim();
  if (!cleaned) return false;

  if (QUESTION_MARK_ONLY_PATTERN.test(cleaned) && cleaned.replace(/\s/g, '').length >= 3) {
    return true;
  }

  if (REPLACEMENT_CHAR_PATTERN.test(cleaned)) {
    return true;
  }

  if (SUSPICIOUS_CJK_MOJIBAKE_FRAGMENTS.some((fragment) => cleaned.includes(fragment))) {
    return true;
  }

  const suspiciousLatinCount = (cleaned.match(/[ÃÂæçäåé]/g) || []).length;
  if (suspiciousLatinCount >= 3) {
    return true;
  }

  return SUSPICIOUS_LATIN_MOJIBAKE_PATTERN.test(cleaned);
}

function sanitizeText(value: string | null | undefined, fallback = ''): string {
  if (typeof value !== 'string') return fallback;
  const cleaned = value.trim();
  if (!cleaned) return fallback;
  return looksGarbled(cleaned) ? fallback : cleaned;
}

function sanitizeName(value: string | null | undefined, fallback: string): string {
  const cleaned = sanitizeText(value, '');
  return cleaned || fallback;
}

function firstHintToken(hints: string): string {
  const chineseToken = hints.match(/[\u4e00-\u9fff]{2,6}/)?.[0];
  if (chineseToken) return chineseToken;

  const english = hints.replace(/[^A-Za-z ]+/g, ' ').trim();
  if (!english) return '角色';
  const token = english.split(/\s+/)[0] || 'Character';
  return token.charAt(0).toUpperCase() + token.slice(1, 12);
}

function buildCharacterNameFallback(index: number): string {
  return `角色${index + 1}`;
}

function buildLocationNameFallback(index: number): string {
  return `地点${index + 1}`;
}

function buildChapterTitleFallback(order: number | undefined, index: number): string {
  const chapterNo = order && order > 0 ? order : index + 1;
  return `第${chapterNo}章`;
}

function stripGeneratedChapterScaffold(content: string, title: string): string {
  const normalized = content.replace(/\r\n?/g, '\n');
  if (!normalized.trimStart().startsWith('#')) {
    return content;
  }

  const lines = normalized.split('\n');
  let cursor = 0;
  while (cursor < lines.length && !lines[cursor].trim()) cursor += 1;

  const headingLine = lines[cursor]?.trim() || '';
  if (!headingLine.startsWith('#')) {
    return content;
  }

  const headingText = headingLine.replace(/^#+\s*/, '').trim();
  if (!headingText || (title.trim() && headingText !== title.trim())) {
    return content;
  }

  cursor += 1;
  while (cursor < lines.length && !lines[cursor].trim()) cursor += 1;

  const summaryLine = lines[cursor]?.trim() || '';
  if (!summaryLine.startsWith('>')) {
    return content;
  }

  cursor += 1;
  while (cursor < lines.length && !lines[cursor].trim()) cursor += 1;

  const narrative = lines.slice(cursor).join('\n').trim();
  if (narrative.length < 300) {
    return content;
  }

  return narrative;
}

function normalizeImportAnalysis(project: Project): Project['importAnalysis'] {
  const analysis = project.importAnalysis;
  if (!analysis) return analysis;

  return {
    ...analysis,
    summary: sanitizeText(analysis.summary, '章节已导入，但自动摘要暂时不可用。'),
    characters: (analysis.characters || []).map((item, index) => ({
      ...item,
      name: sanitizeName(item.name, buildCharacterNameFallback(index)),
    })),
    locations: (analysis.locations || []).map((item, index) => ({
      ...item,
      name: sanitizeName(item.name, buildLocationNameFallback(index)),
    })),
    timeline: (analysis.timeline || []).map((item, index) => ({
      ...item,
      text: sanitizeText(item.text, `第${item.chapter || index + 1}章时间线记录`),
      type: sanitizeText(item.type, '事件'),
    })),
    foreshadowing: (analysis.foreshadowing || []).map((item, index) => ({
      ...item,
      text: sanitizeText(item.text, `伏笔线索 ${index + 1}`),
      category: sanitizeText(item.category, '待确认伏笔'),
      context: sanitizeText(item.context, '上下文暂不可用，建议回到对应章节复核。'),
    })),
  };
}

function normalizeInspirationFragments(project: Project): Project['inspirationFragments'] {
  const fragments = project.inspirationFragments || [];
  return fragments
    .filter((fragment) => fragment && sanitizeText(fragment.content, ''))
    .map((fragment, index) => ({
      ...fragment,
      title: sanitizeText(fragment.title, `灵感碎片 ${index + 1}`),
      content: sanitizeText(fragment.content, ''),
      type: fragment.type || 'idea',
      tags: (fragment.tags || []).filter((tag) => tag && !looksGarbled(tag)),
      source: fragment.source || 'manual',
    }));
}

export function normalizeProject(project: Project): Project {
  const outline = project.outline
    ? normalizeOutline(project.outline, {
      premise: project.premise || project.outline.premise || '',
      structure: project.outline.structure,
      targetWordCount: project.targetWordCount,
      targetChapters: project.targetChapters
        || project.outline.nodes.filter((node) => node.type === 'chapter').length
        || 30,
    })
    : null;
  return {
    ...project,
    name: sanitizeName(project.name, '未命名项目'),
    subtitle: sanitizeText(project.subtitle, ''),
    premise: sanitizeText(project.premise, ''),
    tags: (project.tags || []).filter((tag) => tag && !looksGarbled(tag)),
    importAnalysis: normalizeImportAnalysis(project),
    inspirationFragments: normalizeInspirationFragments(project),
    outline,
  };
}

export function normalizeProjects(projects: Project[]): Project[] {
  return projects.map(normalizeProject);
}

export function normalizeChapter(chapter: Chapter, index = 0): Chapter {
  const normalizedTitle = sanitizeName(chapter.title, buildChapterTitleFallback(chapter.order, index));
  const rawContent = typeof chapter.content === 'string' ? chapter.content : '';
  return {
    ...chapter,
    title: normalizedTitle,
    content: stripGeneratedChapterScaffold(rawContent, normalizedTitle),
    summary: sanitizeText(chapter.summary, ''),
    notes: sanitizeText(chapter.notes, ''),
  };
}

export function normalizeChapters(chapters: Chapter[]): Chapter[] {
  return chapters.map((chapter, index) => normalizeChapter(chapter, index));
}

/**
 * 把角色性格归一化为 `{ description }`。
 *
 * 旧数据里是五维分数 + MBTI（`{openness, conscientiousness, ..., mbti}`）。
 * 这里**不做分数→文字的迁移**：那些分数是关键词规则对一句简介的猜测，从来不
 * 是作者手写的，翻译成「外向度偏高」之类仍是打分口吻，与产品要求相冲。旧结构
 * 直接被丢弃，界面会引导作者手写或让 AI 通读正文与大纲重新生成。
 */
export function normalizePersonality(value: unknown): CharacterPersonality {
  const raw = (value || {}) as Record<string, unknown>;
  const description = typeof raw.description === 'string' ? raw.description.trim() : '';
  return { description };
}

export function normalizeCharacter(character: Character, hints = ''): Character {
  const personality = normalizePersonality(character.personality);
  if (
    !looksGarbled(character.name)
    && !looksGarbled(character.background)
    && !looksGarbled(character.basicInfo.identity)
  ) {
    return { ...character, personality };
  }

  const fallbackName = firstHintToken(hints);

  return {
    ...character,
    name: sanitizeName(character.name, fallbackName),
    basicInfo: {
      ...character.basicInfo,
      gender: sanitizeText(character.basicInfo.gender, '未知'),
      appearance: sanitizeText(character.basicInfo.appearance, '神情克制，长期承压留下了明显痕迹。'),
      identity: sanitizeText(character.basicInfo.identity, fallbackName),
      occupation: sanitizeText(character.basicInfo.occupation, '与主线冲突密切相关的人物'),
    },
    personality,
    background: sanitizeText(
      character.background,
      `${fallbackName}与当前主线冲突有直接牵连，也背着一段尚未公开的过去。`,
    ),
    coreTrauma: sanitizeText(character.coreTrauma, '曾因一次误判失去重要的人或机会。'),
    deepDesire: sanitizeText(character.deepDesire, '想重新掌握主动权，并再次成为值得被信任的人。'),
    arc: sanitizeText(character.arc, '从自我防御逐渐走向主动承担。'),
    catchphrase: sanitizeText(character.catchphrase, '先别着急，现在下结论还太早。'),
    speechStyle: sanitizeText(character.speechStyle, '短句、克制，必要时会带一点锋利。'),
    habits: sanitizeText(character.habits, '思考时会下意识整理手边的物件。'),
    values: sanitizeText(character.values, '秩序、代价，以及被重新建立的信任。'),
  };
}

function buildOutlineNodes(targetWordCount: number, targetChapters: number, premise: string): OutlineNode[] {
  const chapters = Math.max(targetChapters, 6);
  const volumes = chapters >= 18 ? 3 : 2;
  const counts = new Array(volumes).fill(Math.floor(chapters / volumes));

  for (let i = 0; i < chapters % volumes; i += 1) counts[i] += 1;

  const volumeTitles = volumes === 3 ? ['开端', '深入', '回响'] : ['上卷', '下卷'];
  const nodes: OutlineNode[] = [];
  let chapterNo = 1;

  const volumeSummaries = [
    `开端：围绕“${premise.slice(0, 60) || '主线冲突'}”建立人物处境，制造第一个不可逆事件，迫使主角作出选择。`,
    '深化：承接前卷选择造成的后果，让关系与目标发生冲突，并通过新的事件揭开关键真相。',
    '收束：把累积的矛盾推向最终决断，让人物为选择付出代价，回收线索并完成情绪落点。',
  ];
  // 每卷的冲突/揭示/情绪必须各卷不同：曾把同一句模板写给每一卷，
  // 大纲页看起来像「每卷都一样」。纯本地兜底也按卷位给不同文案。
  const volumeSignals = [
    { conflict: '主角被主线事件推进去，原有退路被封死', revelation: '最初的判断被证明并不成立', emotion: '压抑渐紧' },
    { conflict: '旧关系反噬，盟友的立场开始动摇', revelation: '隐藏历史的一半被证实', emotion: '紧绷对抗' },
    { conflict: '主角必须与真正的对手正面决断', revelation: '全书最大的悬念被兑现', emotion: '爆发后回落' },
  ];
  counts.forEach((count, volumeIndex) => {
    const volumeId = `volume-${volumeIndex + 1}`;
    const signals = volumeSignals[volumes === 3 ? volumeIndex % volumeSignals.length : (volumeIndex === 0 ? 0 : 2)];

    nodes.push({
      id: volumeId,
      parentId: null,
      type: 'volume',
      title: `第${volumeIndex + 1}卷·${volumeTitles[volumeIndex]}`,
      summary: volumeSummaries[volumeIndex % volumeSummaries.length],
      conflict: signals.conflict,
      revelation: signals.revelation,
      emotion: signals.emotion,
      isKeyPoint: volumeIndex === volumes - 1,
      order: volumeIndex + 1,
      targetWordCount: Math.max(Math.round(targetWordCount / volumes), 3000),
      povCharacterId: null,
    });

    for (let order = 1; order <= count; order += 1) {
      nodes.push({
        id: `chapter-${chapterNo}`,
        parentId: volumeId,
        type: 'chapter',
        title: `第${chapterNo}章`,
        summary: '推进主线谜团，提高压力，并让主角的下一步选择变得更困难。',
        conflict: '当前目标与阻力发生正面冲突。',
        revelation: '新的线索或后果出现。',
        emotion: '持续推进',
        isKeyPoint: chapterNo % Math.max(Math.floor(chapters / 5), 4) === 0,
        order,
        targetWordCount: Math.max(Math.round(targetWordCount / chapters), 1500),
        povCharacterId: null,
      });
      chapterNo += 1;
    }
  });

  return nodes;
}

function ensureOutlineArcs(outline: Outline): Outline {
  const volumes = outline.nodes.filter((node) => node.type === 'volume');
  const existingArcs = outline.nodes.filter((node) => node.type === 'arc');
  const arcById = new Map(existingArcs.map((arc) => [arc.id, arc]));
  const chapterArcIds = new Map<string, string>();

  for (const volume of volumes) {
    const chapters = outline.nodes
      .filter((node) => node.type === 'chapter' && node.parentId === volume.id)
      .sort((a, b) => a.order - b.order);
    const arcs = existingArcs
      .filter((node) => node.parentId === volume.id)
      .sort((a, b) => a.order - b.order);
    chapters.forEach((chapter, index) => {
      const currentArc = chapter.arcId ? arcById.get(chapter.arcId) : null;
      if (currentArc?.parentId === volume.id) {
        chapterArcIds.set(chapter.id, currentArc.id);
        return;
      }
      const arcIndex = Math.min(arcs.length - 1, Math.floor(index * arcs.length / chapters.length));
      if (arcs[arcIndex]) chapterArcIds.set(chapter.id, arcs[arcIndex].id);
    });
  }

  if (chapterArcIds.size === 0) return outline;
  return {
    ...outline,
    nodes: outline.nodes.map((node) => (
      node.type === 'chapter' && chapterArcIds.has(node.id)
        ? { ...node, arcId: chapterArcIds.get(node.id) }
        : node
    )),
  };
}

export function buildFallbackOutline(params: {
  premise: string;
  structure: Outline['structure'];
  targetWordCount: number;
  targetChapters: number;
  projectId?: string;
}): Outline {
  return {
    id: `outline-${Date.now()}`,
    projectId: params.projectId || '',
    structure: params.structure,
    premise: params.premise,
    mainSummary:
      '故事从被篡改的过去与不稳定的身份切入，在不断加码的外部压力、关系裂缝与信息揭示中推进，直到被压住的真相再也无法继续隐藏。',
    nodes: buildOutlineNodes(params.targetWordCount, params.targetChapters, params.premise),
  };
}

export function normalizeOutline(
  outline: Outline,
  params: { premise: string; structure: Outline['structure']; targetWordCount: number; targetChapters: number },
): Outline {
  const outlineWithArcs = ensureOutlineArcs(outline);
  const volumes = outlineWithArcs.nodes.filter((node) => node.type === 'volume');
  const normalizedSummaries = volumes.map((node) => node.summary.trim());
  const repeatedVolumeSummary = volumes.length > 1 && new Set(normalizedSummaries).size < volumes.length;
  const premiseMarker = params.premise.trim().slice(0, 24);
  const repeatedPremise = premiseMarker.length >= 12
    && normalizedSummaries.filter((summary) => summary.includes(premiseMarker)).length > 1;
  const garbled =
    looksGarbled(outline.mainSummary)
    || outlineWithArcs.nodes.some((node) => looksGarbled(node.title) || looksGarbled(node.summary));

  const volumePrefixes = volumes.map((volume) => volume.title.match(/第[一二三四五六七八九十\d]+卷/)?.[0] || volume.title);
  const repairedNodes = outlineWithArcs.nodes.map((node) => {
    if (node.type !== 'chapter' || !node.parentId) return node;
    const parentIndex = volumes.findIndex((volume) => volume.id === node.parentId);
    if (parentIndex < 0) return node;
    const summary = node.summary.trim();
    const containsJsonFragment = /"(?:volumes|title|summary|mainSummary)"\s*:/.test(summary)
      || /^[\s,\[\]{}]+$/.test(summary);
    const referencesOtherVolume = volumePrefixes.some(
      (prefix, index) => index !== parentIndex && prefix && summary.includes(prefix),
    );
    if (!containsJsonFragment && !referencesOtherVolume) return node;
    const parent = volumes[parentIndex];
    return {
      ...node,
      summary: `${parent.title}·第${node.order}章：围绕“${parent.summary.slice(0, 100)}”推进本卷事件，承接前一章后果并形成新的行动或阻力。`,
    };
  });
  const repairedOutline = repairedNodes.some((node, index) => node !== outlineWithArcs.nodes[index])
    ? { ...outlineWithArcs, nodes: repairedNodes }
    : outlineWithArcs;

  if (!garbled && !repeatedVolumeSummary && !repeatedPremise) return repairedOutline;

  if (repeatedVolumeSummary && !garbled) {
    const stageLabels = ['开端', '深化', '收束'];
    return {
      ...outlineWithArcs,
      nodes: repairedNodes.map((node) => {
        if (node.type !== 'volume') return node;
        const volumeIndex = volumes.findIndex((volume) => volume.id === node.id);
        const chapterSummaries = repairedNodes
          .filter((chapter) => chapter.type === 'chapter' && chapter.parentId === node.id)
          .map((chapter) => chapter.summary.trim())
          .filter(Boolean)
          .slice(0, 3);
        const details = chapterSummaries.join('；') || params.premise || '推进本卷核心冲突';
        return {
          ...node,
          summary: `${stageLabels[volumeIndex] || `第${volumeIndex + 1}阶段`}：${details}`,
        };
      }),
    };
  }

  if (repeatedPremise && !garbled) {
    const fallbackVolumes = buildOutlineNodes(
      params.targetWordCount,
      params.targetChapters,
      params.premise,
    ).filter((node) => node.type === 'volume');
    return {
      ...outlineWithArcs,
      nodes: repairedNodes.map((node) => {
        if (node.type !== 'volume') return node;
        const volumeIndex = volumes.findIndex((volume) => volume.id === node.id);
        return {
          ...node,
          summary: fallbackVolumes[volumeIndex]?.summary || node.summary,
        };
      }),
    };
  }

  return {
    ...outline,
    ...buildFallbackOutline(params),
  };
}

export function normalizeChatReply(reply: string): string {
  if (!looksGarbled(reply)) return reply;
  return '当前已切换到本地创作建议模式。下一章可以尝试加入一条新线索、一处关系变化，以及一个更困难的抉择。';
}

export function buildFallbackChatReply(messages: Array<{ role?: string; content?: string }>): string {
  const lastUser = [...messages].reverse().find((item) => item.role === 'user')?.content || '';

  if (/character|人物/i.test(lastUser)) {
    return '可以先抓住三件事：角色表层目标、隐藏欲望，以及如果他继续不改变会付出的代价。';
  }

  if (/outline|plot|情节|大纲/i.test(lastUser)) {
    return '下一段可以加入一个外部阻碍、一处关系变化，以及一个会改写前文含义的新提示。';
  }

  return '当前已切换到本地建议模式。下一步建议加强线索推进、抬高选择难度，并让情绪后果真正落地。';
}

export function buildFallbackContinuation(contextBefore: string, direction?: string, wordCount = 500): string {
  const focus = (() => {
    const raw = direction?.trim();
    if (!raw) return '继续推进下一段关键情节';
    const lines = raw
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .filter((line) => !/^(请|不要|保持|本章结尾|本章需要)/.test(line));
    return lines[0] || '继续推进下一段关键情节';
  })();

  const limit = Math.max(120, Math.min(wordCount, 800));
  const tone =
    contextBefore.includes('海')
    || contextBefore.includes('港')
    || /harbor|archive|pier|logbook/i.test(contextBefore)
      ? '海风带着锈味和盐粒，贴着窗缝缓慢灌进来。'
      : '房间安静得过了头，像在等谁先把真相说破。';

  const draft = `${tone}${focus}。她没有去看最显眼的痕迹，而是重新追索那个几乎会被忽略的小细节。正是在那里，事情开始露出真正的缝隙。那本该只是例行记录的标记，此刻却显得过于刻意，像是有人提前知道这份材料会落到错误的人手里。这个发现没有直接给出答案，却一下缩小了怀疑范围。能动这份记录的人，不只接近真相，更清楚该拿走什么，才能让真相再晚一点浮出水面。等她再抬起头时，下一步已经不是要不要查下去，而是她还能信谁。`;
  return draft.slice(0, limit * 2);
}

export function buildFallbackCharacter(input: {
  projectId: string;
  role: Character['role'];
  hints: string;
}): Character {
  const hintedName = firstHintToken(input.hints);

  return {
    id: `char-${Date.now()}`,
    projectId: input.projectId,
    name: hintedName || '角色',
    aliases: [],
    role: input.role,
    basicInfo: {
      age: 27,
      gender: '未知',
      appearance: '神情克制，带着长期承压留下的痕迹，也习惯先观察再开口。',
      identity: '与主线谜团紧密相关的人物',
      occupation: '与项目核心真相直接相关',
    },
    personality: {
      description: '',
    },
    background: `${hintedName || '该角色'}与当前主线冲突有一段隐秘过往，并习惯把关键信息藏到最后一刻。`,
    coreTrauma: '曾在一次错误决定之后失去重要的人或机会。',
    surfaceMotivation: input.hints || '先保住当前目标，并设法稳住局面。',
    deepDesire: '重新掌握主动权，并成为关键时刻值得被托付的人。',
    arc: '从谨慎自保逐渐走向主动承担。',
    catchphrase: '我们还漏掉了最关键的那一部分。',
    speechStyle: '短句、克制，偶尔会有很锋利的判断。',
    habits: '思考时会下意识整理身边的物件。',
    values: '秩序、责任，以及被重新建立的信任。',
    relations: [],
  };
}

type ReviewIssue = Record<string, unknown>;
type ReviewResult = Record<string, unknown> & { issues?: ReviewIssue[] };

export function normalizeReviewResult(result: ReviewResult, text: string): ReviewResult {
  const summary = String(result.summary || '');
  const issues = Array.isArray(result.issues) ? result.issues : [];
  const garbled =
    looksGarbled(summary)
    || issues.some((issue) => looksGarbled(String(issue.type || issue.category || issue.issue || '')));

  if (!garbled) return result;

  const normalizedIssues: ReviewIssue[] = [];

  if ((text.match(/\bsuddenly\b/gi) || []).length >= 3) {
    normalizedIssues.push({
      severity: 'suggestion',
      type: '突转提示词过多',
      location: '全文',
      original: 'suddenly',
      suggestion: '减少重复的突转提示词，改用更具体的动作、因果和场景推进转折。',
      reason: '同类突转词过多会削弱节奏层次，也会让逻辑链条显得单薄。',
    });
  }

  const avgSentenceLength = text.length / Math.max(text.split(/[.!?。！？]/).filter(Boolean).length, 1);
  if (avgSentenceLength > 40) {
    normalizedIssues.push({
      severity: 'suggestion',
      type: '平均句长偏长',
      location: '全文',
      original: `平均句长约 ${Math.round(avgSentenceLength)} 字`,
      suggestion: '在高张力段落加入更短的句子，拉开节奏对比。',
      reason: '连续长句会削弱紧张感，也不利于关键信息落点。',
    });
  }

  return {
    ...result,
    totalIssues: normalizedIssues.length,
    fatalCount: 0,
    importantCount: 0,
    suggestionCount: normalizedIssues.length,
    issues: normalizedIssues,
    summary:
      normalizedIssues.length > 0
        ? `本地兜底审校完成：发现 ${normalizedIssues.length} 条建议。`
        : '本地兜底审校未发现明显结构性问题。',
    languageScore: typeof result.languageScore === 'number' ? result.languageScore : 88,
    logicScore: typeof result.logicScore === 'number' ? result.logicScore : 88,
    rhythmScore: typeof result.rhythmScore === 'number' ? result.rhythmScore : 85,
  };
}

export function buildFallbackReviewResult(text: string): ReviewResult {
  return normalizeReviewResult({ summary: '', issues: [] }, text);
}

// ── M4 文风画像引擎 ─────────────────────────

function normalizeRuleList(rules: StyleRule[]): StyleRule[] {
  return (Array.isArray(rules) ? rules : [])
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      rule: sanitizeText(item.rule, ''),
      key: sanitizeText(item.key, ''),
      source: sanitizeText(item.source, ''),
    }))
    .filter((item) => item.rule);
}

/**
 * 画像维度来自数据库历史记录，不能假设 JSON 中每个字段仍符合类型。
 * 这里把它收敛成面板可以安全渲染的原始值，尤其避免 `tone` 对象直接成为 React child。
 */
function normalizeStyleDimensions(value: unknown): StyleDimensions {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const finiteNumber = (candidate: unknown): number | undefined => (
    typeof candidate === 'number' && Number.isFinite(candidate) ? candidate : undefined
  );
  const paragraphLength = raw.paragraphLength === 'short'
    || raw.paragraphLength === 'medium'
    || raw.paragraphLength === 'long'
    ? raw.paragraphLength
    : undefined;

  return {
    avgSentenceLength: finiteNumber(raw.avgSentenceLength),
    dialogueRatio: finiteNumber(raw.dialogueRatio),
    descriptionDensity: finiteNumber(raw.descriptionDensity),
    paragraphLength,
    tone: typeof raw.tone === 'string' ? sanitizeText(raw.tone, '') : '',
  };
}

export function normalizeStyleProfile(profile: StyleProfile | null | undefined): StyleProfile | null {
  // 空值返回 null，不要用 `{} as StyleProfile` 冒充画像：它是个真值，
  // 会通过调用方的 `if (json.data)` 判定进入 state，再在读取 strengths / 规则列表时抛错，
  // 而渲染期异常会掀掉整棵 React 树（表现就是白屏）。
  if (!profile || typeof profile !== 'object') {
    return null;
  }
  return {
    ...profile,
    status: profile.status === 'degraded' ? 'degraded' : 'completed',
    narrativeRules: normalizeRuleList(profile.narrativeRules),
    characterRules: normalizeRuleList(profile.characterRules),
    languageRules: normalizeRuleList(profile.languageRules),
    rhythmRules: normalizeRuleList(profile.rhythmRules),
    antiAiRules: normalizeRuleList(profile.antiAiRules),
    strengths: (Array.isArray(profile.strengths) ? profile.strengths : [])
      .map((item) => sanitizeText(String(item), ''))
      .filter(Boolean),
    weaknesses: (Array.isArray(profile.weaknesses) ? profile.weaknesses : [])
      .map((item) => sanitizeText(String(item), ''))
      .filter(Boolean),
    report: sanitizeText(profile.report, ''),
    metrics: { ...(profile.metrics || {}) },
    dimensions: normalizeStyleDimensions(profile.dimensions),
  };
}

/**
 * 过渡计划归一化。
 *
 * v2 计划带 `sourceSnapshot` / `targetSnapshot` / 完成账本，**不能**再重建成
 * 「只有 step/steps」的旧结构——那会把 planId、revision 和快照全部丢掉。
 * 所以这里只做三件事：补齐 v1 缺失的数值、区分 v1/v2、把非法 pace 收敛成 'medium'。
 * **缺失的 v2 字段保持缺失**：不虚构 revision，不拿旧步骤数换算完成数。
 */
export function normalizeStyleTransition(transition: StyleTransition | null | undefined): StyleTransition | null {
  if (!transition || typeof transition !== 'object') {
    return null;
  }
  const pace = transition.pace;
  const safePace: TransitionPace =
    pace === 'fast' || pace === 'medium' || pace === 'slow' || pace === 'custom' ? pace : 'medium';

  // schemaVersion 缺失时按 v1 处理；只有明确的 2 才算 v2。
  const schemaVersion: 1 | 2 = transition.schemaVersion === 2 ? 2 : 1;
  const legacyReadOnly = schemaVersion === 1 ? true : Boolean(transition.legacyReadOnly);

  const plan: StylePlanLedger = {
    ...(transition.plan && typeof transition.plan === 'object' ? transition.plan : {}),
  };
  if (schemaVersion === 1) {
    plan.step = typeof plan.step === 'number' ? plan.step : 0;
    plan.weight = typeof plan.weight === 'number' ? plan.weight : 0;
    plan.steps = Array.isArray(plan.steps) ? plan.steps : [];
    plan.start_chapter_count = typeof plan.start_chapter_count === 'number' ? plan.start_chapter_count : 0;
  }

  const normalized: StyleTransition = {
    ...transition,
    pace: safePace,
    progress: typeof transition.progress === 'number' ? transition.progress : 0,
    intervalChapters: typeof transition.intervalChapters === 'number' ? transition.intervalChapters : 6,
    totalSteps: typeof transition.totalSteps === 'number' ? transition.totalSteps : 10,
    plan,
    schemaVersion,
    legacyReadOnly,
  };

  // 只搬运后端确实给过的字段：undefined 一律保持 undefined。
  if (typeof transition.planId === 'string') normalized.planId = transition.planId;
  if (typeof transition.revision === 'number') normalized.revision = transition.revision;
  if (typeof transition.targetChapters === 'number') normalized.targetChapters = transition.targetChapters;
  if (typeof transition.completedCount === 'number') normalized.completedCount = transition.completedCount;

  return normalized;
}

// ── P2 伏笔账本 ─────────────────────────

const FORESHADOW_STATUSES = ['planted', 'progressing', 'resolved', 'abandoned'];

function normalizeForeshadowStatus(status: string): Foreshadow['status'] {
  return (FORESHADOW_STATUSES as string[]).includes(status)
    ? (status as Foreshadow['status'])
    : 'planted';
}

export function normalizeForeshadow(item: Foreshadow | null | undefined): Foreshadow | null {
  // 返回 null 让列表把脏元素丢弃，而不是造一条「未命名伏笔」幽灵记录。
  if (!item || typeof item !== 'object') {
    return null;
  }
  return {
    ...item,
    name: sanitizeText(item.name, '未命名伏笔'),
    description: sanitizeText(item.description, ''),
    resolution: sanitizeText(item.resolution, ''),
    status: normalizeForeshadowStatus(item.status),
    events: Array.isArray(item.events) ? item.events : [],
    plantChapter: typeof item.plantChapter === 'number' ? item.plantChapter : 0,
    targetChapter: typeof item.targetChapter === 'number' ? item.targetChapter : 0,
    totalChapters: typeof item.totalChapters === 'number' ? item.totalChapters : 0,
    overdue: Boolean(item.overdue),
    overdueBy: typeof item.overdueBy === 'number' ? item.overdueBy : 0,
  };
}

export function normalizeForeshadowSuggestion(
  item: ForeshadowSuggestion | null | undefined,
): ForeshadowSuggestion | null {
  if (!item || typeof item !== 'object') {
    return null;
  }
  return {
    name: sanitizeText(item.name, '未命名伏笔'),
    description: sanitizeText(item.description, ''),
    plant_chapter: typeof item.plant_chapter === 'number' ? item.plant_chapter : 0,
    target_chapter: typeof item.target_chapter === 'number' ? item.target_chapter : 0,
    plant_chapter_id: String(item.plant_chapter_id || ''),
    target_chapter_id: String(item.target_chapter_id || ''),
  };
}

function normalizeConsistencyIssue(issue: ForeshadowConsistencyIssue): ForeshadowConsistencyIssue {
  return {
    type: sanitizeText(issue.type, 'unknown'),
    foreshadowId: String(issue.foreshadowId || ''),
    name: sanitizeText(issue.name, '未命名伏笔'),
    detail: sanitizeText(issue.detail, ''),
  };
}

export function normalizeForeshadowConsistency(
  consistency: ForeshadowConsistency,
): ForeshadowConsistency {
  if (!consistency || typeof consistency !== 'object') {
    return { passed: true, total: 0, byType: {}, issues: [] };
  }
  return {
    passed: Boolean(consistency.passed),
    total: typeof consistency.total === 'number' ? consistency.total : 0,
    byType: { ...(consistency.byType || {}) },
    issues: Array.isArray(consistency.issues)
      ? consistency.issues.map(normalizeConsistencyIssue)
      : [],
  };
}

export function normalizeForeshadowListData(data: ForeshadowListData): ForeshadowListData {
  if (!data || typeof data !== 'object') {
    return { items: [], consistency: { passed: true, total: 0, byType: {}, issues: [] }, alertCount: 0 };
  }
  return {
    items: Array.isArray(data.items)
      ? data.items.map(normalizeForeshadow).filter((item): item is Foreshadow => item !== null)
      : [],
    consistency: normalizeForeshadowConsistency(data.consistency),
    alertCount: typeof data.alertCount === 'number' ? data.alertCount : 0,
  };
}
