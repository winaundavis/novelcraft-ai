import test from 'node:test';
import assert from 'node:assert/strict';

import { conversationForRegeneration } from '../src/components/chat/chatRegeneration.ts';

test('regeneration reuses conversation through the preceding user message', () => {
  const messages = [
    { id: 'welcome', role: 'assistant', content: 'welcome', timestamp: '1' },
    { id: 'u1', role: 'user', content: '优化第一章', timestamp: '2' },
    { id: 'a1', role: 'assistant', content: '旧回复', timestamp: '3' },
    { id: 'u2', role: 'user', content: '后续问题', timestamp: '4' },
  ];

  assert.deepEqual(
    conversationForRegeneration(messages, 'a1').map(({ role, content }) => ({ role, content })),
    [
      { role: 'assistant', content: 'welcome' },
      { role: 'user', content: '优化第一章' },
    ],
  );
});

test('regeneration returns no conversation when the assistant has no preceding user message', () => {
  const messages = [{ id: 'welcome', role: 'assistant', content: 'welcome', timestamp: '1' }];
  assert.deepEqual(conversationForRegeneration(messages, 'welcome'), []);
});
