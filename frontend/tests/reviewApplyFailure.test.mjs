import test from 'node:test';
import assert from 'node:assert/strict';

import { SKIPPABLE_APPLY_CODES, isSkippableApplyFailure } from '../src/components/review/reviewApplyFailure.ts';

function codedError(code) {
  const error = new Error(`msg:${code}`);
  error.code = code;
  return error;
}

test('服务端「正文未动」的失败码都可跳过', () => {
  assert.deepEqual(
    [...SKIPPABLE_APPLY_CODES].sort(),
    ['copilot_create_timeout', 'copilot_empty_output', 'copilot_no_change', 'copilot_provider_failed'],
  );
  for (const code of SKIPPABLE_APPLY_CODES) {
    assert.equal(isSkippableApplyFailure(codedError(code)), true, `${code} 应可跳过`);
  }
});

test('正文已变 / 计划冲突 / 未保存这类失败绝不能跳过', () => {
  // 409 风格过渡计划冲突：正文可能已被别的写入改变，继续改会错位
  assert.equal(isSkippableApplyFailure(codedError('style_plan_conflict')), false);
  // 普通业务 400 没有码：信息不足，按"必须停"处理
  assert.equal(isSkippableApplyFailure(new Error('当前正文尚未保存或已更新')), false);
  assert.equal(isSkippableApplyFailure(codedError('')), false);
});

test('非错误对象 / 无 code 一律不跳过，避免误判成"什么都没发生"', () => {
  assert.equal(isSkippableApplyFailure(null), false);
  assert.equal(isSkippableApplyFailure(undefined), false);
  assert.equal(isSkippableApplyFailure({ message: 'x' }), false);
  assert.equal(isSkippableApplyFailure({ code: 42 }), false);
  assert.equal(isSkippableApplyFailure('copilot_no_change'), false);
});
