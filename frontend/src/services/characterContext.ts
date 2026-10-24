import type { Character, NarrativeContextCharacter } from '../types';

/**
 * 角色卡 → AI 请求上下文。
 *
 * 续写（MarkdownEditor）与审校（ReviewPanel）共用这一个入口：新增要进提示词的
 * 角色字段时只改这里一处，避免两边漏改导致「填了但 AI 看不见」。
 */
export function buildCharacterContext(character: Character): NarrativeContextCharacter {
  return {
    name: character.name,
    role: character.role,
    age: character.basicInfo.age,
    gender: character.basicInfo.gender,
    appearance: character.basicInfo.appearance,
    identity: character.basicInfo.identity,
    personality: character.personality?.description || '',
    background: character.background,
    coreTrauma: character.coreTrauma,
    surfaceMotivation: character.surfaceMotivation,
    deepDesire: character.deepDesire,
    arc: character.arc,
    catchphrase: character.catchphrase,
    speechStyle: character.speechStyle,
    habits: character.habits,
    values: character.values,
  };
}

export function buildCharacterContexts(characters: Character[] | undefined): NarrativeContextCharacter[] {
  return (characters || []).map(buildCharacterContext);
}
