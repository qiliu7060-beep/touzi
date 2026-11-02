# 财务质量指标独立来源追踪

2026-11-02。此修复只增加取数证据及展示，不改变评分数值、权重、买卖条件、排序、
11月1日覆盖率口径或历史快照，也不补五年扣非归母净利润。

## 丢失位置与修复链路

1. `Cache` 已有 `saved_at()`，但 `cached_fetch()` 原先只返回 DataFrame，无法区分
   命中、成功刷新及失败回退。新增可选 `metadata` 输出参数，默认返回类型不变。
   无新增上游请求；TTL、重试次数及允许旧缓存的策略保持不变。
2. 新浪财务指标标准化原先创建新表时丢掉取数证据。现在将独立来源元数据放入
   `DataFrame.attrs.indicator_source`；来源失败无数据时由异常携带同一元数据，
   `_fetch_one()` 保留为 `fin_metadata`。空/异常表结构保留来源状态，不补数值。
3. `summarize_financials()` 为 ROE、现金流比率及稳定性分别列出实际参与计算的报告期：
   与原计算完全相同的逐列去空值、末3期或配置的稳定性期数。季度回退显式标注。
   不把全表最新季度日期说成所有年度统计量的报告期。
4. `QualityInput` 与 `score_quality()` 将证据放入
   `financial_analysis.quality.indicator_evidence.{roe,cashflow,stability}`。
   原有 `public_entry()`、`site_js()` 和实时模型导出直接保留这个对象，不需要重算评分。
5. 收盘和实时模型消费者共同检查独立来源、缓存状态、年龄、带时区时间、TTL及
   新鲜度声明的一致性。字段缺失的旧快照兼容读取，展示未知；不迁移或改写旧文件。
6. 选股分数旁在原覆盖率提示之后增加独立取数提示；企业财务详情增加来源表，
   列出三项来源、报告期、取数时间依据、缓存状态、检查时间及正式披露时间。
   浏览器根据查看时间复核TTL，快照生成时新鲜不等于今天仍新鲜。

## 字段含义

| 字段 | 含义 |
|---|---|
| `source` / `api` | `sina-financial-analysis-indicator` / `akshare.stock_financial_analysis_indicator` |
| `report_periods` / `period_basis` | 实际参与此项计算的期间；annual 或 quarterly_fallback |
| `fetched_at` | 本次接口返回时间，或可信缓存写入时间；不是正式披露时间 |
| `fetched_at_basis` | `network_response` 或 `cache_saved_at`，明确区分取数时间依据 |
| `cache_saved_at` | 原有缓存元数据的写入时间；不以本次检查或失败重试时间覆盖 |
| `cache_status` | `hit`、`refreshed`、`stale_fallback`、`source_failed` |
| `source_status` | `failed` 表示本轮请求失败；命中时的 `ok` 仅表示缓存可读，不证明当前接口可连通 |
| `checked_at` / `ttl_seconds` / `age_seconds` | 此次读取时点、该数据源实际使用的TTL及缓存年龄 |
| `time_status` | `ok`、`missing`（含无法解析）、`unknown_timezone`、`future` |
| `freshness` | 读取时的 `fresh`、`stale`、`unknown`；不参与分数或交易条件 |
| `disclosed_at` | 当前接口未提供可核验的正式披露时间，明确为 null |

新缓存使用UTC带时区时间；原无时区缓存仍可按既有TTL策略复用，但其独立获取时间
显示未知，不猜测写入机器的时区，不为了补时间额外强制抓取。未来缓存时间不再被
误认为尚未过期；仍按既有流程尝试刷新，失败时回退并标为时间异常。

取数TTL只衡量取数年龄，不能证明上游已纳入最新报告或正式披露时点已核验。
旧缓存可继续参与原评分，但“参与评分/覆盖100%”与“新鲜度可核验”分别展示。
三表的 `financial_analysis.fetched_at` 仅解释三表，不用于填充这三项的独立时间。

## 验证

- `python -m unittest discover -s tests -p 'test_*.py'`
- `node scripts/check-browser-release.mjs`
- Sites 发布门禁 `npm run check:release` 与 TypeScript 检查。

`test_financial_provenance.py` 覆盖正常命中（不触网）、初次及过期刷新成功、
失败回退（保留旧时间）、失败无缓存、缺失/坏元数据、无时区/未来时间、异常表结构、
每项真实计算报告期，以及 Python评分→收盘导出→消费者校验→来源HTML的完整链路。
以相同输入验证元数据状态变化不改变子项分、权重、覆盖率和贡献。
新增来源时间/报告期/披露日/来源被篡改的拒绝用例；两个独立浏览器运行时均验证
未知时间、旧缓存回退、来源表及HTML转义，保留既有表单节点和选股行为回归。
所有测试均使用确定性模拟样本，未全市场重抓或声称3598只指标已经过期。

`audit_quality_evidence.py` 在原CSV字段后追加各项独立来源及时间字段，
旧快照的缺项留空，不再将三表时间解释为财务指标的独立取数时间。
