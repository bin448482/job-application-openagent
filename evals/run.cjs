/** 运行本机 promptfoo 自检或显式选择 provider 的模型评测，产物只写入 data/evals。 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { spawnSync } = require('node:child_process');

/** 为离线自检回放参考输出；模型档仅接受显式 provider，不自动读取私人配置。 */
function main(args) {
  const [mode, provider, ...extra] = args;
  if (!['smoke', 'model'].includes(mode) || extra.length
      || (mode === 'smoke' && provider)
      || (mode === 'model' && (!provider || provider.startsWith('-')
        || provider === 'echo' || provider.includes('reference-provider.cjs')))) {
    console.error('用法：npm run eval:smoke，或 npm run eval:model -- "<provider-id>"');
    return 2;
  }
  const root = path.resolve(__dirname, '..');
  const runDirectory = path.join(root, 'data', 'evals', `${mode}-${randomUUID()}`);
  fs.mkdirSync(runDirectory, { recursive: true });
  const packageDirectory = path.join(root, 'node_modules', 'promptfoo');
  const packageJson = JSON.parse(fs.readFileSync(path.join(packageDirectory, 'package.json'), 'utf8'));
  const cli = path.join(packageDirectory, packageJson.bin.promptfoo);
  const cliArgs = [cli, 'eval', '--config', path.join(root, 'promptfooconfig.json'),
    '--providers', mode === 'smoke' ? 'file://evals/reference-provider.cjs' : provider,
    '--no-share', '--no-cache', '--no-write', '--no-progress-bar', '--no-table',
    '--output', path.join(runDirectory, 'results.json')];
  if (mode === 'smoke') {
    console.log('SMOKE：回放合成参考输出，仅验证评测链路；没有调用模型。');
  } else {
    console.log('MODEL：将合成场景发送给指定 provider；此结果才是模型评测。');
  }
  console.log(`本机报告目录：${runDirectory}`);
  const result = spawnSync(process.execPath, cliArgs, {
    cwd: root,
    stdio: 'inherit',
    env: { ...process.env, PROMPTFOO_DISABLE_TELEMETRY: '1', PROMPTFOO_DISABLE_UPDATE: '1',
      PROMPTFOO_CONFIG_DIR: path.join(root, 'data', 'evals', 'promptfoo'),
      PROMPTFOO_CACHE_PATH: path.join(root, 'data', 'evals', 'cache') },
  });
  if (result.error) console.error(result.error.message);
  return result.status ?? 1;
}

process.exitCode = main(process.argv.slice(2));
