# ForgePilot

企业级 Coding Agent 平台：面向「查 Bug → 修 Bug → 出可评审 PR」这条链路，在通用编码 Agent 之上补齐三样东西——企业上下文（跨仓代码、TAPD 工单）、固化的构建/测试/返修流程、可解释的代码评审。

架构设计见 `ForgePilot文档.md`；实现方案见 `CLAUDE.md` 与 plans 目录。

## 架构定位

**自研 Harness，以 DeepSeek Harness (DSH) 为架构参照**，不依赖 DSH 代码。插件内核复用现成的 Cordis Python 移植 `cordis-port`，并把它隔离在 `src/forgepilot/kernel/` 之内——`kernel/` 是全仓库唯一允许 import `cordis_port` 的包。

## 开发

```bash
# 安装（含开发依赖）
.venv/Scripts/python.exe -m pip install -e ".[dev]"

# M0：内核一致性验证
.venv/Scripts/python.exe -m pytest tests/kernel/ -v

# 快速自检
.venv/Scripts/python.exe -m forgepilot --kernel-check
```

## 当前进度

M0（内核验证）已完成。业务代码尚未开始，里程碑 M1–M11 见实现方案。

**注意**：`tests/kernel/test_kernel_contract.py` 是全案的地基验收。内核语义一旦漂移，先停下来重新评估内核选型，不要为了迁就内核去改测试。
