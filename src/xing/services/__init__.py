"""业务服务层。

这一层持有 SQL 与会话，路由只做 HTTP 转换。
没有为每张表建 repository：跨表查询（选题排序、审批流、内容版本）
放进 repository 只会把它变成另一个 service，多一层没有收益。
"""
