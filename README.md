# 中国市场可转债五类风格策略研究 | Convertible Bond Style Research

<p align="center">
  <a href="#简体中文"><img src="https://img.shields.io/badge/语言-中文-E84D3D?style=for-the-badge&labelColor=3B3F47" alt="中文"></a>
  &nbsp;
  <a href="#english-version"><img src="https://img.shields.io/badge/Language-English-2F73C9?style=for-the-badge&labelColor=3B3F47" alt="English"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/数据存储-SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite">
  <img src="https://img.shields.io/badge/策略-五类转债风格-F2C94C?style=for-the-badge" alt="Five convertible-bond styles">
  <img src="https://img.shields.io/badge/数据截止-2026--11--20-4CAF50?style=for-the-badge" alt="Data through 2026-11-20">
  <img src="https://img.shields.io/badge/调仓频率-周度-9B51E0?style=for-the-badge" alt="Weekly rebalancing">
</p>

<p align="center">
  <a href="examples/metrics.csv"><img src="https://img.shields.io/badge/组合指标-查看-2F73C9?style=for-the-badge" alt="Portfolio metrics"></a>
  <a href="examples/annual_returns.csv"><img src="https://img.shields.io/badge/年度收益-查看-4CAF50?style=for-the-badge" alt="Annual returns"></a>
  <a href="strategy.py"><img src="https://img.shields.io/badge/策略源码-Python-E84D3D?style=for-the-badge" alt="Python source"></a>
</p>

---

<a id="简体中文"></a>

## 简体中文

