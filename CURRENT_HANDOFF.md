# 当前交接（2026-10-01）

目标：美股自动化交易系统，按 `docs/trading-roadmap.md` 分阶段推进；`trading_enabled: false`，未接券商下单。

## 数据基座（Mac：`/Volumes/mysql/duckdb-pilot/us-stock-research`，Parquet）

- 日线 + 分红：596 只（80 ETF、13 宏观/指数/期货、503 只标普成分股），591 只通过质量门禁，5 只隔离（DHR ETN HPQ EXPE KDP，见 `configs/quality_exceptions.yml`）。
- 第二来源核对：Tiingo 复权收盘价，核心 ETF 与被标记的 28 只股票通过。
- 宏观：FRED 14 条序列；基本面：SEC EDGAR 503 家（按 `filed` 时点）；元数据：`parquet/meta/`，目录 `artifacts/catalog.md`。
- 每日增量：`usr-update`（重叠窗口比对，发现复权修订则整段重下）；launchd 定时任务脚本已写，用户尚未安装。
- 分钟线（仅 IEX 单一交易所，成交量不可代表全市场）：
  - Alpaca `intraday_1min_alpaca`：SPY、QQQ 已下，2020-07-27 至 2026-09-28，1550 个交易日，约 60 万根/只，含盘前盘后。
  - Tiingo `intraday_1min`：每次请求最多返回 1 万根（只留最后部分），已改为按月分段 + 截断自动拆分；首次按年下载的 SPY/QQQ 不完整，需 `--restart` 重下。免费额度未核实。
  - 分工：Alpaca 下全部（`scripts/mac_download_intraday.sh alpaca`，`--all-stored`），Tiingo 只下 `configs/universes/intraday_core.yml` 的 15 只核心 ETF。

## 研究

- `etf-trend-baseline`、`vol-target-baseline` 已冻结并回测：回撤优于 SPY，收益不优于 SPY（超额收益置信区间下限不为正）。
- 试验登记 `research/trials.jsonl`（Deflated Sharpe 门槛 0.95）；独立引擎复核误差 ~1e-15。
- 已知缺陷：标普成分股只有当前成员，存在幸存者偏差。

## 下一步

