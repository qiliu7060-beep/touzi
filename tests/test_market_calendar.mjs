import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { MARKET_CALENDAR, calendarQuoteFresh, calendarModelExpiry, closedBaseline, marketClock } from '../scripts/market-calendar.mjs';
const stamp = value => Date.parse(value + '+08:00');
const cases = [
  ['2026-10-24T10:00:00','2026-10-23T16:15:00','closed'],
  ['2026-10-25T20:00:00','2026-10-23T16:15:00','closed'],
  ['2026-10-26T09:29:59','2026-10-23T16:15:00','closed'],
  ['2026-10-26T09:30:00','2026-10-23T16:15:00',null],
  ['2026-10-26T09:31:00','2026-10-26T09:30:00','trading'],
  ['2026-10-26T09:33:01','2026-10-26T09:30:00',null],
  ['2026-10-26T11:45:00','2026-10-26T11:29:00','break'],
  ['2026-10-26T11:45:00','2026-10-26T11:27:59',null],
  ['2026-10-26T15:15:00','2026-10-26T15:00:00','closed'],
  ['2026-10-26T15:15:00','2026-10-26T14:59:59',null],
  ['2026-10-07T12:00:00','2026-09-30T16:15:00','closed'],
  ['2026-10-08T09:30:00','2026-09-30T16:15:00',null],
  ['2026-10-10T10:00:00','2026-10-09T16:15:00','closed'],
  ['2026-02-23T10:00:00','2026-02-13T16:15:00','closed'],
  ['2027-01-01T10:00:00','2026-12-31T16:15:00',null],
  ['2026-10-25T20:00:00','2026-10-22T16:15:00',null],
  ['2026-10-26T10:00:00','2026-10-26T10:02:00',null]
];
for (const [now, quote, expected] of cases) {
  assert.equal(calendarQuoteFresh({price:10,quote_ts:quote+'+08:00'},stamp(now),MARKET_CALENDAR),expected,now+' / '+quote);
}
assert.equal(marketClock(stamp('2026-10-25T12:00:00'),{...MARKET_CALENDAR,version:'invented'}),null);
const built = stamp('2026-10-23T23:51:22'), quote = '2026-10-23T16:15:00+08:00';
assert.equal(calendarModelExpiry(built,quote,MARKET_CALENDAR),stamp('2026-10-26T09:30:00'));
assert.equal(closedBaseline(built,quote,stamp('2026-10-25T20:00:00'),MARKET_CALENDAR),true);
assert.equal(closedBaseline(built,quote,stamp('2026-10-26T09:30:00'),MARKET_CALENDAR),false);
assert.equal(closedBaseline(stamp('2026-10-25T23:00:00'),quote,stamp('2026-10-25T20:00:00'),MARKET_CALENDAR),false);
// 直接执行两套浏览器实际时间入口，独立仓库也必须测到自身运行时。
const browserPaths = [...new Set([
  new URL('../public/legacy/app.js', import.meta.url).pathname,
  new URL('../scripts/static/app.js', import.meta.url).pathname,
  new URL('../../scripts/static/app.js', import.meta.url).pathname,
])].filter(path => existsSync(process.platform === 'win32' ? path.replace(/^\//, '') : path));
assert.ok(browserPaths.length, '发布仓库必须至少具备自己的浏览器运行时');
for (const path of browserPaths) {
  for (const [baselineDate, builtAt, checks] of [
    ['2026-10-23', '2026-10-23T23:51:22+08:00', [
      ['2026-10-25T20:00:00', true], ['2026-10-26T09:29:59', true],
      ['2026-10-26T09:30:00', false], ['2027-01-01T10:00:00', false],
    ]],
    ['2026-09-30', '2026-09-30T17:23:00+08:00', [
      ['2026-10-07T12:00:00', true], ['2026-10-08T09:30:00', false],
    ]],
  ]) {
    const window = {__SITE__: {trading_calendar: MARKET_CALENDAR, built_at: builtAt,
      as_of: baselineDate, quote_at: baselineDate + ' 16:15:00', quote_coverage: 1, entries: {}}};
    const source = readFileSync(process.platform === 'win32' ? path.replace(/^\//, '') : path, 'utf8');
    runInNewContext(source.replace(/\}\)\(\);\s*$/, 'window.calendarEntry={fresh:publicScoreFresh};})();'), {window});
    for (const [when, expected] of checks) {
      assert.equal(window.calendarEntry.fresh(stamp(when)), expected, path + ' ' + when);
    }
  }
}
console.log(`market calendar: ${cases.length} quote cases, weekend/holiday/opening/unknown-year/model boundaries passed`);
