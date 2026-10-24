import type { ChatMessage } from '../../types';

export function conversationForRegeneration(
  messages: ChatMessage[],
  assistantMessageId: string,
): ChatMessage[] {
  const assistantIndex = messages.findIndex((message) => message.id === assistantMessageId);
  if (assistantIndex < 1) return [];
  let userIndex = -1;
  for (let index = assistantIndex - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'user') {
      userIndex = index;
      break;
    }
  }
  if (userIndex < 0) return [];
  return messages.slice(0, userIndex + 1);
}
