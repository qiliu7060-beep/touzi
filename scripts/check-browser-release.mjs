import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { verifyBrowserTestCopies } from '../tests/browser/browser-test-sync.mjs';

// 独立 Python 发布仓库的必跑门禁；不依赖 webapp 或外部行情。
const root = fileURLToPath(new URL('../', import.meta.url));
verifyBrowserTestCopies();
for (const file of ['tests/browser/test-holdings-actions.mjs', 'tests/browser/test-financial-ui.mjs', 'tests/browser/test-pick-financial.mjs',
  'tests/test_market_calendar.mjs', 'tests/test_financial_coverage.mjs']) {
  const result = spawnSync(process.execPath, [file], {
    cwd: root, stdio: 'inherit', env: { ...process.env, TOUZI_BROWSER_TEST_RUNTIME: 'static' },
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
console.log('Static browser release regression gate passed');
