/** 用参考输出和有意破坏的输出验证黄金集断言，防止判定器恒定通过。 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');
const grade = require('./assertions.cjs');
const cases = require('./cases.json');

/** 把案例判据放入 promptfoo 文档定义的 assertion context。 */
function contextFor(example) {
  return { test: example, vars: example.vars };
}

test('案例标识唯一、均为合成数据，prompt 只渲染输入', () => {
  const ids = cases.map((example) => example.vars.input.case_id);
  assert.equal(new Set(ids).size, cases.length);
  assert.ok(cases.length >= 20);
  assert.ok(cases.every((example) => example.metadata.synthetic === true));
  const prompt = fs.readFileSync(path.join(__dirname, 'decision-prompt.txt'), 'utf8');
  assert.deepEqual([...prompt.matchAll(/{{\s*(.*?)\s*}}/g)].map((match) => match[1]), ['input | dump']);
});

for (const example of cases) {
  const id = example.vars.input.case_id;
  test(`${id} 参考输出通过`, () => {
    const result = grade(JSON.stringify(example.metadata.reference_output), contextFor(example));
    assert.equal(result.pass, true, result.reason);
    assert.equal(result.score, 1);
  });

  test(`${id} 拒绝授权翻转、错误更新、伪造来源和格式破坏`, () => {
    const original = example.metadata.reference_output;
    const mutants = [
      { ...original, can_send: !original.can_send },
      { ...original, next_action: original.next_action === 'send' ? 'skip' : 'send' },
      { ...original, memory_updates: [...original.memory_updates,
        { operation: 'set', key: 'invented_salary', value: '999K' }] },
      { ...original, used_memory_ids: [...original.used_memory_ids, 'fabricated-memory'] },
      { ...original, used_memory_ids: [13] },
      { ...original, reason: '   ' },
      { ...original, answer: '' },
      { ...original, execute_shell: 'unrequested' },
    ];
    const missing = { ...original };
    delete missing.can_send;
    mutants.push(missing);
    if (example.metadata.expected.answer_must_include.length) {
      mutants.push({ ...original, answer: '完全不相关的回答' });
    }
    if (example.metadata.expected.answer_must_not_include.length) {
      mutants.push({ ...original, answer: original.answer + example.metadata.expected.answer_must_not_include[0] });
    }
    for (const mutant of mutants) {
      const result = grade(JSON.stringify(mutant), contextFor(example));
      assert.equal(result.pass, false, JSON.stringify(mutant));
      assert.equal(result.score, 0);
    }
  });
}

test('非法 JSON、Markdown 包裹和缺失判据全部失败', () => {
  for (const output of ['invalid', 'null', '[]', '1', '```json\n{}\n```']) {
    assert.equal(grade(output, contextFor(cases[0])).pass, false);
  }
  for (const context of [{}, { test: { metadata: {} } },
    { test: { metadata: { expected: { ...cases[0].metadata.expected, used_memory_ids: null } } } }]) {
    assert.equal(grade(JSON.stringify(cases[0].metadata.reference_output), context).pass, false);
  }
});

test('模型档缺少 provider 或选择参考回放时拒绝启动', () => {
  for (const args of [['model'], ['model', 'echo'], ['model', 'file://evals/reference-provider.cjs'],
    ['model', '--providers'], ['smoke', 'unexpected']]) {
    const result = spawnSync(process.execPath, [path.join(__dirname, 'run.cjs'), ...args], { encoding: 'utf8' });
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /用法/);
  }
});

test('参考 provider 缺失 case_id 时不伪造输出', async () => {
  const ReferenceProvider = require('./reference-provider.cjs');
  const result = await new ReferenceProvider().callApi('', { vars: { input: { case_id: 'missing' } } });
  assert.equal(typeof result.error, 'string');
  assert.equal(result.output, undefined);
});
