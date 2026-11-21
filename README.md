# 农贸货批行情采证台

批发市场信息员离线登记摊位报价、货批检验、双边成交和按品种等级发布的行情。仓内仅含虚构摊位与货批，无外部站点、账号或密钥。

## 快速运行

Python 3.11+，在仓库根目录执行：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m marketproof demo --db demo.sqlite
python -m unittest discover -s tests -v
```

四账独立入口：

```powershell
python -m marketproof quote-import --db state.sqlite --input fixtures/quote_valid.json
python -m marketproof grade-import --db state.sqlite --input fixtures/grade_valid.json
python -m marketproof trade-import --db state.sqlite --input fixtures/trade_valid.json
python -m marketproof index-publish --db state.sqlite --input fixtures/index_valid.json
python -m marketproof report --db state.sqlite
```

`*-conflict.json`、`grade_late.json`、`trade_overdraw.json`、`index_sparse.json` 是改变业务结论的反例。全部来源保存在 `source` 和 `source_conflict`，含原始 JSON、SHA-256、业务时间和接收时间。同来源同内容不重复处理；异内容留冲突原文，不覆盖旧版。单次导入使用 SQLite 事务，货批占额与交易状态同事务重算。

## 冷链归责采证台

`cold-import` 保留记录器短表原件并只按报警总数给出 `clear` / `alarm_unattributed`，接口与旧版完全一致。报警未归责后，操作员可基于真实可取得的两套现场开两种采录工单，原件先入 `source` 再核证：

```powershell
python -m marketproof cold-anchor-import --db state.sqlite --input fixtures/cold_anchor_minus_one.json
python -m marketproof cold-seal-import   --db state.sqlite --input fixtures/cold_seal_rear.json
python -m marketproof cold-reset-import  --db state.sqlite --input fixtures/cold_reset.json
python -m marketproof cold-attribute     --db state.sqlite --input '{"journey":"cool-J1"}'
python -m marketproof cold-recommend     --db state.sqlite --input recommend.json
```

- **共用偏移**：一段设备运行内本地时钟到统一时钟只有一个未知整数偏移；双对时锚同时比对本地与统一时间，必须用同一个相容偏移重新解释全段热区，不能逐热区挑选有利偏移。责任门槛为同一连续热区内单方达到 3 分钟，分离热区不合计。
- **双锚工单**（`cold_anchor`）：只有在全部合法解释下同一责任方都达门槛才签发凭据；热区 `[4,8)`、交接 6、偏移界 `[-1,1]` 下，-1 恰签首方、+1 恰签后方、0 双方不足。
- **温敏封条工单**（`cold_seal`）：封条在各自时间范围内独立证明连续报警，校验身份与货品一致，不恢复记录器偏移；后方封条证明 `[6,9)` 且前方封条已证低于门槛时签后方，记录器总量不变，档案不填入 ±1。
- **矛盾见证**：两套原件世界交集为空（或指向不同责任方）时停在 `evidence_conflict` 并给出矛盾见证，不按来源优先级写死赢家。
- **复位签证**（`cold_reset_visa`）：到来后运行拆为两代，各自持有独立偏移；旧整段凭据保留并标 `superseded`，不假装原来未签，且后一代不得借用前一代的对时窗口。
- **预算建议**（`cold-recommend`）：按现场可取得的见证集合（`anchor_region` / `seal_range`）与解释覆盖给建议，不机械选最近的原件。

## 独立业务决定

| 子系统 | 持久化业务实体 | 独立结论 |
| --- | --- | --- |
| 摊位报价账 | `stall_licence`, `quote_revision`, `quote_decision`（见通用 `decision`） | 许可范围和同版异价是否允许报价进入成交合同 |
| 货批等级账 | `lot_origin`, `inspection_result`, `grade_decision` | 产地一致、抽检等级与可售量是否可信 |
| 真实成交流水 | `trade_receipt`, `lot_balance`, `trade_decision` | 双边回执、价格合同与余量是否支持确认成交 |
| 公开行情版本 | `daily_sample`, `index_release`, `publication_receipt` | 独立摊位数是否足以发布，以及分等级价格区间和撤回历史 |

行情采用每个摊位的交易价格中位数，再对不同摊位求最小、最大与中位值。同摊位多单不会增加独立摊位数。当前日报只采信 `confirmed` 成交，旧发布回执遇上游变化后标为 `withdrawn`，并生成新版；已结算历史交易遇到迟到复检保留争议状态和货批占额。

三条跨账路径在测试中实际执行：同版异价冻结引用交易并使严格三摊位门槛失效；迟到复检将未结高等级成交转争议并重算分级区间；高价成交撤销释放货批余量、使最大价从 200 降为 150，旧回执保留。

## 数据边界

本版只使用本地合成 JSON。节假日基线、多实验室仲裁、期货交割、零售传导、召回触达与替代指数属于后续独立方向，当前未实现。冷链归责的原件采录、双锚/封条核证与复位分代见上节。
