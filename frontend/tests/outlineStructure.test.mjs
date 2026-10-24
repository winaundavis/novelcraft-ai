import test from 'node:test';
import assert from 'node:assert/strict';

import { OUTLINE_STRUCTURE_LABELS, outlineStructureLabel } from '../src/types/index.ts';

/**
 * 大纲结构的中文名解析。
 *
 * 背景：后端声明的枚举是下划线（`schemas.py` 的 `Literal["three_act", ...]`），
 * 但导入链路（`api/imports.py`）历史上写入的是连字符 `three-act`。
 * 于是同一个字段在库里存在两种写法，直接查表会漏掉导入来的项目，
 * 把内部枚举值原样显示给作者 —— 用户报告的就是「结构卡片上写着英文」。
 */

test('四种已声明结构都翻成中文', () => {
  assert.equal(outlineStructureLabel('three_act'), '三幕式结构');
  assert.equal(outlineStructureLabel('kishotenketsu'), '起承转合');
  assert.equal(outlineStructureLabel('hero_journey'), '英雄之旅');
  assert.equal(outlineStructureLabel('free'), '自由结构');
});

test('连字符写法也认（历史导入数据），不会露出英文内部值', () => {
  // 这是用户实际遇到的那条：库里的值是 'three-act'
  assert.equal(outlineStructureLabel('three-act'), '三幕式结构');
  assert.equal(outlineStructureLabel('THREE-ACT'), '三幕式结构');
  assert.equal(outlineStructureLabel(' hero-journey '), '英雄之旅');
  // 反向保护：任何返回值都不该是内部枚举写法
  for (const raw of ['three_act', 'three-act', 'hero_journey', 'hero-journey']) {
    assert.ok(!/[-_]/.test(outlineStructureLabel(raw)), `仍然露出内部值：${raw}`);
  }
});

test('空值显示为「未设置」，不留空白', () => {
  // 真实库里多数项目 outline.structure 为 null，此前卡片上什么都不显示
  for (const raw of [null, undefined, '', '   ']) {
    assert.equal(outlineStructureLabel(raw), '未设置');
  }
});

test('未知取值原样返回，不猜成某个已知结构', () => {
  assert.equal(outlineStructureLabel('three_act_v2'), 'three_act_v2');
  assert.equal(outlineStructureLabel('以后新增的结构'), '以后新增的结构');
});

test('中文名只有一份来源，选择器不再另抄一遍', () => {
  assert.deepEqual(Object.keys(OUTLINE_STRUCTURE_LABELS).sort(), ['free', 'hero_journey', 'kishotenketsu', 'three_act']);
  for (const [key, label] of Object.entries(OUTLINE_STRUCTURE_LABELS)) {
    assert.equal(outlineStructureLabel(key), label);
    assert.ok(label.length > 0);
  }
});
