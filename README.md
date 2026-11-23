# 展商咨询名单的授权交付与撤回回收

工作人员在本地演练的**登记门户**（`/`）与**展商收件门户**（`/receiver/A`、`/receiver/B`）之间，
把观众留下的出游意向按授权逐家交给参展旅行社，并在授权变化后回收。项目通过 Chromium
实际操作页面，不使用门户背后的任何隐藏接口。所有姓名、联系方式、证件和机构均为演练数据。

来源服务的边界见 [来源契约](docs/source-contract.md)；本项目的操作与状态约定见
[业务交付说明](docs/handoff.md)。

## 快速开始

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install --with-deps chromium

# 终端一：启动演练来源（仅回环地址，重启后回到阶段 0）
python3 -m source.app --port 8765
```

```bash
# 终端二：一轮完整演练——读取登记、分别交付 A/B、核对授权、提交回收、汇总风险
python3 -m handoff.cli --state handoff-state/state.json run
```

也可以分步执行，便于观察每一步：

```bash
python3 -m handoff.cli sync                                  # 读取全部咨询卡与授权抽屉
python3 -m handoff.cli prepare --vendor A                    # 按当前授权组装交付
python3 -m handoff.cli send --vendor A                       # 上传并读取逐行回执
python3 -m handoff.cli status --vendor A                     # 哪些已交付、拒收、待收回
python3 -m handoff.cli trace --id L-101                      # 单条咨询的完整经历
python3 -m handoff.cli risk                                  # 权限变化后尚未确认停用的范围
```

查看页面操作时加 `--headed`。默认状态在 `handoff-state/`，已被 `.gitignore` 忽略。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `sync [--id ID] [--overwrite-evidence]` | 逐张打开咨询卡并展开授权抽屉；登记新版本或发现冲突 |
| `prepare --vendor A\|B [--id ID]` | 只组装，不上传；行内只含该展商获准的字段 |
| `send [--vendor] [--reference REF] [--lose-response]` | 上传；先用同一引用查历史页，已入账则直接采纳 |
| `resume` | 继续所有未完成交付，重查确认页，只补投未定论的行 |
| `retry --reference REF` | 为被拒收的行开**新的**交付引用，不修改旧引用 |
| `verify [--id ID]` | 重新核对授权，为被缩小的字段登记停用申请 |
| `stops [--vendor] [--submit]` | `--submit` 提交停用申请；不带参数刷新回执（区分 pending / confirmed） |
| `status [--vendor]` | 按展商查看交付、逐行状态、回执与停用申请 |
| `trace --id ID` | 单条咨询跨授权版本与收件回执的经历 |
| `risk` | 已交付但当前授权不再覆盖的字段，以及停用是否已确认 |
| `evidence --digest D --reason R --output PATH` | 受限读取授权原文，写 0600 文件并记审计 |
| `run` | 同步 → 分别交付 → 核对 → 回收 → 风险汇总的一轮工作 |

全局参数：`--base`（默认 `http://127.0.0.1:8765`）、`--state`、`--evidence`、`--journal`、
`--headed`。也可用环境变量 `HANDOFF_BASE`、`HANDOFF_STATE`、`HANDOFF_EVIDENCE_DIR`、
`HANDOFF_JOURNAL` 设置。

## 关键行为

- **按展商拆行**：每次交付只包含目标展商在该授权版本下获准的渠道。未获准渠道的键不会
  出现在上传名单、状态文件或普通日志里，原值也不会。
- **不靠姓名或电邮合并**：咨询号是唯一身份。共用同一电邮的家庭成员、同一观众的不同行程
  始终是不同行。
- **引用不可变**：同一 `reference` 重复提交返回首次记录；被拒收的行用 `retry` 开新引用，
  已接收行不会因剩余行失败而重复投递。
- **确认页丢失可恢复**：提交后先用同一引用读历史页；即使返回 503 也能找到已登记结果。
- **停用是独立事实**：`pending` 只表示对方收到请求，`confirmed` 才是对方已停用。
  `verify` 只对**实际接收过该字段的展商**发起回收，不会动另一家仍获授权的咨询。

## 数据边界

- 状态文件：`handoff-state/state.json`，只存咨询号、访客号、行程、授权版本、获准**字段名**、
  回执号和停用状态。
- 受限证据：`handoff-state/restricted/`（目录 0700、文件 0600），以内容摘要命名保存授权
  抽屉原文，单独保管；读取记入 `evidence-access.log`。可用 `--evidence` 指向受限卷。
- 普通日志：`handoff-state/handoff.log`，只有标识、字段名和状态，不含任何联系方式原值。
- 业务查询（`status`/`trace`/`risk`）只用版本、字段名和摘要解释判断，不展示被屏蔽的信息。

## 检查

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q source tests handoff
```

`tests/test_source.py` 检查来源自身；`tests/test_handoff.py` 启动 Chromium，通过真实页面
走完登记、部分接收、拒收重试、确认页丢失、授权缩小、停用待确认/已确认等过程，并核对
业务文件与日志不含原值。

## 演练阶段

来源支持三个演练阶段（检查人员通过 `POST /__control` 切换，请求头
`X-Operator: local-operator`）：阶段 0 初始授权且 L-102 被拒收；阶段 1 缩小 L-101 对 A 的
许可、撤掉 L-103 对 B 的许可，L-102 可接受；阶段 2 收件方确认停用。来源重启会清空远端
状态，业务状态在 `handoff-state/` 中保留，可接着 `resume`。
