import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// 同一测试在完整工作区、独立 Sites 和独立 Python 仓库运行，门禁显式指定目标。
export function browserTestRuntimes(moduleUrl) {
  const directory = dirname(fileURLToPath(moduleUrl));
  const candidates = [
    { file: resolve(directory, '../../scripts/static/app.js'), cloud: false },
    { file: resolve(directory, '../public/legacy/app.js'), cloud: true },
  ];
  const mode = process.env.TOUZI_BROWSER_TEST_RUNTIME || 'auto';
  if (!['auto', 'static', 'cloud', 'both'].includes(mode)) {
    throw new Error('TOUZI_BROWSER_TEST_RUNTIME must be auto, static, cloud or both');
  }
  const selected = candidates.filter(target => mode === 'auto' ? existsSync(target.file)
    : mode === 'both' || (mode === 'cloud') === target.cloud);
  if (!selected.length || selected.some(target => !existsSync(target.file))) {
    throw new Error(`Required ${mode} browser runtime is missing`);
  }
  return selected;
}
