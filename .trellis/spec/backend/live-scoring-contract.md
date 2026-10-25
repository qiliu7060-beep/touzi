# 动态评分与网页卖出监控契约

## 1. 范围与触发场景

修改 Python 评分输入、公开模型导出、Sites 分钟级评分、提醒、新鲜度或发布流程时读取本规范。Python `Screener.score_one` 是权威公式；Worker 只使用实际捕获的公开输入作精确镜像，浏览器负责私人持仓和个人成本规则。

## 2. 签名与所有者

- `touzi/live_model.py::capture_input(...)`：捕获 live、非 point-in-time 输入；回测不得捕获。
- `export_model(res, cfg, now=None)`：生成 `live-model.json`；在线模式要求本轮股票全部有输入。
- `validate_export_model(model, expected_codes, now)`：生产者覆盖、时间和资源门禁，拒绝时抛 `ValueError`。
- `webapp/lib/live-scoring.mts::computeEntry(model, input, quote)`：唯一 Worker 纯评分镜像；`pyRound` 保持 Python 舍入，累加顺序为 valuation、quality、dividend、policy。
- `webapp/lib/live-model-validation.mts::validateModel(value, now)`：完整校验后才返回 Model。
- `GET /api/scores/live`：公共只读接口，不接受查询参数、股票列表、持仓或上游 URL。
- `python scripts/sync_live_runtime.py [--check]`：把上述两个 Worker 模块同步为独立 Python 仓库的 `scripts/*.mts` 副本，不能人工维护第二份公式。
- `node tests/check_live_model.mjs dist/live-model.json`：使用同一 Worker 校验器验证真实发布产物。

## 3. 输入、输出与环境契约

模型 `schema_version=1`、`model_version=touzi-live-v1`、`provenance=python-score-one-live-inputs`，包含 built_at、expires_at、as_of、quote_at、quote_coverage、settings、plan、fees、pools、entries。条目包含原始 valuation/dividend 输入、历史池引用、固定质量/政策分、公开 sell_checks、来源日期及计算截止日。

企业财务分析扩展采用 `touzi-live-v2`，仍沿用 schema_version=1，明确增加 financial_analysis 和财务预警 Check。消费者兼容 v1/v2 时须显式标记财务能力；v1 不能以空对象冒充新分析，v2 缺必要字段不得静默接受。具体口径见 [企业财务分析](./financial-analysis-contract.md)。

计算截止日不等于财报披露日或源数据抓取时间。已知时分别记录估值历史末日、财报报告期、最近除息日；未知抓取时间不能写成当前时间。模型绝不含私人持仓、买入成本、数量或身份。零值与缺失 null 不等价。

API 返回 `status=ok|partial|unavailable`，以及 model_version、built_at、as_of、quote_at、computed_at、provenance、plan、fees、entries、errors。每股行情时间独立核验；动态 sell_checks 的 hit、known、value、note、threshold、level 必须与相同输入的 Python 一致。固定条件保留自己的基础数据时间。

生产任务明确设置 Python 3.12、Node 24；运行 parity 测试不依赖另一个部署仓库的 webapp 目录。Node 测试可用 `TOUZI_TEST_PYTHON` 指定干净的 Python 解释器。

## 4. 校验与错误矩阵

| 条件 | 行为 |
| --- | --- |
| 模型为空、未完整捕获本轮代码、版本不兼容、错误阈值或非法数值 | 拒绝发布或返回 unavailable，不用旧分冒充新分 |
| 过期、未来超过一分钟、源交易日不匹配，或生成时输入已超龄 | 拒绝模型；日期校验不能用请求时间代替。没有日历能力的旧模型仍受原36小时门槛约束 |
| 模型超过 12 MiB、600 只股票、单池 10000 样本或总池 100 万样本 | 拒绝 |
| 发布报价覆盖低于 0.9 | 拒绝模型；保留既有收盘/跨日发布门禁 |
| 行情缺失、陈旧、异常时间或源请求失败 | 对应股票待核验；部分有效结果为 partial |
| 交易时段报价延迟超过两分钟，或未来超过一分钟 | 不发出新的确定性卖出提醒 |
| 午休/盘后/盘前/公告休市 | 有经核验日历时按最近有效交易日及下一开盘核验；展示收盘基线不等于新触发提醒。无日历能力保守兼容，未知年度待核验 |
| 未登录、空持仓、身份读取失败 | 显示相应状态，清空旧账户提醒，不在共享响应中读持仓 |

网络请求最多十批、每批 60 个代码，限时八秒，实际响应字节受限；失败不能缓存为空成功。私人响应 no-store，公共缓存也不得包含个人数据。

Worker 请求使用 redirect:manual 并在读 body 前拒绝非 2xx；workerd 不实现 redirect:error，会在发出请求前抛 TypeError。Node fetch 测试不能代替完整构建在 wrangler/workerd 下的真实接口验收。两类上游都必须回归拒绝 301/302/303/307/308；日志只记录固定阶段与脱敏错误，不包含身份、请求头或台账。

