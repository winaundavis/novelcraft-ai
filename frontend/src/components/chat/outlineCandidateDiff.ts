type DiffNode = {
  id?: string;
  type?: string;
  title?: string;
  fields?: string[];
};

type OutlineDiff = {
  mainSummaryChanged?: boolean;
  added?: DiffNode[];
  removed?: DiffNode[];
  changed?: DiffNode[];
  moved?: DiffNode[];
};

export type OutlineDiffSection = {
  key: 'summary' | 'added' | 'removed' | 'changed' | 'moved';
  label: string;
  count: number;
  items: string[];
};

const NODE_LABELS: Record<string, string> = {
  volume: '卷',
  arc: '剧情弧',
  chapter: '章节',
};

const FIELD_LABELS: Record<string, string> = {
  title: '标题',
  summary: '简介',
  conflict: '冲突',
  revelation: '揭示',
  emotion: '情绪',
  targetWordCount: '目标字数',
  isKeyPoint: '关键节点',
};

function nodeLabel(item: DiffNode): string {
  return NODE_LABELS[item.type || ''] || '节点';
}

function nodeTitle(item: DiffNode): string {
  return item.title?.trim() || item.id || '未命名';
}

function entries(value: unknown): DiffNode[] {
  return Array.isArray(value) ? value.filter((item): item is DiffNode => Boolean(item && typeof item === 'object')) : [];
}

export function summarizeOutlineDiff(diff: OutlineDiff | Record<string, unknown> = {}): OutlineDiffSection[] {
  const sections: OutlineDiffSection[] = [];
  if (diff.mainSummaryChanged === true) {
    sections.push({ key: 'summary', label: '主线摘要', count: 1, items: ['修改主线摘要'] });
  }

  const added = entries(diff.added);
  if (added.length) sections.push({
    key: 'added', label: '新增', count: added.length,
    items: added.map((item) => `新增${nodeLabel(item)}：${nodeTitle(item)}`),
  });

  const removed = entries(diff.removed);
  if (removed.length) sections.push({
    key: 'removed', label: '移除', count: removed.length,
    items: removed.map((item) => `移除${nodeLabel(item)}：${nodeTitle(item)}`),
  });

  const changed = entries(diff.changed);
  if (changed.length) sections.push({
    key: 'changed', label: '内容修改', count: changed.length,
    items: changed.map((item) => {
      const fields = Array.isArray(item.fields)
        ? item.fields.map((field) => FIELD_LABELS[field] || field).join('、')
        : '内容';
      return `修改${nodeLabel(item)}：${nodeTitle(item)}（${fields}）`;
    }),
  });

  const moved = entries(diff.moved);
  if (moved.length) sections.push({
    key: 'moved', label: '归属调整', count: moved.length,
    items: moved.map((item) => `调整${nodeLabel(item)}归属：${nodeTitle(item)}`),
  });
  return sections;
}
