# ForgePilot

企业级 Coding Agent 平台：面向「查 Bug → 修 Bug → 出可评审 PR」这条链路，在通用编码 Agent 之上补齐三样东西——企业上下文（跨仓代码、TAPD 工单）、固化的构建/测试/返修流程、可解释的代码评审。

架构设计见 `ForgePilot文档.md`，实现方案见 `docs/实现方案.md` 与 `~/.claude/plans/`。**这三份和 `CLAUDE.md` 一样不进仓库**——它们是工作区里的私有说明与记录（`.gitignore` 里写了原因），克隆下来得到的是代码、`docs/notes/` 与本文件。

## 架构定位

**自研 Harness，以 DeepSeek Harness (DSH) 为架构参照**，不依赖 DSH 代码。插件内核复用现成的 Cordis Python 移植 `cordis-port`，并把它隔离在 `src/forgepilot/kernel/` 之内——`kernel/` 是全仓库唯一允许 import `cordis_port` 的包。

## 开发

```bash
# 安装（含开发依赖）
.venv/Scripts/python.exe -m pip install -e ".[dev]"

# 全部门禁：ruff + 测试 + 覆盖率门（CI 跑的是同一个文件，两份命令清单会漂移）
.venv/Scripts/python.exe tools/check.py

# M0：内核一致性验证（地基验收，见文末）
.venv/Scripts/python.exe -m pytest tests/kernel/ -v

# 快速自检
.venv/Scripts/python.exe -m forgepilot --kernel-check
```

三种模式、`--serve`、端到端跑一张工单、以及评测的入口，都在工作区文档 `CLAUDE.md` 的 **Commands** 里——那里是唯一的清单（该文件不进仓库，见 `.gitignore`）。

## 当前进度

**M0–M11 与 P1 已完成**，此后每一批 P 条目（安全边界的收口、一票一仓、启停与报告的可读性、评测任务集、远端索引的假 provider）也都各自带着验收测试落地了。

⚠️ **这里刻意不列清单，因为两份清单一定会漂移——这一份已经漂过**：它长期写着「业务代码尚未开始」，而在那期间仓库已经拥有三种模式、一个真实 repo host、一套评测任务集，和一条能开出真实 PR 的链路。**读一句话会读到过期的事实；跑一条命令不会。**

```bash
.venv/Scripts/python.exe -m forgepilot --config-dir configs --boot   # 每一行插件是否都能加载
.venv/Scripts/python.exe tools/check.py                              # 门禁：ruff + 测试 + 覆盖率门
.venv/Scripts/python.exe evals/run.py --scripted                     # 评测任务的无网络自证
.venv/Scripts/python.exe evals/report.py --latest                    # 上一次评测到底发生了什么
```

里程碑、每个 P 条目的验收标准、以及每一处**刻意留下的缺口**，权威清单在 `CLAUDE.md` 的 **State of the work**——本文件只指向它，不复制它。

**注意**：`tests/kernel/test_kernel_contract.py` 是全案的地基验收。内核语义一旦漂移，先停下来重新评估内核选型，不要为了迁就内核去改测试。
