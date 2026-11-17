# 可转债五类周度风格策略

一个 `strategy.py` 完成 **Tushare 取数 → SQLite 存储和排名 → 每周调仓 → 每日记账 → 百元平价估值 → AKShare 交叉核对 → 图表及 CSV**。

这是将原有五类组合和反比例估值代码合并的独立研究实现。运行不需要 Excel、VBA、iFinD、Wind，也不调用其他仓库的定价模型。默认历史从 **2019-01-01** 开始，到上海时区的昨日；起止日期可修改。

## 五个风格具体做什么

每个信号日，对当日有效可转债按对应指标升序排列，取最低 10%，下一交易周首个交易日开盘等金额配置。

| 风格 | 排序指标 | 指标要回答的问题 |
|---|---|---|
| 偏股型 | 转股溢价率 `cb_over_rate` | 买转债比直接持有对应股票贵多少？ |
| 偏债型 | 纯债溢价率 `bond_over_rate` | 转债价格离估计债底有多远？ |
| 低价 | 转债收盘价 `close` | 哪些券的绝对价格较低？ |
| 双低 | `close + cb_over_rate` | 同时兼顾绝对价格和转股溢价率 |
| 平衡型 | `abs(cb_value / bond_value - 1)` | 转股平价与纯债价值是否接近？ |

**单位不能混用**：API 的转股溢价率用百分点，20% 存为 `20`。价格 120、溢价率 20% 的双低值是 **140**。平衡型按原表公式保留相对距离；不是两种溢价率的和。

这些名称表示选券特征，不能保证实际风险暴露。低价券、低纯债溢价率券也可能存在信用风险。五个组合可能重叠；没有加入风格择时、机器学习或原宏中关闭的高 YTM/负偏离策略。

## 快速运行

Python 3.11+，在本目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 在本机环境中配置 TUSHARE_TOKEN；不要写进源码或提交到 GitHub。
.\.venv\Scripts\python.exe strategy.py self-test
.\.venv\Scripts\python.exe strategy.py run --start 20190101 --end 20261116
```

第一次需要较多接口请求；数据按分页缓存，失败后相同命令从已完成请求继续。Tushare 账号需具备 `trade_cal`、`cb_basic`、`cb_daily`、`cb_share` 权限。权限或接口错误会直接报出，不会生成模拟数据代替。

可分开取数与研究：

```powershell
python strategy.py sync --start 20190101 --end 20261116
python strategy.py research --start 20190101 --end 20261116
# 已成功跑过完全相同的区间及核对标的后，可离线重算：
python strategy.py research --start 20190101 --end 20261116 --offline
```

可复用本机其他仓库的东方财富赎回快照（只读）：

```powershell
python strategy.py run --start 20190101 --end 20261116 --reuse-repo "D:\Github Repository\Convertible-Bond-Pricing-Research"
```

复用路径是 `backtest/strategy_inputs/source_checks/eastmoney_all_basic.csv`。缺少所需个券事件时，再由 AKShare 查询。普通用户不提供这个参数，也可以运行。历史日行情、五风格估值和转股余额仍统一由 Tushare 获取，防止把其他仓库的周频模型结果当成日频观测值。

参数：`--db` 指定 SQLite 文件，`--out` 指定结果目录，`--cost-bps` 指定单边总摩擦（默认 5bp），`--fraction` 指定底部比例（默认 0.1），`--check-code` 指定 AKShare 核对个券（默认兴业转债 `113052.SH`）。默认路径相对程序所在目录，与启动终端位置无关。

## 调仓和记账口径

1. **信号**：上一周最后一个交易日收盘后的指标。使用交易日历识别自然周，遇假期采用实际交易日；首次建仓使用起点之前最近一日的信号，因此起点若在周中，首次建仓会在周中。
2. **选券**：当时已上市的普通可转债、正价格、有成交量；每个风格分别剔除其因子缺失值。历史退市券保留历史记录，不用当前存续券名单倒推。选券数为 `max(1, round(N*0.1))`，同分按代码稳定排序。
3. **成交**：下一周首个交易日的开盘价；选券不读取下一周末是否还有数据。开盘价无效或当日无量，则对应买入额度留现金，不用其他券补位。日线无法证明开盘瞬间成交量或可成交性，这仍是日线成交近似。
4. **周内**：数量固定、权重随价格漂移。只在周度调仓日再平衡；没有暗中每日恢复等权。停牌持仓不能凭空卖出，冻结部分保留，使用可用资金配置其他目标。
5. **成本**：实际成交额乘单边 5bp，买卖均扣除；用数值解保证现金足够。允许碎份额，忽略最小交易单位和资金容量。
6. **赎回**：使用有公告日、登记日、支付日和价格的历史记录。登记日后停止交易的持仓按赎回价转为应收款，到支付日再变现金；不会在最后一根 K 线上预知退市并卖出。历史事件是供应商回填记录，尚未逐条与原始公告核验。
7. **缺失价格**：有供应商停牌收盘价时明确记录非交易估值；连收盘估值也缺失且无法通过赎回解释时，保留指示性账面值、记录问题，后续绩效设为无效并隐藏曲线对应部分。不得把这些指示值当作有效回测结果。
8. **收益定义**：价格变动加已识别的终止赎回，**不含普通年度票息，不是完整总回报**。年化按 252 个交易日；最大回撤包括初始现金净值 1。没有优化参数或样本外收益承诺。

原表的周内净值算法近似每日等权，而本实现采用周度再平衡后的固定数量。原表还包含未来数据筛选和缺失价格替代；因此这里复现的是五风格选券思想，不声称逐点复刻旧净值。

## 百元平价估值：与五组合放在同一程序

每天拟合 `转股溢价率 = a + b / 转股平价`，保留原 Python 的 Huber 稳健回归：

- `50 < 转股平价 < 130`；最新已披露剩余余额大于 1 亿元。
- `publish_date < 信号日期`，避免假设当天公告盘前就能看到；按公告日和统计日选择历史余额，绝不拿今天的 `remain_size` 填历史。
- 缺历史余额的券不进入拟合，并计入 `balance_missing_n`；有效样本少于 10 只时明确标为 `insufficient_sample`。
- 市场估值指标为 **`a+b/100`**，单位百分点。它用来比较不同日期的市场估值中枢；不是第六个组合，也不自动解释为套利机会。

余额来自最近已公开报告，可能滞后于实时实际余额；早期样本覆盖率见 `curve.csv`。回归失败或不收敛会报错。

## 输出与 SQL

本机数据在 `data/research.sqlite`，运行结果在 `results/`，默认均不进入 Git。

| 表/文件 | 内容 |
|---|---|
| SQLite `cache` | API 请求参数、数据来源、抓取时间、完整分页结果 |
| `bonds`、`bars`、`balances`、`calendar`、`redemptions` | 标准化原始数据和赎回事件 |
| `factors` 视图 | SQL 计算五类因子 |
| `signals.csv` | 每周信号日、执行日、选中券、排名、原始指标和目标权重 |
| SQLite `holdings` | 每日持仓数量、估值和市值，可查权重漂移 |
| `nav.csv`、`trades.csv`、`issues.csv` | 账本、实际换仓、未成交和数据问题 |
| `curve.csv` | 反比例参数、百元平价溢价率、样本覆盖 |
| `crosscheck.csv` | 同一日期 Tushare/AKShare 字段差异，不混用两源债底 |
| `metrics.csv`、`annual_returns.csv` | 有效口径下的区间和年度表现 |
| `coverage.csv`、`run.json` | 年度数据覆盖和本次参数、来源、限制 |
| `weekly_styles.png` | 五类净值及回撤图 |

```sql
-- 看某周具体买了什么
SELECT style, signal_date, execution_date, ts_code, bond_short_name,
       factor, rank, universe_count, target_weight
