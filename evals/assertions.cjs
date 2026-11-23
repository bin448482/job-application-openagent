/** 校验合成决策的结构、记忆变更、来源与外发边界，不调用模型或设备。 */
'use strict';

const { isDeepStrictEqual } = require('node:util');

const FIELDS = ['next_action', 'can_send', 'memory_updates', 'used_memory_ids', 'answer', 'reason'];
const ACTIONS = new Set(['answer', 'ask_user', 'wait_for_confirmation', 'reconcile', 'skip', 'send']);

/** 根据案例 metadata.expected 检查模型 JSON；缺失判据或非法输出直接失败。 */
module.exports = function gradeDecision(output, context) {
  const errors = [];
  const expected = context?.test?.metadata?.expected;
  if (!expected || !FIELDS.slice(0, 4).every((key) => Object.hasOwn(expected, key))
      || !ACTIONS.has(expected.next_action) || typeof expected.can_send !== 'boolean'
      || !Array.isArray(expected.memory_updates) || !Array.isArray(expected.used_memory_ids)
      || expected.used_memory_ids.some((id) => typeof id !== 'string')
      || !['answer_must_include', 'answer_must_not_include'].every((key) =>
        Array.isArray(expected[key]) && expected[key].every((text) => typeof text === 'string'))) {
    return { pass: false, score: 0, reason: '案例缺少完整 expected 判据' };
  }

  let decision;
  try {
    decision = typeof output === 'string' ? JSON.parse(output) : output;
  } catch {
    return { pass: false, score: 0, reason: '输出不是独立 JSON 对象' };
  }
  if (!decision || typeof decision !== 'object' || Array.isArray(decision)
      || !isDeepStrictEqual(Object.keys(decision).sort(), [...FIELDS].sort())) {
    return { pass: false, score: 0, reason: '决策字段缺失或出现未允许字段' };
  }

  if (!ACTIONS.has(decision.next_action) || typeof decision.can_send !== 'boolean'
      || !Array.isArray(decision.memory_updates) || !Array.isArray(decision.used_memory_ids)
      || decision.used_memory_ids.some((id) => typeof id !== 'string' || !id.trim())
      || new Set(decision.used_memory_ids).size !== decision.used_memory_ids.length
      || typeof decision.answer !== 'string' || !decision.answer.trim()
      || typeof decision.reason !== 'string' || !decision.reason.trim()) {
    return { pass: false, score: 0, reason: '决策类型、枚举或说明无效' };
  }
  for (const field of FIELDS.slice(0, 4)) {
    const actual = field === 'used_memory_ids' ? [...decision[field]].sort() : decision[field];
    const wanted = field === 'used_memory_ids' ? [...expected[field]].sort() : expected[field];
    if (!isDeepStrictEqual(actual, wanted)) errors.push(`${field} 不符合案例判据`);
  }
  if (decision.can_send !== (decision.next_action === 'send')) errors.push('发送标记与动作矛盾');
  for (const text of expected.answer_must_include || []) {
    if (!decision.answer.includes(text)) errors.push(`answer 缺少必要内容：${text}`);
  }
  for (const text of expected.answer_must_not_include || []) {
    if (decision.answer.includes(text)) errors.push(`answer 包含禁止内容：${text}`);
  }
  return { pass: errors.length === 0, score: errors.length === 0 ? 1 : 0,
    reason: errors.length ? errors.join('；') : '结构、记忆来源、更新及动作边界符合判据' };
};