1. 分钟线质量检查与两源核对已写（`usr-audit-intraday`，`quality/intraday.py`）：缺失交易日、节假日/周末出现的 K 线、OHLC 顺序、收盘价对比日线（拆股比例单独计数）、分钟覆盖率、Tiingo/Alpaca 逐分钟比对（含 ±1 分钟标签错位检测）。已在真实数据上跑（2026-09-29，Alpaca 已下 202 只）：201 只通过；FERG 缺 36 个交易日（2021 年在美上市前 IEX 成交稀少）。Alpaca 源有全市场缺失日：2025-03-10（几乎全部）、2021-04-19、2021-10-25、2022-03-08（约 2/3 标的）。分钟覆盖率中位数 0.54（IEX 单一交易所，冷门股大量分钟无成交）。雅虎日线收盘价对分拆做了固定比例回调（BDX CMCSA DD DHR DTE EXC FDX），分钟线为原始成交价，检查已按“连续恒定比例”识别。SPY/QQQ 两源逐分钟一致率 99.97%/100%，中位差 <1 bp，K 线时间标记一致。
2. 研究用正常时段筛选：`quality.intraday.regular_session`。首批 Alpaca 下载截止到 2026-09-29（含当天未收盘的盘前数据），研究时剔除；之后默认截止到前一天。
3. 在用户 Mac 的隔离 Linux 环境里可直接跑检查：用 `/Volumes/mysql/duckdb-pilot/linux-py310`（duckdb/pyarrow，Python 3.10）+ `datetime.UTC` 垫片。该环境无网络、无 pydantic，回测/研究合同类命令仍需在用户终端运行。
4. 数据目录已含分钟线（`usr-catalog`），日线复查 2026-09-29：591/596 通过，隔离 5 只不变。
5. 补充数据（2026-09-29 晚写好，`scripts/mac_data_extras.sh`，用户通宵运行，次日早上检查）：拆股记录 `parquet/splits/`；Fama-French 五因子+动量（日/月）`parquet/factors/`；新增 11 个 FRED 序列与 ^VIX9D ^VIX3M ^VIX6M ^VVIX；标普 500 历史成分（fja05680/sp500，1996 起）`meta/sp500_history`，前成分股日线 `parquet/daily_delisted/`（先雅虎、后 Tiingo，按成分期覆盖率 ≥90% 验收，代码复用的拒收），覆盖表 `meta/sp500_coverage`。试算（未写入）：2000 年以来 1097 个代码/1142 段成分期，现有日线可用 514 段；另有 16 段是代码被新公司复用（旧 DOW、DELL、HLT、Q 等），免费源补不回来；历史名单的当前成分与我们的现成分差 3 只（BLDR TAP TTD，会由前成分股步骤从雅虎补下）。
6. 2026-09-30 检查：Alpaca 583 只全部下完，577 通过；未通过 6 只均已核实：FERG（2021 年美国上市前成交稀少）、HONA（2026-06 新上市，历史太短）、SW（2024-07-08 前的分钟线属于其他证券，已写入 symbol_start）、T（1 根 K 线高低价倒挂）、TLH/TPL（IEX 成交稀少缺天）。全市场异常日写入 `configs/intraday_exceptions.yml`，研究用 `regular_session(rows, exclude, since)` 剔除。拆股 332 只/625 次（含 37 次合股），核对 AAPL/NVDA/TSLA/AMZN/WMT 与事实一致；Fama-French 市场因子与 SPY 日收益相关 0.99；11 个 FRED 新序列、4 个 VIX 序列通过。标普历史成分第 4 步首次运行因列名 `end` 为保留字失败，已修复（列名改为 start_date/end_date，TableStore 建表列名加引号），成分表已写入；前成分股日线待用户重跑 `usr-collect-sp500-history --delisted --execute`。
7. 2026-09-30 新增：因子归因 `usr-attribution`（结果见 docs/results/factor-attribution.md：两策略加入债券/黄金因子后 alpha 不显著）；路线图阶段 1 工具 `usr-order-intent`（只写订单意向文件，未批准研究一律 rehearsal；数据过期/质量失败/回撤 15% 熔断 → 只减仓），待用户在终端做首次演练（需 pydantic，隔离环境跑不了）。
8. 选股研究（2026-09-30）：已预先登记 `research/sp500-momentum-pit`（12-1 动量前 50）与 `research/sp500-lowvol-pit`（低波动前 100），在看任何结果前提交到 Git（d2d8cc1）。引擎 `usr-xsec`（`research/cross_section.py`，无 pydantic，可在用户 Mac 隔离环境直接跑）：按月末时点成分股、次日成交、退市按最后收盘退出（另报 -30% 折价敏感性）、主基准为同池等权。改代码别名表 `configs/ticker_aliases.yml`（24 个，人工核对）。只算覆盖率（未算收益）：2001 年 48% → 2025 年 97%，前成分股下载进行到字母 H；下完后覆盖率达标（≥90%）才运行两项研究。
9. 2026-09-30 下午：前成分股日线质量检查 `usr-audit-delisted`（Tiingo 原始价按拆股系数调整后再查、只查成分期+回看期）；已复核 9 个分拆/特别分配为 accepted，AIV 隔离；下载完成后需再复核新增的（已见 HP 2002-10-01、HSH 2012-06-29）。每日更新脚本扩展为日线+FRED+Alpaca 分钟线+每周拆股/因子+质量门禁+目录+月末订单意向演练；分钟线下载默认截止改为“最近一个已收盘交易日”。定时任务待用户安装（`bash scripts/mac_install_schedule.sh`）。
10. RSP 验证（2026-09-30 15:40，前成分股下到约字母 H 时；`usr-xsec-validate`，只输出等权基准）：覆盖率 ≥90% 的 63 个月（2020-06 至 2025-10）等权组合与 RSP 月度相关 0.9989、跟踪误差 0.72%/年，但引擎每年高 1.3%（2023 年高 3.1 个百分点，疑为 SIVB/FRC/SBNY 等倒闭银行尚未下载）。前成分股下完后复跑，差距应明显缩小；若仍 >0.5%/年需排查。
11. 独立第二实现 `usr-verify-xsec`（`research/verify_xsec.py`，只输出差异）：2026-09-30 在真实数据上两项研究 × 两种退市假设、各 299 个月，最大差异 ≤2e-16，前十持仓逐月一致。前成分股下完后正式运行前再跑一次。
12. SEC 批量数据（2026-09-30）：`usr-collect-sec-bulk`（财报数据集 2009q1–2026q2、内部人交易 2006q1–2026q2）；首次下载 FSDS 有 3 个季度断线（已加重试）。历史代码→CIK `usr-build-cik-map`：用 Form 4 里的（当时代码, CIK, 日期）匹配，CIK 变更处切段，人工复核表 `configs/cik_overrides.yml`；2009 年后成分期对应率 99.9%（仅 VMRK 无数据）。覆盖率检查发现：SEC 2024-12 重处理后封面流通股数缺失（改用稀释加权股数算市值）、收入/净利/权益有替代科目 → 科目扩到 55 个，需 `--what fsds --refresh` 重新解析。
13. 2026-10-01：前成分股下载完成（雅虎 136、Tiingo 157；拒收 163、未找到 156；EMC/APC/BRCM 等大量被收购公司 Tiingo 未返回，待用户用 curl 诊断）。价格覆盖率 2013 年起 ≥90%，2001–2012 为 61%–89%。新增价格身份检查 `usr-build-identity`（`research/identity.py` → `meta/price_identity`）：同一代码早期成分期 CIK 与现今不同、或价格序列晚于成分期开始，均视为无可信价格（仍计入成员、计入覆盖率）；人工表 `configs/identity_reviewed.yml`（RIG 同一公司；CB 2016 年前为老 Chubb）。当前拦截 24 段。别名新增 GPS→GAP、CTL→LUMN、EQR→VMRK（推断）与带日期的 `IR@2020-03-02: TT`。CIK 映射起点改为 2006。定时任务因 macOS 权限（launchd 访问“文稿”）失败，需给 /bin/bash 完全磁盘访问权限。GitHub 上用户另有 CI 自建运行器与 mypy 修正提交，已 rebase 合并。
14. 2026-10-01 正式运行四个时点成分股研究（v1 2001 起、v2 2012 起，v2 在看结果前登记）：均未通过晋级（相对同池等权超额收益置信区间下限不为正）。低波动风险显著更低但收益更低，且 v1 中 2008 年最差 12 月 −31.7% 超过 25% 约束。结果见 docs/results/sp500-pit-cross-section.md；试验登记现有 4 个不同参数组。Tiingo 免费版无法提供被沿用代码的退市公司价格，2001–2011 覆盖率补不上。
15. 质量+价值研究（2026-10-01，预先登记 ad7fe92；同日在算任何收益前修订财报来源为 SEC company facts，ecf8735）：
    拆股记录全量重查并记入 `meta/splits_checked`（`usr-collect-splits --yahoo-delisted`，每周一自动）；
    company facts 下载 966 个 CIK（885 有数据，81 个为 2009 年前已消失的公司）到 `parquet/sec_companyfacts/`
    （`usr-collect-fundamentals --all-ciks`）。财报覆盖率 ≥95%，独立实现逐月一致。结果未通过：相对等权
    −0.04%/月，最差 12 月 −32.1%；归因显示价值 +0.16、盈利 +0.26 暴露显著，但无 alpha。见 docs/results/sp500-quality-value-pit.md。