[English](#english-version)

## 项目概述

本项目研究中国可转债的偏股、偏债、低价、双低和平衡五类风格，以中证转债指数（000832.CSI）为基准，比较统一风险约束下的周度组合表现，并通过百元平价修正转股溢价率跟踪市场估值变化。风格选券与市场估值分别计算，百元平价指标不直接用于组合择时。

数据来自 Tushare、巨潮资讯、东方财富与 AKShare，以 SQLite 保存行情、历史公告和持仓账本。取数、因子计算、周度选券、风险退出和每日记账集中在一个 [strategy.py](strategy.py) 中，不依赖 Excel 或 VBA。

## 策略方法

五类组合均在风险合格池中按指标升序选取最低 10%，数量为 `max(1, round(N × 10%))`；同分按代码排序，调仓时等金额配置。不同风格可持有相同转债。

| 风格 | 排序指标 | 研究含义 |
|---|---|---|
| 偏股型 | 转股溢价率 | 观察转债价格相对转换价值的溢价 |
| 偏债型 | 纯债溢价率 | 观察转债价格相对估计债底的溢价 |
| 低价 | 转债收盘价 | 观察较低绝对价格的组合表现 |
| 双低 | 收盘价 + 转股溢价率（百分点） | 同时考虑价格与转股溢价 |
| 平衡型 | `abs(转股平价 / 纯债价值 - 1)` | 选择转换价值与债底相近的转债 |

转股溢价率 20% 在数据中表示为 20，因此价格 120、溢价率 20% 的双低值为 **140**。平衡型保留原研究中的相对距离公式，不使用两类溢价率之和。

### 百元平价估值

每日采用 Huber 稳健回归拟合 `转股溢价率 = a + b / 转股平价`。样本要求 `50 < 转股平价 < 130`，且当时已披露剩余余额大于 1 亿元；余额公告日必须严格早于计算日。缺少历史余额的样本排除并记录覆盖，有效样本少于 10 只时不输出拟合值。

`a + b / 100` 为百元平价修正转股溢价率，单位为百分点，用于比较不同日期的市场估值中枢。

## 组合规则与结果

正常再平衡使用上一周末收盘指标，在下一周首个交易日开盘执行；节假日按交易日历顺延。首次建仓使用起点前最近交易日的指标，起点在周中时也会在该日建仓。周内持仓数量固定，权重随价格漂移，买不到的额度保留现金。

风险过滤先于排名，排除已公开的正股 ST/退市风险、赎回或停止交易事件，以及前一日收盘价低于 **90 元**的标的。未知股票名称历史不视为风险已排除。公告日后才生效，并尊重正式生效日；周末到开盘前已知的新风险同样阻止买入。风险每日检查，触发后在下一可交易开盘退出，正常轮换仍按周进行。90 元为固定研究参数，未按收益调优。

单边综合摩擦为 **5bp**。停牌持仓冻结，确认停牌后沿用最后估值，复牌价格跳变进入收益；不能虚构停牌前卖出。赎回按登记日和付款日记应收款及现金。无法解释的缺价仍会使后续绩效失效。

### 全区间表现

<!-- RESULTS_ZH:START -->
共同区间为 **2019-01-02—2026-11-20**，共 **1,912 个交易日、404 个正常建仓/调仓日**。组合计入税前票息及已识别赎回，扣除单边 5bp 交易摩擦；年化按 252 个交易日计算。

| 组合 | 累计收益 | 年化收益 | 年化波动率 | 最大回撤 |
|---|---:|---:|---:|---:|
| 偏股型 | 471.33% | 25.82% | 23.39% | -31.89% |
| 偏债型 | 101.90% | 9.70% | 7.43% | -16.47% |
| 低价 | 105.84% | 9.98% | 8.35% | -19.95% |
| 双低 | 328.10% | 21.13% | 13.40% | -22.64% |
| 平衡型 | 61.24% | 6.50% | 13.50% | -27.77% |
| 中证转债指数（000832.CSI） | 49.86% | 5.48% | 10.50% | -21.60% |

已核验 9,560 条组合日净值、1,175 笔票息到账。持仓票息期间与估值未解决记录均为 0。
<!-- RESULTS_ZH:END -->

[组合指标](examples/metrics.csv) · [年度收益](examples/annual_returns.csv) · [数据覆盖](examples/coverage.csv) · [运行参数与来源](examples/run.json)

停牌估值、风险退出冻结及未成交记录保留在[问题分类汇总](examples/issue_summary.csv)中；有效账本不等于每一天的估值都可成交。

## 关键图表

### 五类风格与中证转债指数：净值及回撤

![Five convertible-bond styles and CSI Convertible Bond benchmark: NAV and drawdown](examples/weekly_styles.png)

五组合初始资金均为 1，指数为黑色虚线，使用 `当日收盘点位 / 2019-01-02 开盘点位（279.6077）` 归一化，以匹配策略首次建仓时点并保留首日收益。六条曲线覆盖相同交易日，缺任何指数报价都会报错，不前向填充或缩短区间。[每日比较净值](examples/comparison_nav.csv)可直接复核图表。

指数通过 Tushare `index_daily` 获取，采用官方公布点位，不另扣模拟交易成本。中证[编制方案](https://pdf.dfcfw.com/pdf/H2_AN202502251643475838_1.pdf)采用市值加权、全价计算；策略按实际公告计入税前票息并扣交易成本，指数直接采用公布点位，不额外添加模拟票息或扣费。图表用于市场参照，收益差不能直接解释为纯选券超额。策略历史已施加当前风险规则，不是事前实盘记录。

### 百元平价修正转股溢价率

![Market conversion premium at parity 100](examples/premium_at_100.png)

该序列反映样本约束下的市场估值变化，不直接构成买卖信号。

### 票息与公告自动更新

常规票息从巨潮资讯发行人付息公告提取，保留公告日期、登记日、除息日、实际付款日、每张税前金额和原始 PDF 链接。登记日收盘持仓确定权益；除息日确认应收利息，付款日收盘转为现金，次日起可用于交易。登记日不重复增加全价持仓净值，除息日卖出仍保留已登记权益，除息日新买入不享有本次票息；到期兑付含末期利息时不得再加同一期常规票息。税率因投资者身份而异，本研究展示税前口径。

每次运行自动分页查询付息／兑付利息、赎回、停止交易、终止上市和摘牌公告；对分类检索遗漏的年度付息，按发行人跨分类补查。Tushare `cb_call` 全量分页和东方财富兑付数据补充赎回状态、登记日及实际付款信息。风险按公告可知日期处理，不能按后来退市结果提前剔除。完整请求和原文缓存在 SQLite，公告数量、补查覆盖和分类记录一并导出。

年度付息覆盖检查对照债券起息日与实际持仓，并逐条核对公告金额与年度合同利率：持有期间缺少已核验公告、金额/日期歧义、分页不完整或无法解释的估值，都使运行失败，公开成果保留上一版。新公告格式需要修正解析后重跑；不会把漏息当作零收入继续发布。[票息明细](examples/coupons.csv) · [到账汇总](examples/coupon_summary.csv) · [持仓期间覆盖](examples/coupon_coverage.csv) · [公告查询覆盖](examples/announcement_coverage.csv)

## 数据与实现

Tushare 提供历史行情、估值、转股余额公告、交易日历、股票名称变更、停复牌记录及中证转债指数日线。AKShare 用于独立核对和缺失行情补充。历史退市券保留，不用当前存续券名单倒推；两源债底口径存在差异，不自动混用。

| 文件或目录 | 内容 |
|---|---|
| [strategy.py](strategy.py) | 取数、SQLite、选券、记账及绘图的单文件入口 |
| [risk_events.csv](risk_events.csv) | 公告风险事件及来源；股票名称状态以完整 API 历史为准 |
| [verified_redemptions.csv](verified_redemptions.csv) | 有原始公告链接的到期兑付补充 |
| [quote_overrides.csv](quote_overrides.csv) | 2025-03-28 三条新浪 OHLCV 补充及相邻日单位核对 |
| [examples/](examples/) | 公开的组合指标、年度收益、来源记录和图表 |
| `data/research.sqlite` | 本地原始请求、标准化数据、信号、成交与持仓表 |
| `results/` | 本地选券明细、交易、净值、问题记录和回归结果 |

详细数据库及账本由 Git 忽略。报价补充只填真实 OHLCV，不补造平价、债底，也不覆盖冲突价格。原始公告链接保留在对应 CSV 中。

## 更新与复现

使用 Python 3.12+，安装 [requirements.txt](requirements.txt)，并在本机环境配置 `TUSHARE_TOKEN`。账号需具备 `cb_call`、`cb_basic`、`cb_daily`、`cb_share`、`trade_cal`、`namechange`、`suspend_d`、`index_daily` 的访问权限。

```powershell
python -m pip install -r requirements.txt
python strategy.py run --start 20190101 --end 20261120
```

已有数据库时可单独重算；相同参数在线运行成功后，可离线重现：

```powershell
python strategy.py research --start 20190101 --end 20261120 --offline
python strategy.py self-test
```

`--db`、`--out` 分别指定数据库与输出目录，`--cost-bps` 调整单边摩擦，`--fraction` 调整底部分位，`--check-code` 指定核对个券。默认路径相对程序所在目录。`--reuse-repo` 可只读复用另一个转债仓库的赎回快照，并核对日行情覆盖。

25 项内置测试覆盖票息权益、除息记账、延迟付款、赎回防重计、公告更正、基准、周度截止日和空行情缓存。完整回测另检查持仓票息覆盖、净值有效性和费用账本；离线复现需要相同参数的完整缓存。

### 周度自动更新

[GitHub Actions](.github/workflows/weekly-update.yml) 在北京时间 **周一至周五 19:01**（UTC 11:01）触发。定时运行以最近已到的工作日 19:01 所在日期为截止日，即使排队跨过午夜也按该日期查询交易所日历。仅在**每周最后一个交易日**更新，节假日缩短的交易周提前执行，整周休市跳过。旧本机 18:01 任务停用。

仓库 Secret `TUSHARE_TOKEN` 提供接口凭据。任务恢复 SQLite 缓存，自动更新行情、指数、票息和风险公告，运行回测与检查，再用 `publish` 同步图表、指标和双语 README 并提交。失败不会发布部分区间。手动运行支持指定截止日和强制重算；凭据不得写入代码或公开结果。

```powershell
python strategy.py week-check
python strategy.py run --start 20190101 --end YYYYMMDD --out results/weekly_candidate
python strategy.py publish --out results/weekly_candidate
```

手动执行 `week-check` 默认使用北京时间当天，其他研究命令默认使用昨天。自动任务将日历检查返回的截止日传给回测。日历、当日行情或指数缺失均报错，不默认固定周五、不前向填充。Tushare 公告权限不足或来源暂不可达时，需在 Actions 日志处理失败后重跑。

## 局限性

目前剩余限制主要是成交假设与实盘的差异，以及投资者税费和历史数据修订。常规票息已按公告计入税前收益，公告已接入自动刷新与缺失阻断。日线开盘成交允许碎份额，未模拟最小交易单位、盘口容量或排队成交。停牌估值可能低估区间波动；供应商可能修订历史，不能保证原始发布版本。风险规则在查看历史后确定，尚需未来样本验证，不能将本结果视为预期收益或实盘成交保证。

## 参考资料

- [东北证券 — 可转债研究框架：从理论到策略](https://github.com/ericxuzhesheng/Convertible-Bond-Pricing-Research/blob/main/report/Northeast_Securities_Convertible_Bond_Research_Framework_Theory_to_Strategy_20240823.pdf)：百元平价估值及技术性风格策略。
- [Tushare 可转债日行情](https://tushare.pro/document/2?doc_id=187) · [基本信息](https://tushare.pro/document/2?doc_id=185) · [转股结果](https://tushare.pro/document/2?doc_id=247) · [股票名称变更](https://tushare.pro/document/2?doc_id=100)
- [AKShare 债券数据接口](https://akshare.akfamily.xyz/data/bond/bond.html)

---

<a id="english-version"></a>

## English Version

[简体中文](#简体中文)

## Overview

This project compares five weekly Chinese convertible-bond styles under common risk controls: equity-like, bond-like, low-price, double-low and balanced. A separate robust regression tracks the market conversion premium at parity 100; it does not time the portfolios.

A single [strategy.py](strategy.py) handles Tushare, CNInfo, Eastmoney and AKShare data, SQLite storage, portfolio selection, risk exits and daily accounting. Excel and VBA are not required.

## Strategy methods

Each portfolio selects the lowest 10% of its eligible universe, with `max(1, round(N × 10%))` positions and deterministic code-based tie breaking. Positions receive equal notional allocations at rebalancing; portfolios may overlap.

| Style | Ranking variable |
|---|---|
| Equity-like | Conversion premium |
| Bond-like | Straight-bond premium |
| Low-price | Closing price |
| Double-low | Closing price + conversion premium in percentage points |
| Balanced | `abs(conversion parity / straight-bond value - 1)` |

A price of 120 and a conversion premium of 20% give a double-low score of **140**.

The daily Huber regression fits `conversion premium = a + b / parity`, using `50 < parity < 130` and outstanding balances above CNY 100 million disclosed strictly before the observation date. Missing balances are excluded and counted; fewer than ten observations produce no fitted value. The market indicator is `a+b/100`, in percentage points.

## Portfolio rules and results

Normal rebalancing uses the prior week's final closing indicators and the next week's first session open. Holidays follow the trading calendar. Initial entry uses the preceding session even if the requested start falls midweek. Quantities remain fixed between rebalances, except for risk exits and corporate actions. Unfilled entry slots remain cash.

Risk screening precedes ranking. Known underlying ST/delisting status, announced redemption or trading termination, and prior closing prices below **CNY 90** exclude new positions and trigger exits at the next tradable open. Announcement availability is delayed beyond its date and respects formal effective dates; unknown name histories are excluded. New risks known before execution also block entries. Risk monitoring is daily; normal selection remains weekly. The price threshold is fixed and was not optimized for returns.

One-way friction is **5bp**. Suspended holdings cannot be sold; confirmed suspensions carry the last mark, and resumed quotations capture the price jump. Identified redemptions become receivables and then cash on the relevant dates. Unexplained marks invalidate subsequent performance rather than being replaced by assumed par recovery.

<!-- RESULTS_EN:START -->
Common interval: **2019-01-02 to 2026-11-20**, **1,912 sessions and 404 initial-entry/rebalance dates**. Portfolios include gross coupons and identified redemptions, net of 5bp one-way friction; annualization uses 252 sessions.

| Portfolio | Cumulative return | Annualized return | Annualized volatility | Max drawdown |
|---|---:|---:|---:|---:|
| Equity-like | 471.33% | 25.82% | 23.39% | -31.89% |
| Bond-like | 101.90% | 9.70% | 7.43% | -16.47% |
| Low-price | 105.84% | 9.98% | 8.35% | -19.95% |
| Double-low | 328.10% | 21.13% | 13.40% | -22.64% |
| Balanced | 61.24% | 6.50% | 13.50% | -27.77% |
| CSI Convertible Bond (000832.CSI) | 49.86% | 5.48% | 10.50% | -21.60% |

Validated 9,560 portfolio-day NAVs and 1,175 coupon payments. No unresolved held coupon periods or valuation entries remain.
<!-- RESULTS_EN:END -->

[Portfolio metrics](examples/metrics.csv) · [Annual returns](examples/annual_returns.csv) · [Coverage](examples/coverage.csv) · [Run parameters and sources](examples/run.json)

Suspension marks, frozen exits and unfilled orders remain in the [issue summary](examples/issue_summary.csv); an accounted mark is not necessarily executable.

## Key figures

### Five styles vs CSI Convertible Bond: NAV and drawdown

![Five convertible-bond styles and CSI Convertible Bond benchmark: NAV and drawdown](examples/weekly_styles.png)

Portfolios start with capital of 1. The black dashed benchmark is `daily close / first-session open (279.6077 on 2019-01-02)`, matching initial portfolio entry and retaining the first session return. All six series cover identical sessions. Missing index dates fail validation; no forward filling or interval truncation is allowed. [Daily comparison NAV](examples/comparison_nav.csv) reproduces the chart.

The benchmark uses published 000832.CSI levels from Tushare `index_daily`, with no simulated trading cost. The official [methodology](https://pdf.dfcfw.com/pdf/H2_AN202502251643475838_1.pdf) uses market-value weights and full prices. Portfolios include gross announced coupon cash flows and trading costs; the benchmark uses published levels without synthetic coupon adjustments or fees. Differences are not pure selection alpha. Results apply the stated risk controls retrospectively and are not a live trading record.

### Market premium at parity 100

![Market conversion premium at parity 100](examples/premium_at_100.png)

### Coupon accounting and announcement refresh

Issuer payment PDFs from CNInfo supply announcement, record, ex and payment dates, gross interest per bond and source links. Record-date closing quantities lock entitlement; ex-date receivables become cash at payment-date close, available for subsequent trades. This avoids double counting in full-price record-date marks. Ex-date sellers retain entitlement and new buyers do not receive it. Final interest already included in redemption cannot be counted twice. Investor-specific taxes are excluded.

Every run paginates coupon, redemption and trading-termination notices, then searches missing annual coupon periods across all categories by issuer. Full Tushare `cb_call` snapshots and Eastmoney supplement redemption data. Risk availability follows announcement dates. Raw queries and PDF text remain cached in SQLite. Missing held-period coupon evidence, ambiguous amounts/dates, incomplete pagination or unresolved valuations fail the run and block publication; unfamiliar document formats require a parser correction and rerun.

[Coupon events](examples/coupons.csv) · [Payment summary](examples/coupon_summary.csv) · [Held-period coverage](examples/coupon_coverage.csv) · [Announcement query coverage](examples/announcement_coverage.csv)

## Data and implementation

Tushare supplies historical quotations, valuation fields, balance announcements, calendars, name changes and suspensions. AKShare provides independent checks and documented quotation supplements. Delisted bonds retain their historical observations. Straight-bond values from different providers are not silently combined.

The repository includes [dated risk events](risk_events.csv), [verified redemptions](verified_redemptions.csv), and [three OHLCV repairs](quote_overrides.csv) with provenance and adjacent-day volume-unit checks. Repairs do not invent missing valuation fields or overwrite conflicting prices. [examples/](examples/) contains public aggregates and figures. Raw responses, signals, trades and holdings remain in the ignored local SQLite database and `results/` directory.

## Updates and reproduction

Use Python 3.12+, install [requirements.txt](requirements.txt), and set `TUSHARE_TOKEN` locally. Access to `cb_call`, `cb_basic`, `cb_daily`, `cb_share`, `trade_cal`, `namechange`, `suspend_d` and `index_daily` is required.

```powershell
python -m pip install -r requirements.txt
python strategy.py run --start 20190101 --end 20261120
python strategy.py research --start 20190101 --end 20261120 --offline
python strategy.py self-test
```

Offline reproduction requires a successful online run with matching parameters. Use `--db`, `--out`, `--cost-bps`, `--fraction` and `--check-code` to specify the database, output directory, friction, selection fraction and cross-check bond. Paths default to the script directory. `--reuse-repo` optionally reads another repository's redemption snapshot and checks daily coverage.

The 25 built-in tests cover coupon entitlement, ex-date accounting, delayed payment, redemption overlap, notice corrections, the benchmark, weekly cutoffs and empty quote caching. Full runs additionally validate held coupon periods, NAV and fee ledgers.

### Weekly automation

[GitHub Actions](.github/workflows/weekly-update.yml) triggers at **19:01 Asia/Shanghai (11:01 UTC), Monday–Friday**. Scheduled runs check the exchange calendar for the most recent elapsed weekday slot, even when the queue crosses midnight. Updates run only on the week's final trading session, including holiday-shortened weeks. Fully closed weeks are skipped. The former local 18:01 task is disabled.

The repository secret `TUSHARE_TOKEN` authenticates requests. The workflow restores the SQLite cache, refreshes prices, index data, coupons and risk announcements, runs research and checks, then publishes figures, metrics and both README result tables. Failures preserve the previous complete publication. Manual dispatch supports an explicit cutoff and forced rebuild. Credentials are never committed. Missing calendar or cutoff data fails validation, without assuming Friday or forward filling.

## Limitations

Remaining limitations concern execution realism, investor-specific taxes and historical data revisions. Gross regular coupons and automatic announcement refresh are included, with missing-evidence publication guards. Daily-open fills allow fractional quantities without lot-size, capacity or queue modeling. Suspension marks may understate volatility. Provider revisions prevent an original-release-vintage guarantee. Retrospectively specified risk rules require future-sample evaluation; results are not expected returns or guaranteed fills.

## References

- [Northeast Securities — Convertible Bond Research Framework](https://github.com/ericxuzhesheng/Convertible-Bond-Pricing-Research/blob/main/report/Northeast_Securities_Convertible_Bond_Research_Framework_Theory_to_Strategy_20240823.pdf)
- [Tushare daily CB data](https://tushare.pro/document/2?doc_id=187) · [Bond master](https://tushare.pro/document/2?doc_id=185) · [Conversion and balance disclosures](https://tushare.pro/document/2?doc_id=247) · [Name changes](https://tushare.pro/document/2?doc_id=100)
- [AKShare bond data](https://akshare.akfamily.xyz/data/bond/bond.html)
