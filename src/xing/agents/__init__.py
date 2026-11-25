"""Agent 层。

按依赖顺序：

    pricing    模型价格 + 预留上界（纯计算，无 IO）
    provider   一次模型调用的抽象（stub / openai）
    schemas    三个输出契约（Pydantic）+ 供 stub 合成响应的形状描述
    runtime    step 循环、预算预留、调用录制、结构化输出（事务边界都在这里）
    upstream   读上游任务的 output（只取指针）
    planner    Orchestrator：goal → 三个任务
    researcher 调研 → external_events
    writer     写作 → contents + content_versions
    reviewer   审核 → approvals(PENDING)

不预建更多文件。工具注册表、MemoryReader、Producer 都会出现，但它们出现时
各自要有明确的输入输出契约；现在铺空壳只会让「哪里是真正的边界」变模糊。

这里**不导入**任何具体 Agent：handler 的注册由 ``worker.handlers`` 在末尾触发，
依赖方向保持单向（worker → agents），不会在包初始化时形成互相导入。
"""
