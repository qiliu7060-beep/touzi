import { readFileSync, statSync } from 'node:fs';
import { resolve, dirname, sep } from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { validateCloseManifest, validateClosePart, MAX_MANIFEST_BYTES, MAX_PART_BYTES } from './close-score-validation.mts';

// 与线上消费者共用校验器，实际组装产物通过后才允许移动数据分支。
export function checkCloseSnapshot(input, now = Date.now()) {
  const supplied = resolve(input);
  const manifestPath = statSync(supplied).isDirectory() ? resolve(supplied, 'close-scores.json') : supplied;
  const root = dirname(manifestPath);
  function readJson(path, limit) {
    if (statSync(path).size > limit) throw new Error('收盘资产超过字节限制');
    const bytes = readFileSync(path);
    if (bytes.byteLength > limit) throw new Error('收盘资产超过字节限制');
    return { bytes, value: JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) };
  }
  const manifest = validateCloseManifest(readJson(manifestPath, MAX_MANIFEST_BYTES).value, now);
  let scored = 0;
  for (const shard of manifest.shards) {
    const path = resolve(root, shard.path);
    if (!path.startsWith(root + sep)) throw new Error('收盘分片路径越界');
    const { bytes, value } = readJson(path, MAX_PART_BYTES);
    if (createHash('sha256').update(bytes).digest('hex') !== shard.sha256) throw new Error('收盘分片摘要不匹配');
    scored += Object.keys(validateClosePart(value, manifest, shard)).length;
  }
  if (scored !== manifest.universe.scored) throw new Error('实际收盘分片覆盖不完整');
  return { score_date: manifest.score_date, scored, failed: manifest.universe.failed, shards: manifest.shards.length };
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (!process.argv[2]) throw new Error('用法：node scripts/check_close_snapshot.mjs <folder-or-manifest>');
  console.log(JSON.stringify(checkCloseSnapshot(process.argv[2])));
}
