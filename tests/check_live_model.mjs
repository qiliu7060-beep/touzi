// 独立 Python 数据仓库直接运行 Worker 的同一份模型校验器。
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === './live-scoring.mjs') return nextResolve('./live-scoring.mts', context);
  return nextResolve(specifier, context);
} });
const { validateModel } = await import('../scripts/live-model-validation.mts');
const path = process.argv[2];
if (!path) throw new Error('Usage: node tests/check_live_model.mjs dist/live-model.json');
const bytes = readFileSync(path);
if (bytes.byteLength > 12 * 1024 * 1024) throw new Error('Model exceeds Worker 12 MiB limit');
const model = validateModel(JSON.parse(bytes.toString('utf8')), Date.now());
console.log(`Worker model gate passed: ${Object.keys(model.entries).length} stocks, model ${model.model_version}, built ${model.built_at}`);
