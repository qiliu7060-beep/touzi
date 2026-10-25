# 跨层数据与发布链路

## 总体数据流

```text
外部行情/宏观源
  → touzi/data/*（抓取、标准化、缓存）
  → regime / screener / plans / tracker / portfolio（评分与决策）
  → scripts/run_all.py（编排）
  → output/*（结构化结果与报告）
  → scripts/publish_site.py（静态导出与新鲜度闸门）
  → dist/* / GitHub Pages
  → webapp/app/live/*（公开页代理与 legacy 回退）
```

个人持仓另有三条互不等价的路径：

- 本机 Python 服务：`config/holdings.csv` 或 `data/accounts.db`。
- 公开静态站：浏览器 `localStorage`，键 `touzi.ledger.v1`。
- Web App：D1 `holdings` 表，以 `oai-authenticated-user-id` 为 owner。

不要把其中一条路径的安全或一致性假设套到另一条路径。

## 边界一：时间与新鲜度

本项目的“今天”是北京时间交易语义，不是 GitHub runner 的 UTC 日期。行情日期、`quote_at`、收盘最小时间和覆盖率共同决定能否发布。

关键位置：

- `.github/workflows/update-site.yml` 根据早盘/收盘计划传入 `--min-quote-time`。
- `scripts/publish_site.py` 在发布前核验报价交易日、时间和覆盖率。
- `scripts/static/app.js::publicScoreFresh()` 在浏览器再次拒绝旧评分。
- `tests/test_score_quote_freshness.py` 是新鲜度回归测试。

规则：任何放宽发布条件的改动，都必须说明盘中、盘后、周末、节假日和数据源延迟时会发生什么。旧数据可以展示，但不能冒充今日评分或触发确定性交易动作。

发布端使用经交易所公告核验、范围有限的交易日历：休市及盘前允许最近已完成交易日的完整收盘行情，下一交易日`09:30`起旧报价退出；同日仍按调用方的`min_quote_time`、未来时间容差和覆盖率独立核验。Python、Worker及两浏览器使用同一日历来源，未知年份待核验。收盘基线复用不重写源时间，不绕过生成时的输入有效性；具体契约见`../backend/live-scoring-contract.md`。旧版无日历能力的消费者仍采取保守兼容，不能声称支持长假连续核验。

## 边界二：缺失数据不是零

网络抓取允许局部失败，但下游必须区分：

- 真正的数值 0；
- `None`/`NaN`/字段缺失；
- 数据源失败但命中旧缓存；
- 无缓存导致整个必要数据集不可用。

`touzi/util.py::cached_fetch()` 允许取数失败时退回缓存；`touzi/data/stock.py::parallel_map()` 允许单股失败；`plans.py` 对未知门槛使用 `known=False`。前端必须继承这种语义，不能把未知转为 0、通过或“一切照旧”。

## 边界三：金额与卖出规则

权威实现位于 `touzi/portfolio.py` 和 `touzi/plans.py`；`scripts/static/app.js` 为浏览器离线运行而镜像相关公式。费率、最低佣金、卖出比例、100 股取整、缺报价处理任何一处变化，都必须同步镜像并运行 `scripts/static/test_engine.cjs` 的逐字段对比。

不要在 React 组件或页面模板中再写第三套金额公式。若 Web App 需要同样的完整计算，应先设计共享的可验证契约，而不是复制片段。

## 边界四：公开数据与私人数据

- `dist/site.js` 只能包含公开评分、规则参数和公开基准行情。
- GitHub Pages、`/live/*` 代理和带 `public` 缓存头的响应不得出现用户持仓。
- D1 查询必须按 owner 过滤；客户端传入的 owner 不可信，owner 只来自请求头。
- Connector binding 与调用结果保持请求级、服务端、`no-store`。

## 修改检查表

- [ ] 已列出字段/规则的生产者和所有消费者。
- [ ] 已确认北京时间与交易时段边界。
- [ ] 已保留缺失、旧缓存、部分失败的显式状态。
- [ ] 已核对公开/私有数据边界。
- [ ] 已运行最靠近该边界的合约测试，而不是只跑一个页面冒烟。

