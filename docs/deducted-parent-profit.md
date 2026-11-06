# 五年扣非归母净利润独立采集（默认关闭）

2026-11-06只读核验后实现，并适配主分支 `f1f7f3991c08ecd8e59394b3229c92d2c149bbe2`。
本改动仅实现和离线验证，默认不启用生产采集，不重新发布。历史快照不改写。

## 来源和已确认范围

现有三表 `lrb/llb/fzb` 不提供已验证的目标映射。独立读取同一新浪API的 `source=gjzb`，
精确选择 `item_field=NPCUT`、`item_title=扣非净利润`、`item_source=ysb`。
接口：`https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022`。
参数：正确市场前缀的 `paperCode`、`source=gjzb&type=0&page=1&num=40`。
报告键为12月31日，只接受合并期末、人民币，金额已为元，不乘万或股数。

2024年度核对（人民币元）：

| 股票 | 新浪NPCUT、东财KCFJCXSYJLR及年报扣非归母金额 |
| --- | ---: |
| 贵州茅台600519 | 86,240,905,977.42 |
| 公牛集团603195 | 3,741,807,159.86 |
| 万科A000002 | -45,393,719,289.83 |
| 招商银行600036 | 148,011,000,000（年报148,011百万元） |

2021年度也核对这四只股票。样本及公告链接在 `tests/fixtures/deducted_parent_verified.json`。
该文件是此前只读调查结果的人工摘录，不是完整原始HTTP响应，不宣称拥有原始响应哈希。
测试构造的HTTP响应及其哈希仅用于验证解析、缓存机制，均为合成数据。

金额权威依据是公告中的“归属于上市公司股东的扣除非经常性损益的净利润”，
银行对应“扣除非经常性损益后归属于本行股东的净利润”，不是普通合并或归母净利润。

## 版本和披露时间限制

新浪茅台2021年返回52,581,102,656.24，东财返回52,556,464,900.24；公牛分别为
2,632,476,489.56和2,632,296,518.65。后续年报列有调整前后金额。
因此不自动以东财兜底，不把供应商间差异视为浮点误差，也不保证新浪序列是最新追溯调整值。
版本标记为 `vendor_report_year_series_restatement_unverified`。

新浪 `publish_date` 保存为 `supplier_disclosed_date`，不是已核验的实际披露时刻。
例如招商银行2021年API日期为2022-03-19，公司/港交所已有2022-03-18公开文件。
`disclosed_at=null`、`disclosure_status=unverified`、`point_in_time_eligible=false`。
新浪gjzb响应的“其他/未审计”标签原样保留，不用于推断正式年报的审计意见。
完整首次披露时刻、每次更正版本链及全市场覆盖仍未验证。

## 接入接口与不变量

- `normalize_deducted_reports(code, reports, fetched_at, response_sha256, url=None)`：独立DataFrame。
  金额先用Decimal解析，保留精确金额字符串、原值列表、来源URL及响应SHA256。
  相同字段重复且值一致可去重；冲突、币种/范围/字段身份变动或缺失保留缺失原因，不替代。
- `fetch_deducted_parent_profit(code, cache, ttl_days=1)`：8秒超时，4MiB响应上限，最多两次尝试，
  独立缓存键 `deducted_parent_v1_{市场代码}`；不清理、覆盖现有三表/财务指标缓存。
  全部字段无效按失败处理，保留旧好缓存；部分缺失逐年明确记录。
  失败旧缓存不重写获取时间，返回独立cache_status、失败阶段和字段缺失原因。
- `fetch_financial_statements(..., include_deducted=False)`：默认仍仅读取三表。
  显式开启时，三表最多3线程结束后另行读取关键指标，加入字典键 `deducted_parent`。
- `analyze_financials` 仅把该数据填入原五年窗口的 `deducted_parent_net_profit`，
  附加 `deducted_parent_evidence`；不改三表的status、获取时间、metrics及亏损年份定义。
- `general.collect_deducted_parent_profit=false` 为默认值；Screener仅在值确为布尔true时开启。
  `point_in_time=True` 仍不消费当前三表或扣非缓存。
- 超过TTL的金额可供展示，并标为stale；未来/无时区的获取时间及未来供应商公告日不展示金额。
  页面逐年显示独立来源、获取时间、供应商公告日“未独立核验”标记；旧缓存有明确标签。
- 不跳过缺年，不改五年窗口，不用季度/TTM/预告，不补零，不按EPS反推，不跨源混版本。
- 原质量权重、买卖规则完全不变。原覆盖聚合自动统计新展示字段；覆盖提高不等于评分核验程度提高。

## 离线验证

`python -m unittest discover -s tests -p 'test_*.py' -q`

新增 `tests/test_deducted_profit.py`，包含23项测试；所有HTTP请求均mock。
其中执行 `tests/test_deducted_profit_ui.cjs` 检查实际财务表渲染器、单位、零值、过期状态、
独立获取时间、未核验公告日、HTML转义和旧快照兼容。
对比有效、缺失、过期扣非输入的质量分、子分、完整买入/卖出Check及最终动作，要求一致。
测试同时检查公开JSON保留逐年证据、历史时点评分不使用当前扣非数据、默认关闭不新增请求。

## 提交范围与后续生产操作边界

1. 本次草稿PR只发布代码、文档和离线样本，不启用开关，不运行生产数据任务。
2. 隔离试采：批准后对上述4只股票、2021—2025窗口发起最多4个初始请求及有限重试，
   使用新隔离目录；验证返回范围、单位、版本及披露证据，不写生产缓存或公开快照。
   此次原始响应应单独保存，记录抓取时刻与SHA256，进一步固定为完整离线fixture。
3. 生产启用/全市场发布：分别批准后，启用开关，冷缓存按3596只估算新增3596次逐股请求，
   最多一次重试/股。只向未来新快照增加展示金额和证据，历史快照保持原样。
   先比较同一已固定输入下分数/Check/动作是否相同，再发布；不能拿两个交易日结果作不变量验证。
4. 新源故障可关闭采集开关。现有三表和评分继续工作；不删除历史快照或回填历史收益。

无需也不应重做 `bc65589d` 的财务指标独立来源时间修复。