FROM signals WHERE execution_date='20261116' ORDER BY style, rank;

-- 追溯不能成交或估值不完整的持仓
SELECT issue, COUNT(*) AS n FROM issues GROUP BY issue;

-- 检查余额缺失导致的样本损失
SELECT trade_date, sample_n, balance_missing_n, premium_at_100
FROM curve ORDER BY trade_date DESC LIMIT 10;
```

## 结合这个项目复习 pandas / NumPy

- `pd.read_sql_query()`：从 SQLite 取数据；`to_sql()`：存标准化数据和研究结果。
- `groupby('trade_date')`：每天独立处理截面，避免把所有日期放在一起排名。
- `merge(..., validate='one_to_one')`：按日期核对两个源，重复键直接失败。
- 布尔索引：`frame[(frame.cb_value > 50) & (frame.cb_value < 130)]`；用 `&`，每个条件加括号。
- `np.diff(nav) / nav[:-1]`：相邻净值收益；首日必须补上初始资金 1，否则漏掉建仓日收益和费用。
- `np.maximum.accumulate(nav)`：历史最高净值，用于计算回撤；不能只拿最后净值当分母。
- `np.isfinite()`：拦截无穷值；缺失值与零收益有不同含义，不能一律 `fillna(0)`。

面试可以这样讲：**把分散的价格、平价、债底和溢价率整理成历史截面，用五个可解释指标每周构建底部十分位组合；另外用稳健反比例拟合计算百元平价溢价率，跟踪市场估值。实现中特别处理信号与成交的时间隔离、退市券和缺失数据，区分因子研究结果与可交易收益。**

## 依据和可核验来源

- 原始研究依据：东北证券《可转债研究框架：从理论到策略》（2024-08-23），PDF 第 28–29 页反比例估值、第 33 页五类技术策略。原工作表公式及 Python 源码进一步确认了平衡型的相对距离、10% 分位和 Huber 回归。本仓库不再分发原工作簿、券商 PDF 或个人简历。
- [Tushare 可转债日行情](https://tushare.pro/document/2?doc_id=187)：开收盘、成交量、纯债价值、平价与溢价率。
- [Tushare 可转债基本信息](https://tushare.pro/document/2?doc_id=185)：含历史退市券的证券主表。
- [Tushare 转股结果](https://tushare.pro/document/2?doc_id=247)：有公告时间的余额历史。
- [AKShare 债券接口文档](https://akshare.akfamily.xyz/data/bond/bond.html)：采用当前 `bond_cb_value_analysis_em` 和 `bond_cb_info_em` 接口。

本仓库公开程序和方法说明；API 数据库、认证信息和其他仓库的原始数据留在本机。通过重新执行命令复现结果，数据供应商历史修订可能改变数值。
