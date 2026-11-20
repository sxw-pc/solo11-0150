# 农贸货批行情采证台

批发市场信息员离线登记摊位报价、货批检验、双边成交和按品种等级发布的行情。仓内仅含虚构摊位与货批，无外部站点、账号或密钥。

## 快速运行

Python 3.11+，在仓库根目录执行：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m marketproof demo --db demo.sqlite
python -m pytest -q
python -m unittest discover -s tests -v
```

四账独立入口：

```powershell
python -m marketproof quote-import --db state.sqlite --input fixtures/quote_valid.json
python -m marketproof grade-import --db state.sqlite --input fixtures/grade_valid.json
python -m marketproof trade-import --db state.sqlite --input fixtures/trade_valid.json
python -m marketproof index-publish --db state.sqlite --input fixtures/index_valid.json
python -m marketproof party-import --db state.sqlite --input fixtures/parties_chain.json
python -m marketproof handover-import --db state.sqlite --input fixtures/handover_chain.json
python -m marketproof report --db state.sqlite
```

`*-conflict.json`、`grade_late.json`、`trade_overdraw.json`、`index_sparse.json` 是改变业务结论的反例。全部来源保存在 `source` 和 `source_conflict`，含原始 JSON、SHA-256、业务时间和接收时间。同来源同内容不重复处理；异内容留冲突原文，不覆盖旧版。单次导入使用 SQLite 事务，货批占额与交易状态同事务重算。

## 离线交接、拆分与回流

`handover-import` 采录摊位间转手：每张双边回执带摘要、货批、数量、价格、交接时刻、**一张**前手回执与受让方。一次拆分由多张引用同一前手的回执表达，各笔合计不得超出该持有人当时余额；多前手而未逐份标明去向的回执保持 `source_pending`，不替它猜一条来源。`party-import` 按带生效期与签认摘要的主体登记采录经营者、受益所有人、摊位与采购商，身份按交接日有效登记核对，摊位名称不用于推断独立性。

交接链按交接时刻确定性重建，因此作废和迟到更正沿链传播而非依赖录入顺序：

| 证据（`voids[].kind`） | 对托管的作用 |
| --- | --- |
| `physical_void` | 证明实物未交接，上游余额恢复，失去来源的下游成交转 `custody_dispute` |
| `refund` / `contract_cancel` | 仅款项或合同变化，货物不回流，原成交保持有效 |
| `correction` | 迟到双边更正数量，原件保留，链上按更正后数量重算 |

作废须两名签署见证与摘要，单方声明记为 `unwitnessed` 且不改变任何结论。旧 `trade-import` 的 `cancel` 保持原“单次直销未交付释放”语义；转手回执只能来自交接来源并声明 `physical_handover` 证据。

## 两份行情产品与法律交割账

同一批成交同时进入三条互不替代的产品线（`index_release.product_line`）：

| 产品线 | 样本 | 验收示例 |
| --- | --- | --- |
| `legal` | 全部 `confirmed` 法律交割 | 四笔全部确认，独立摊位 3、区间 100–200 |
| `turnover` | 每次真实转售，仅取最终进入外部采购链的数量；回流部分为 0 | 甲@100 与乙@160 两摊，中位价 130 |
| `procurement` | 只取卖给最终独立采购商的最后一笔 | 仅乙@160，一摊样本不足 |

有正量合格份额的成交才进入样本；同摊位多笔先取价格中位数代表价，再对独立摊位取中位数与最小/最大值并使用原有门槛。行情排除**不**回写成交无效：`legal` 账仍保留全部真实交割，排除只记录在 `daily_sample`/`index_release`。上游报价、等级或交接链变化时，旧发布回执标 `withdrawn` 并生成新版本，两份行情各自重算、互不替代。

`demo` 一次性跑通旧单次直销与十公斤转手链，输出 `legal_vs_price_products`，展示同一输入下法律交割与两份行情的不同决定。

## 独立业务决定

| 子系统 | 持久化业务实体 | 独立结论 |
| --- | --- | --- |
| 摊位报价账 | `stall_licence`, `quote_revision`, `quote_decision`（见通用 `decision`） | 许可范围和同版异价是否允许报价进入成交合同 |
| 货批等级账 | `lot_origin`, `inspection_result`, `grade_decision` | 产地一致、抽检等级与可售量是否可信 |
| 主体登记账 | `party_register` | 交接日有效的签认身份、受益所有人与独立性 |
| 真实成交流水 | `trade_receipt`, `lot_balance`, `trade_decision` | 双边回执、价格合同、逐持有人托管余额是否支持确认成交 |
| 交接证据账 | `custody_position`, `transfer_void` | 拆分/回流后的持有人余额、实物作废与迟到更正 |
| 公开行情版本 | `daily_sample`, `index_release`, `publication_receipt` | 独立摊位数是否足以发布，以及分等级、分产品线的价格区间和撤回历史 |

货批原始数量仍只由 `grade_decision.saleable` 决定；`lot_balance.occupied` 只统计直接出库的旧语义占额，转手不再二次占额。行情采用每个摊位的交易价格中位数，再对不同摊位求最小、最大与中位值。同摊位多单不会增加独立摊位数。旧发布回执遇上游变化后标为 `withdrawn`，并生成新版；已结算历史交易遇到迟到复检保留争议状态和货批占额。

三条跨账路径在测试中实际执行：同版异价冻结引用交易并使严格三摊位门槛失效；迟到复检将未结高等级成交转争议并重算分级区间；高价成交撤销释放货批余量、使最大价从 200 降为 150，旧回执保留。

## 数据边界

本版只使用本地合成 JSON。跨市场运输、冷链归责、节假日基线、多实验室仲裁、期货交割、零售传导、召回触达与替代指数属于后续独立方向，当前未实现。
