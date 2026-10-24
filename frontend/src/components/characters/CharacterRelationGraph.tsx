/**
 * CharacterRelationGraph — 人物关系可视化（基于 Cytoscape）
 *
 * - 节点按角色类型着色，重要角色更大，带柔和阴影
 * - 边按关系类型着色，标签默认淡显，悬停强调
 * - 悬停节点：聚焦其直接关系网络，其余淡化（减少杂乱）
 * - 点击节点切换到该角色详情（不重建图，避免跳动）
 * - 右下角可折叠图例：角色类型 + 关系类型
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import cytoscape, { type Core } from 'cytoscape';
import type { Character } from '../../types';

const ROLE_COLORS: Record<string, string> = {
  protagonist: '#4c6ef5',
  antagonist: '#fa5252',
  supporting: '#f59f00',
  mentor: '#7950f2',
  love_interest: '#e64980',
  sidekick: '#12b886',
  cameo: '#868e96',
};

const ROLE_LABELS: Record<string, string> = {
  protagonist: '主角',
  antagonist: '反派',
  supporting: '配角',
  mentor: '导师',
  love_interest: '感情线',
  sidekick: '搭档',
  cameo: '客串',
};

// 角色重要性 → 节点大小
const ROLE_SIZE: Record<string, number> = {
  protagonist: 46,
  antagonist: 42,
  love_interest: 38,
  mentor: 36,
  sidekick: 34,
  supporting: 32,
  cameo: 28,
};

const RELATION_COLORS: Record<string, string> = {
  family: '#20c997',
  friend: '#4dabf7',
  love: '#f06595',
  enemy: '#ff6b6b',
  interest: '#ffa94d',
  secret: '#9775fa',
  rival: '#ff922b',
  master_student: '#3bc9db',
};

const RELATION_LABELS: Record<string, string> = {
  family: '家人',
  friend: '朋友',
  love: '爱慕',
  enemy: '敌对',
  interest: '利益',
  secret: '秘密',
  rival: '竞争',
  master_student: '师徒',
};

const ROLE_LEGEND_ORDER = [
  'protagonist', 'antagonist', 'love_interest', 'mentor', 'sidekick', 'supporting', 'cameo',
];

const RELATION_LEGEND_ORDER = [
  'family', 'friend', 'love', 'enemy', 'interest', 'secret', 'rival', 'master_student',
];

interface CharacterRelationGraphProps {
  characters: Character[];
  selectedId: string;
  onSelectCharacter: (id: string) => void;
}

function nodeSize(role: string): number {
  return ROLE_SIZE[role] || 30;
}

export default function CharacterRelationGraph({
  characters,
  selectedId,
  onSelectCharacter,
}: CharacterRelationGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [showLegend, setShowLegend] = useState(true);
  // `onSelectCharacter` 由父组件传内联箭头函数（每次渲染都是新身份）。
  // 它只在一个点击回调里用到，**绝不能进下面那个建图 effect 的依赖** ——
  // 否则父组件每次渲染（含“点一下节点”导致的选中态更新）都会销毁并重建整张图，
  // 正好抵消了下面注释里“避免点击重建图”的意图。
  const onSelectCharacterRef = useRef(onSelectCharacter);
  useEffect(() => {
    onSelectCharacterRef.current = onSelectCharacter;
  });

  // 构建节点与边（与渲染分离，选中变化不重建图）
  const elements = useMemo(() => {
    const nodes = characters.map((character) => ({
      data: {
        id: character.id,
        label: character.name,
        role: character.role,
      },
    }));

    const edgeSet = new Set<string>();
    const edges: Array<{ data: { id: string; source: string; target: string; label: string; type: string } }> = [];

    for (const character of characters) {
      for (const relation of character.relations || []) {
        const targetId = relation.targetCharacterId;
        if (!characters.some((c) => c.id === targetId)) continue;

        const key = [character.id, targetId].sort().join('--') + relation.type;
        if (edgeSet.has(key)) continue;
        edgeSet.add(key);

        edges.push({
          data: {
            id: `${character.id}-${targetId}-${relation.type}`,
            source: character.id,
            target: targetId,
            label: RELATION_LABELS[relation.type] || relation.type,
            type: relation.type,
          },
        });
      }
    }

    return [...nodes, ...edges];
  }, [characters]);

  useEffect(() => {
    if (!containerRef.current || characters.length === 0) return;

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      // shadow-* 为 Cytoscape 运行时支持但 TS 类型未收录的样式，故用 any 断言
       
      style: [
        {
          selector: 'node',
          style: {
            'background-color': (ele: cytoscape.NodeSingular) => ROLE_COLORS[ele.data('role')] || '#adb5bd',
            label: 'data(label)',
            color: '#212529',
            'font-size': 11,
            'font-weight': 600,
            'text-valign': 'bottom',
            'text-halign': 'center',
            'text-margin-y': 3,
            'text-wrap': 'ellipsis',
            'text-max-width': '64px',
            'text-outline-width': 3,
            'text-outline-color': '#ffffff',
            width: (ele: cytoscape.NodeSingular) => nodeSize(ele.data('role')),
            height: (ele: cytoscape.NodeSingular) => nodeSize(ele.data('role')),
            'border-width': 2,
            'border-color': '#ffffff',
            'shadow-blur': 10,
            'shadow-color': (ele: cytoscape.NodeSingular) => ROLE_COLORS[ele.data('role')] || '#adb5bd',
            'shadow-opacity': 0.35,
            'shadow-offset-x': 0,
            'shadow-offset-y': 1,
          },
        },
        {
          selector: `node[id = "${selectedId}"]`,
          style: {
            'border-color': '#f08c00',
            'border-width': 4,
            'border-opacity': 1,
          },
        },
        {
          selector: 'edge',
          style: {
            width: 2,
            'line-color': (ele: cytoscape.EdgeSingular) => RELATION_COLORS[ele.data('type')] || '#adb5bd',
            'line-opacity': 0.85,
            'target-arrow-color': (ele: cytoscape.EdgeSingular) => RELATION_COLORS[ele.data('type')] || '#adb5bd',
            'target-arrow-shape': 'triangle',
            'target-arrow-fill': 'filled',
            'curve-style': 'bezier',
            'control-point-step-size': 40,
            label: 'data(label)',
            'font-size': 9,
            color: '#495057',
            'text-rotation': 'autorotate',
            'text-margin-x': 2,
            'text-margin-y': -4,
            'text-outline-width': 2,
            'text-outline-color': '#ffffff',
            'text-opacity': 0.7,
          },
        },
      ] as unknown as cytoscape.StylesheetStyle[],
      layout: {
        name: 'cose',
        animate: false,
        nodeRepulsion: () => 9000,
        idealEdgeLength: () => 150,
        edgeElasticity: () => 100,
        gravity: 0.2,
        numIter: 800,
        randomize: false,
      },
      userZoomingEnabled: false,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
    });

    // 聚焦模式：悬停节点时高亮其直接关系，其余淡化
    const resetFocus = () => {
      cy.nodes().style('opacity', 1);
      cy.edges().style('line-opacity', 0.85);
      cy.edges().style('text-opacity', 0.7);
      cy.edges().style('width', 2);
    };

    cy.on('mouseover', 'node', (event) => {
      const node = event.target;
      const connected = node.closedNeighborhood();

      cy.nodes().forEach((el) => {
        el.style('opacity', el === node || connected.has(el) ? 1 : 0.18);
      });
      cy.edges().forEach((el) => {
        const involved = connected.has(el);
        el.style('line-opacity', involved ? 1 : 0.1);
        el.style('text-opacity', involved ? 1 : 0.1);
      });
    });
    cy.on('mouseout', 'node', resetFocus);

    // 悬停边：强调该条关系
    cy.on('mouseover', 'edge', (event) => {
      const edge = event.target;
      edge.style('width', 3.5);
      edge.style('line-opacity', 1);
      edge.style('text-opacity', 1);
    });
    cy.on('mouseout', 'edge', (event) => {
      const edge = event.target;
      edge.style('width', 2);
      edge.style('line-opacity', 0.85);
      edge.style('text-opacity', 0.7);
    });

    // 点击节点 → 切换到该角色（通过 ref 读取最新回调，避免进依赖导致重建图）
    cy.on('tap', 'node', (event) => {
      const nodeId = event.target.id();
      if (nodeId) onSelectCharacterRef.current(nodeId);
    });

    cyRef.current = cy;

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
    // selectedId 与 onSelectCharacter 刻意都不进依赖：
    // 前者用单独 effect 更新、后者走 ref，都是为了避免“点一下就重建整张图”。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [characters, elements]);

  // 选中角色高亮更新（不重建图）
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes().style('border-width', 2);
    cy.nodes().style('border-color', '#ffffff');
    cy.nodes().style('border-opacity', 1);
    if (selectedId) {
      cy.nodes(`node[id = "${selectedId}"]`).style('border-color', '#f08c00');
      cy.nodes(`node[id = "${selectedId}"]`).style('border-width', 4);
    }
  }, [selectedId]);

  return (
    <div
      className="relative h-64 w-full overflow-hidden rounded-lg border border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-850"
      data-testid="character-relation-graph"
    >
      <div ref={containerRef} className="h-full w-full" />

      {/* 图例 */}
      {showLegend ? (
        <div className="absolute right-2 bottom-2 max-w-[240px] rounded-lg border border-surface-200 bg-white/90 p-2.5 shadow-sm backdrop-blur-sm dark:border-surface-600 dark:bg-surface-800/90">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-[11px] font-semibold text-surface-600 dark:text-surface-300">图例</span>
            <button
              type="button"
              onClick={() => setShowLegend(false)}
              className="text-[10px] text-surface-400 hover:text-surface-600 dark:hover:text-surface-200"
              aria-label="收起图例"
            >
              收起
            </button>
          </div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-1">
            {ROLE_LEGEND_ORDER.map((role) => (
              <div key={role} className="flex items-center gap-1.5">
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full border border-white shadow-sm"
                  style={{ backgroundColor: ROLE_COLORS[role] }}
                />
                <span className="text-[10px] text-surface-500 dark:text-surface-400">{ROLE_LABELS[role]}</span>
              </div>
            ))}
          </div>
          <div className="my-1.5 border-t border-dashed border-surface-200 dark:border-surface-600" />
          <div className="grid grid-cols-2 gap-x-3 gap-y-1">
            {RELATION_LEGEND_ORDER.map((type) => (
              <div key={type} className="flex items-center gap-1.5">
                <span className="h-0.5 w-4 shrink-0 rounded-full" style={{ backgroundColor: RELATION_COLORS[type] }} />
                <span className="text-[10px] text-surface-500 dark:text-surface-400">{RELATION_LABELS[type]}</span>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setShowLegend(true)}
          className="absolute right-2 bottom-2 rounded-lg border border-surface-200 bg-white/90 px-2 py-1 text-[11px] font-medium text-surface-500 shadow-sm backdrop-blur-sm hover:text-surface-700 dark:border-surface-600 dark:bg-surface-800/90 dark:text-surface-400"
        >
          图例
        </button>
      )}
    </div>
  );
}

