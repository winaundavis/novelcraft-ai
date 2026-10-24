/**
 * 判定一条自由输入是否属于"修改正文"的指令。
 *
 * 背景：工作区写作助手有两类结果——(a) 生成**正文候选**（用户接受后才改正文），
 * (b) 普通**对话回复**。此前用一个很窄的关键词正则判定，导致口语化指令
 * （"把这段改一下""精简一下本章""帮我润色一下"）落到对话里：用户以为正文被改了，
 * 实际正文没变、改动只出现在聊天记录中。
 *
 * 规则（确定性、无副作用、可单测）：
 * 1. 提到大纲层对象（大纲/标题/摘要/章节结构…）→ 不抢为正文候选，交回大纲意图流程；
 * 2. 必须同时具备「正文范围指代」或「明确的改写动词」；
 * 3. 只有"改/删/换"这类弱动词时，必须同时指代正文对象；
 * 4. 疑问语气（吗/什么/多少/怎么/哪些…）且没有明确改写动词 → 视为咨询，避免误判提问。
 *
 * 判不准也不会丢工作：面板对普通回复提供"按此指令修改正文"的兜底入口。
 */

/** 指代当前正文对象（"这段""本章""开头"…）。 */
const SCOPE_TERMS = [
  '正文', '本章', '这章', '这一章', '当前章', '最新章', '本章内容', '小说内容',
  '这一段', '这段', '那一段', '那段', '开头', '结尾', '末尾', '段落', '句子',
  '这句话', '这句', '那句话', '那句', '文字',
];

/** "第三章""第 3 章"这类具体章节指代。 */
const SCOPE_PATTERN = /第\s*(?:\d+|[一二三四五六七八九十百零〇两]+)\s*章/;

/** 明确的改写动词：单独出现即可判定为改写请求。 */
const STRONG_EDIT_TERMS = [
  '修改', '改写', '重写', '调整', '优化', '润色', '改善', '精简', '简化',
  '扩写', '扩充', '压缩', '缩短', '拉长', '加长', '删掉', '删除', '去掉',
  '替换', '换成', '改成', '改得', '更通顺', '更紧凑', '更生动', '更自然',
  '更流畅', '加强', '强化', '弱化', '削弱', '补写',
];

/** 弱改写动词：必须与正文范围指代同时出现。 */
const WEAK_EDIT_PATTERN = /[改删减补换润修缩扩增]/;

/** 疑问语气标记。 */
const QUESTION_PATTERN = /[?？]|吗|呢|是否|什么|多少|怎么|如何|为什么|为何|哪些|哪个|技巧/;

/** 大纲层对象：这些交给后端 outline 意图，不在此处判为正文候选。 */
const OUTLINE_TERMS = [
  '大纲', '标题', '摘要', '章节名', '章节简介', '章节结构', '章节安排', '剧情弧', '弧线', '伏笔台账',
];

/** 是否为"修改正文"的指令。 */
export function isProseEditRequest(text: string): boolean {
  const t = (text || '').trim();
  if (!t) return false;
  if (OUTLINE_TERMS.some((term) => t.includes(term))) return false;

  const hasScope = SCOPE_TERMS.some((term) => t.includes(term)) || SCOPE_PATTERN.test(t);
  const hasStrongVerb = STRONG_EDIT_TERMS.some((term) => t.includes(term));
  const hasWeakVerb = WEAK_EDIT_PATTERN.test(t);
  const isQuestion = QUESTION_PATTERN.test(t);

  // 没有任何修改动作 → 咨询
  if (!hasStrongVerb && !hasWeakVerb) return false;
  // 只有弱动词时必须指代正文对象（"这一章改了吗"不算，见下）
  if (!hasScope && !hasStrongVerb) return false;
  // 疑问语气：只有弱动词，或没有指代正文对象时，都按咨询处理
  if (isQuestion && (!hasStrongVerb || !hasScope)) return false;

  return true;
}
