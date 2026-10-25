import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// 两个独立仓库都核验自身清单；完整工作区还核验对侧副本，拒绝漏同步。
export function verifyBrowserTestCopies() {
  const directory = dirname(fileURLToPath(import.meta.url));
  const manifest = JSON.parse(readFileSync(resolve(directory, 'browser-test-manifest.json'), 'utf8'));
  const peer = existsSync(resolve(directory, '../public/legacy/app.js'))
    ? resolve(directory, '../../tests/browser') : resolve(directory, '../../webapp/scripts');
  for (const [name, expected] of Object.entries(manifest)) {
    const content = readFileSync(resolve(directory, name), 'utf8').replace(/\r\n/g, '\n');
    const digest = createHash('sha256').update(content).digest('hex');
    if (digest !== expected) throw new Error(`Browser regression manifest differs: ${name}; run python scripts/sync_browser_tests.py`);
    if (existsSync(peer)) {
      const other = readFileSync(resolve(peer, name), 'utf8').replace(/\r\n/g, '\n');
      if (other !== content) throw new Error(`Browser regression copies differ: ${name}; run python scripts/sync_browser_tests.py`);
    }
  }
}
