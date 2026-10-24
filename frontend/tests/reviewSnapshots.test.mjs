import test from 'node:test';
import assert from 'node:assert/strict';

import {
  REVIEW_SNAPSHOT_LIMIT,
  pruneReviewSnapshots,
  reviewContentSignature,
  reviewSnapshotKey,
} from '../src/stores/reviewSnapshots.ts';

/**
 * 审校快照的存储策略。
 *
 * 背景（作者报告）：工作台闲置一段时间后回到页面，审校结果就没了 —— 因为这份快照
 * 此前只活在内存里，而浏览器丢弃闲置标签页、开发服务器重连都会导致整页重新加载。
 * 现在改为持久化，于是必须有明确的**上限规则**：既不能无限增长，淘汰规则也要可解释
 * （「最近审校的留着」）且确定（同样的输入永远同样输出）。
 */

const NOW = 1_700_000_000_000;

function snapshot(reviewedAt) {
  return {
    result: { summary: `审校于 ${reviewedAt}` },
    degradedReason: '',
    contentSignature: '12:abc',
    reviewedAt,
    appliedIssueKeys: [],
    lastApplied: null,
  };
}

test('snapshots under the limit are returned untouched', () => {
  const snapshots = { a: snapshot(NOW), b: snapshot(NOW + 1) };

  const pruned = pruneReviewSnapshots(snapshots, 5);

  assert.equal(pruned, snapshots, '没超上限时不该复制或重排');
});

test('the oldest snapshots are dropped first once the limit is exceeded', () => {
  const snapshots = {
    oldest: snapshot(NOW),
    middle: snapshot(NOW + 1_000),
    newest: snapshot(NOW + 2_000),
  };

  const pruned = pruneReviewSnapshots(snapshots, 2);

  assert.deepEqual(Object.keys(pruned), ['middle', 'newest']);
});

test('a missing timestamp counts as the oldest, and ties keep insertion order', () => {
  const snapshots = {
    // 没有时间戳（历史数据 / 脏数据）：按最旧处理，不能被它挤掉真正的新快照
    broken: { ...snapshot(0), reviewedAt: undefined },
    tieA: snapshot(NOW),
    tieB: snapshot(NOW),
  };

  const pruned = pruneReviewSnapshots(snapshots, 2);

  assert.deepEqual(Object.keys(pruned), ['tieA', 'tieB'], '同时间戳时保持原顺序（先写入的先淘汰）');
});

test('pruning is deterministic for the same input', () => {
  const build = () => ({
    a: snapshot(NOW + 3),
    b: snapshot(NOW + 1),
    c: snapshot(NOW + 2),
    d: snapshot(NOW + 0),
  });

  const first = pruneReviewSnapshots(build(), 2);
  const second = pruneReviewSnapshots(build(), 2);

  assert.deepEqual(Object.keys(first), Object.keys(second));
  assert.deepEqual(Object.keys(first), ['a', 'c']);
});

test('the default limit matches what the store documents', () => {
  const snapshots = Object.fromEntries(
    Array.from({ length: REVIEW_SNAPSHOT_LIMIT + 5 }, (_, index) => [`k${index}`, snapshot(NOW + index)]),
  );

  const pruned = pruneReviewSnapshots(snapshots);

  assert.equal(Object.keys(pruned).length, REVIEW_SNAPSHOT_LIMIT);
  assert.ok('k34' in pruned);
  assert.ok(!('k0' in pruned));
});

test('a snapshot key is per project and per chapter', () => {
  assert.equal(reviewSnapshotKey('p1', 'c1'), 'p1::c1');
  // 没有章节（正文页还没选章）时也要有稳定的键，不能退化成该项目所有情况共用一个坑
  assert.equal(reviewSnapshotKey('p1', ''), 'p1::no-chapter');
  assert.notEqual(reviewSnapshotKey('p1', ''), reviewSnapshotKey('p2', ''));
});

test('the content signature changes when the text changes and stays stable otherwise', () => {
  const text = '她把信放回抽屉。';

  assert.equal(reviewContentSignature(text), reviewContentSignature(text), '同样正文必须同样指纹');
  assert.notEqual(reviewContentSignature(text), reviewContentSignature(`${text}后来她走了。`));
  assert.notEqual(reviewContentSignature(text), reviewContentSignature('她把信放回了抽屉。'));
});
