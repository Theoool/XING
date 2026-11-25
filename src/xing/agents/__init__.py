"""Agent 层。

四个文件，按依赖顺序：

    pricing    模型价格 + 预留上界（纯计算，无 IO）
    provider   一次模型调用的抽象（stub / openai）
    runtime    step 循环、预算预留、调用录制（所有事务边界在这里）
    writer     第一个真实 Agent，注册为 task_type='write_content'

不预建更多目录。Planner / Orchestrator / Researcher 等会出现，但当它们出现时
每个都该有明确的输入输出契约；现在铺空壳只会让「哪里是真正的边界」变模糊。

这里**不导入** writer：handler 的注册由 ``worker.handlers`` 在末尾触发，
依赖方向保持单向（worker → agents），不会在包初始化时形成互相导入。
"""