16. 阶段 1 演练账本 `usr-rehearsal-fill`（2026-10-01）：订单按成交日收盘价模拟记账到 `portfolio/rehearsal/<研究>.yml`，
    下月订单在模拟持仓上生成；每日更新脚本已接入，从 2026-09-30 的清单开始。2026-09-30 清单已独立重算一致（待用户本人复核）。
17. 内部人买入研究（预先登记 5f8782b）：未通过，相对等权 −0.13%/月；见 docs/results/sp500-insider-buying-pit.md。试验登记 6 组。
18. 2026-10-01 用户决定把最差 12 个月亏损上限由 25% 放宽到 50%（追求收益）；旧研究不追溯。
19. SPY 200 日均线 + 2 倍杠杆（预先登记 36ede99，`usr-leveraged-trend`/`usr-verify-lt`）：年化 11.5% vs SPY 8.8%、
    最差 12 月 −34%、DSR 0.976、与 SSO 模型差 0.11%，唯独超额收益区间下限 −0.27%/月不为正 → 未通过。
    SPY 日线已补到 1993 年起，新增 SSO。试验登记 7 组。见 docs/results/spy-trend-leverage-2x.md。
20. 杠杆趋势每日演练 `usr-lt-intent`（SSO/BIL，熔断线 40% 为用户决定）已接入每日更新；首次运行建仓（全现金 10 万）。
21. 样本外检验 1955–2000（预先登记 49c06c1）：未通过——超额区间下限 −0.04%/月、1987 崩盘使最差 12 月 −57.3%。
    数据：^GSPC 1950 起、multpl 月度股息率（`usr-collect-sp-dividend-yield`）、FRED DTB3；全收益构造与 SPY 差 0.07%/年。试验登记 8 组。
22. 趋势 + 波动率目标（预先登记 8f88fa5，1955–2025）：只差主检验；最差 12 月 −40.1%（守住 50%）。试验登记 9 组。
    趋势 + 杠杆一类规则四个版本方向一致、均不显著：超额来自少数几次长熊市，样本数量是根本限制；是否采用由用户判断。
23. 2026-10-03 用户人工批准 sp500_trend_voltarget（仅限无真钱阶段；同时批准单一资产 200% 名义敞口），合同 status: promoted。
    每日更新改为该策略的每日订单（SPY/SSO/BIL，信号用 SPY 复权价，熔断 40%）；2 倍杠杆演练停止。阶段 1 复核从 10 月开始计。
24. 阶段 2 券商：用户 2026-10-03 决定先用 Alpaca 模拟账户。`usr-paper`（只连 paper 地址、独立密钥、对账、去重、收盘竞价单）已写好；
    `configs/paper_broker.yml` enabled=false，阶段 1 通过后由用户打开；定时任务已改为北京 10:05、13:05（雅虎数据到这时才齐，也在 MOC 窗口内）。
25. 监控 `usr-status`（净值历史、artifacts/status.md、与模型偏差）+ Mac 系统通知；阶段门槛 `configs/stage_gates.yml`（2026-10-03 定）。
26. 待办：用户 Mac 上 `tar` 不支持 `--overwrite`（BSD tar），同步包用 `tar xzf 包名` 即可；Mac 仓库需 `git pull` 与 GitHub 同步。
    2026-09-30 的订单意向演练待人工复核；/bin/bash 完全磁盘访问权限（定时任务）；Alpaca 密钥建议重新生成。
