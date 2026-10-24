const OUTLINE_FAILURE_MESSAGES: Record<string, string> = {
  outline_json_missing: '模型已返回，但没有形成结构化大纲修改。',
  outline_schema_invalid: '模型返回的大纲结构不完整，本次未应用任何修改。',
  node_reference_invalid: '模型返回的卷或剧情弧无法对应当前大纲，本次未应用任何修改。',
  node_reference_ambiguous: '模型返回的卷或剧情弧无法唯一对应当前大纲，本次未应用任何修改。',
  saved_chapter_move_forbidden: '已有正文的章节不能跨卷移动，本次未应用任何修改。',
  candidate_create_failed: '模型修改已通过校验，但候选保存失败，本次未应用任何修改。',
  outline_field_invalid: '模型返回的大纲字段格式不正确，本次未应用任何修改。',
  outline_parent_invalid: '模型返回的卷、剧情弧或章节归属不一致，本次未应用任何修改。',
  arc_range_invalid: '模型返回的剧情弧章节范围存在遗漏、重叠或越界，本次未应用任何修改。',
  saved_chapter_delete_forbidden: '已有正文的章节不能从大纲中删除，本次未应用任何修改。',
  outline_scope_too_broad: '一次重排全书章节的范围过大，请缩小到具体卷、剧情弧或章节。',
  outline_context_too_large: '当前大纲上下文仍超过安全上限，模型未被调用。',
  outline_instruction_missing: '请明确说明需要修改的大纲内容。',
};

export function outlineFailureMessage(code = '', traceId = ''): string {
  const message = OUTLINE_FAILURE_MESSAGES[code]
    || '模型已返回，但大纲修改未通过安全校验，本次未应用任何修改。';
  return traceId ? `${message}诊断编号：${traceId}` : message;
}

type ChatFailure = {
  intent?: string;
  mode?: string;
  error_code?: string | null;
  outline_failure_code?: string;
  reply?: string;
  trace_id?: string;
  request_id?: string;
};

export function chatFailureMessage(result: ChatFailure): string {
  const traceId = result.trace_id || result.request_id || '';
  if (result.intent === 'outline_edit' && result.outline_failure_code) {
    return outlineFailureMessage(result.outline_failure_code, traceId);
  }
  const message = result.reply?.trim() || '模型请求失败。';
  return traceId ? `${message}诊断编号：${traceId}` : message;
}
