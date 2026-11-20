# 保税研发材料的双计量耗用仓

仓库包含合成业务资料、通用资料检查器（sourcepack）和耗用仓实现（consume）。字段、单位和业务记录的含义见 docs/资料口径.md，完整性目录为 data/catalog.json，耗用仓的分层与口径实现见 docs/耗用仓设计.md。不得把文件自检成功解释成业务结论成立。

## 环境

Python 3.11.2；仓库依赖已在 requirements.txt 固定版本（耗用仓本身只用标准库）。

    python3 -m pip install -r requirements.txt
    python3 -m sourcepack

检查入口读取资料目录，核对文件摘要、字段类型、记录键、引用与行数，不进行估计、推荐、批准或结算。新增业务资料可使用相同字段契约，目录摘要只对应当前保存的输入文件。

## 已有检查与语法构建

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack consume tests

测试覆盖资料容器、输入诊断和耗用仓业务规则（双计量换算、知识截点、待核、交接版本与差异分类、三方核对）。

## 耗用仓使用方式

所有命令经 `python3 -m consume`，`--root` 指向含 data/ 的仓库根（缺省当前目录），业务状态写在 warehouse/ 下。时刻须带时区；接收时的 `--at` 是该批资料的知识时刻。

    # 1. 接收基础资料（跳过后续接收类的密度补正）
    python3 -m consume receive-dir --dir data --at 2026-10-27T18:00:00+08:00 --skip density_corrections
    # 2. 在知识截点上构建可读仓表（warehouse/mart/）
    python3 -m consume build --cutoff 2026-10-27T23:59:59+08:00
    # 3. 生成并确认一期交接（内容相同不新增版本）
    python3 -m consume handover --period 2026-10 --cutoff 2026-10-27T23:59:59+08:00 --confirm --show
    # 4. 后续接收密度补正，重建并另行比较
    python3 -m consume receive --file data/density_corrections.csv --table density_corrections --at 2026-10-28T08:00:00+08:00
    python3 -m consume handover --period 2026-10 --cutoff 2026-10-31T23:59:59+08:00 --confirm
    python3 -m consume diff --period 2026-10 --from v1 --to v2
    # 5. 待核队列、版本回看、三方核对
    python3 -m consume pending --cutoff 2026-10-31T23:59:59+08:00
    python3 -m consume versions --period 2026-10
    python3 -m consume check --cutoff 2026-10-31T23:59:59+08:00

仓表（warehouse/mart/）：container_ledger（包装明细，可追到来源行）、holder_balance（在手余额）、project_cost_lines / project_cost_bills（项目耗用与账单）、writeoff_lines（核销交接行）、pending_queue（待核去向）、recon.json（三方核对）。交接版本在 warehouse/handover/<期间>/ 下，确认后可回看，后来的修订用 diff 比较，差异按新增实物动作 / 历史资料补正分类到项目与期间。examples/batch2/ 是第二批实验数据的示例，用于演示新增实物动作的接收与比较。
