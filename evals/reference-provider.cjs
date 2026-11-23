/** 仅回放合成参考答案以自检 promptfoo 链路；不执行记忆推理、模型请求或设备操作。 */
'use strict';

const cases = require('./cases.json');

/** 为离线 smoke 返回案例参考输出；它的通过率不代表模型表现。 */
module.exports = class ReferenceProvider {
  /** 标记结果来源，避免把参考答案回放当作真实模型。 */
  id() {
    return 'synthetic-reference-only';
  }

  /** 按合成 case_id 查找参考输出，缺少案例时拒绝生成结果。 */
  async callApi(_prompt, context) {
    const example = cases.find((entry) => entry.vars.input.case_id === context?.vars?.input?.case_id);
    if (!example) return { error: '没有对应的合成参考案例' };
    return { output: JSON.stringify(example.metadata.reference_output) };
  }
};
