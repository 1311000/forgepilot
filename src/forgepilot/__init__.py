"""ForgePilot — 企业级 Coding Agent 平台。

分层（自下而上）::

    kernel/          cordis_port 之上的薄封装；唯一允许 import cordis_port 的包
    seams/           接缝「角色 1：接口定义」，只放 Protocol 与值对象
    providers/       接缝「角色 2：实现」，每个 provider 是一个 Cordis 插件
    runtime/         Harness 核心：会话日志、工具管线、主循环、审批、守卫
    orchestration/   任务理解与路由编排（确定性预处理 + 可替换分类器）
    identity/        身份与授权，产出 ExecutionConstraints
    config/          profile / bundle / patch 三层配置
    httpapi/         前端可替换边界

架构参照 DeepSeek Harness (DSH)，但不依赖其代码：DSH 仅作为设计参考，
所有机制都重新表达为 ForgePilot 自己的接口。
"""

__version__ = "0.0.1"

__all__ = ["__version__"]
