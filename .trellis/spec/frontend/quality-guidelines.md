# 前端质量与验证

## 最小验证矩阵

按改动范围运行，不用无关的重型流程代替针对性测试。

### `scripts/static/` 或 Python 生成页面

```powershell
python scripts/static/ref_portfolio.py --out .tmp/ref.json
node scripts/static/test_engine.cjs .tmp/ref.json
node scripts/static/test_dom.cjs
python scripts/check_static.py
```

真实行情只在需要验证网络契约时运行：

```powershell
node scripts/static/smoke_quotes.cjs
python scripts/check_static.py --live
```

### `webapp/`

```powershell
cd webapp
npm run lint
npm run build
node scripts/test-holding-overview.mjs
node scripts/test-legacy-comparison.mjs
node scripts/test-risk-cockpit.mjs
```

## 必测情形

- 请求：成功、非 2xx、无效 JSON、超时或绑定缺失。
- 数据：空列表、部分字段缺失、旧行情、未来时间戳、覆盖率不足。
- 金额：最低佣金、生效方向不同的印花税、零股/整手边界、部分减仓。
- 身份：未登录 401、不同 owner 隔离、更新或删除不属于当前 owner 的记录。
- 页面：无数据时可读、错误可见、窄屏表格可横向滚动。

## 评审红线

- 不接受只修改测试期望来“适配”错误业务结果。
- 不接受仅靠 `check_static.py` 验证浏览器 JS；它看不到运行时 DOM 和金额计算。
- 不接受把网络偶发失败吞成空成功；未知状态必须显式呈现。
- 不接受在生成物上修补而没有更新源文件和生成流程。

## 自动发布门禁

Sites标准`npm run build`先执行发布回归，再构建框架；`npm run check:release`可独立复现。Python发布仓库使用`node scripts/check-browser-release.mjs`执行可移植浏览器/时间/覆盖合约，不得因没有webapp目录而跳过测试。实际生成后再执行`node scripts/static/test_dom.cjs dist/site.js`和actual-model校验，之后才上传。

浏览器测试来源与独立副本通过`python scripts/sync_browser_tests.py`维护，runner验证同步摘要；改变源码测试须同步，缺失或失配必须失败。CI触发路径包含日历源、时间/覆盖模块和门禁测试。必须通过故意破坏实际onclick、编辑替换和中文名称匹配证明门禁有效，不能只测试runner存在。

