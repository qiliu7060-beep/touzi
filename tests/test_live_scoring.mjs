import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { computeEntry, quoteFresh, pyRound } from '../scripts/live-scoring.mts';
const cases = JSON.parse(readFileSync(0, 'utf8'));
for (const fixture of cases) {
  const entry = computeEntry(fixture.model, fixture.model.entries['000001'], fixture.quote);
  for (const key of ['total','quality','policy','dividend','valuation','dy']) {
    const actual = entry[key], expected = fixture.expected[key];
    if (expected === null) assert.equal(actual, null, `${fixture.name} ${key}`);
    else assert.ok(Math.abs(actual - expected) < 1e-10, `${fixture.name} ${key}: ${actual} != ${expected}`);
  }
  assert.equal(entry.grade, fixture.expected.grade, fixture.name);
  for (const key of ['pe_percentile','pb_percentile','pe_percentile_source','pb_percentile_source','graham_product'])
    assert.equal(entry.valuation_detail[key], fixture.expected.valuation_detail[key], `${fixture.name} ${key}`);
  assert.deepEqual(entry.sell_checks, fixture.expected.checks, fixture.name);
}
assert.equal(pyRound(2.675, 2), 2.67);
assert.equal(pyRound(2.685, 2), 2.69);
assert.equal(pyRound(.5, 0), 0);
assert.equal(pyRound(1.5, 0), 2);
const now = Date.parse('2026-10-23T10:00:00+08:00');
const q = stamp => ({ code: '000001', price: 10, pe: 10, pb: 1, quote_ts: stamp });
assert.equal(quoteFresh(q('2026-10-23T09:58:00+08:00'), now), 'trading');
assert.equal(quoteFresh(q('2026-10-23T09:57:59+08:00'), now), null);
assert.equal(quoteFresh(q('2026-10-23T10:01:01+08:00'), now), null);
assert.equal(quoteFresh(q('2026-10-23T11:29:00+08:00'), Date.parse('2026-10-23T12:00:00+08:00')), 'break');
assert.equal(quoteFresh(q('2026-10-23T15:00:00+08:00'), Date.parse('2026-10-23T16:00:00+08:00')), 'closed');
assert.equal(quoteFresh(q('2026-10-22T15:00:00+08:00'), Date.parse('2026-10-23T09:29:00+08:00')), 'closed');
assert.equal(quoteFresh(q('2026-10-22T15:00:00+08:00'), Date.parse('2026-10-23T09:30:00+08:00')), null);
console.log(`Python/Worker parity: ${cases.length} same-input cases; freshness and rounding passed`);