## 5. 正常、边界与错误示例

- 正常：基础财报相同、PE/PB/价格变化；估值、股息及综合分按现有公式更新，个人成本规则另按有效报价判断。
- 边界：部分股票无报价时仅有效条目可显示动态分；其余待核验。初次请求尚未返回前，动态列也必须显示待核验。
- 错误：模型生成于今天但 source quote 过期，或发布快照无对应股票；不得显示正常评分/监控。

## 6. 必需测试与验收点

- `tests/test_live_model.py`：捕获与回测隔离、Python/Worker 同输入完整 Check 一致、运行时副本一致、真实 JSON 正反校验。
- `tests/test_live_model_gate.py`：实际产物覆盖、空捕获、过期、配置及资源限制。
- `webapp/scripts/test-live-model-validation.mjs`、`test-live-score-service.mjs`：消费者输入、失败、覆盖和批次上限，北交所 92 代码。
- `webapp/scripts/test-live-monitor.mjs`：15/60 秒单一调度链、首屏旧分快照遮蔽、逐股时间、账户隔离、去重、升级、恢复及隐藏页面补查。
- 保留 buy/sell/freshness/valuation-threshold、费用和风险驾驶舱回归；TS lint/type/build 必须通过。
- `.github/workflows/update-site.yml` 必须在真实模型生成后、Upload 前执行 actual-model CLI；合成 fixture 通过不能替代实际产物校验。

## 7. 错误与正确做法

错误：加快刷新 HTML 或仅改 built_at，就标注实时；只比较 hit 而保留旧的 Check.value；把个人台账烘进公开模型；手工修改 runtime 副本后漏同步。

正确：请求版本化公开输入和有效行情，先验证再计算，逐股显示实际时间，缺失明确待核验；用完整 Check 对照和同步命令守住两条独立发布链。

网页只在可见时检查，隐藏/关闭不保证执行，不声称能回溯没有观察到的触发。提醒只在当前页内存保存；重复触发保留一条，完整核验恢复后可再次提醒。买入建议分组、宏观、报告及下载保持发布快照并明确标示。

## 8. 经核验日历与休市基线

`config/trading-calendar.json`记录来源、版本、覆盖年度和公告休市区间；`touzi/market_calendar.py`和`webapp/lib/market-calendar.mjs`实现同一交易语义。`scripts/sync_live_runtime.py`维护独立Python部署副本和两个浏览器的日历代码；不得人工维护不同假日表。行政调休周末不因此开市；日历覆盖范围外保守待核验，年度安排需在交易所公告后核查更新。

公开模型/静态站/API的`trading_calendar`是显式能力。严格核对受支持日历，不能仅信任外部JSON自报的闭市日期。收盘基线的`expires_at`精确受下一交易日09:30约束，并受有界日历搜索限制；盘中模型不因有日历就无限延期。周日和公告长假允许最近交易日有效完整收盘数据，下一开盘起旧报价退出。保留真实built_at/quote_at/fetched_at；消费者只在已核验休市基线中以真实生成时刻核验固定输入/财务获取状态，发布时陈旧数据不能借此复活。

`tests/test_market_calendar.py`、`tests/test_market_calendar.mjs`和Sites时间测试必须覆盖周五到周一09:29/09:30、午休、长假、调休周末、未知年度、假日历、未来时间、盘中延迟及生成时旧输入。API与浏览器的收盘展示和提醒抑制也需端到端测试，不能只有纯函数断言。

## 9. 旧部署到日历模型的受控衔接

一般`validateModel`不接受过期旧模型。仅固定公开MODEL_URL读取边界可调用`live-legacy-calendar.mts::migrateLegacyClose`：必须为无日历v2、原模型以真实built完整通过校验、原expires精确built+36小时、当前同一有效收盘基线。迁移只增加日历策略期限/覆盖摘要与明确来源说明，原条目、数值、built/quote/input/fetched/source_updated全部保持。已有错误日历、未知年、原生成时陈旧或未来数据、下一开盘一律拒绝。新有效上游优先，短缓存也保留迁移来源；新模型恢复后清除迁移文案。

固定上游暂不可用时可使用发布时核验的同一公开收盘快照作为启动回退；`live-legacy-bootstrap.mts`仅在有效闭市窗口懒解压，12MiB流式上限并校验原始SHA后再执行完整迁移校验。禁止在交易时段/未知年尝试复活快照。页面明确标记启动快照，不以回退冒充新财报或发新交易提醒；禁止夹带用户持仓。

Python CI通过`publish_eligibility.py`区分已核验交易日与整日休市：休市仍运行全部回归，成功跳过重算/上传/自定义部署；交易日09:30前，若同一日历最近交易日15:00收盘已超过36小时，也成功跳过重算，避免周一/长假后盘前用旧行情重建。普通次日盘前仍处36小时以内的收盘可继续原流程。09:30恢复正常发布资格，未知年度/无法确认前一交易日则失败。生产36小时源输入门槛不因此放宽。此门禁不能更改或担保GitHub仓库另行配置的默认branch Pages构建。
