"""来源分类与检索分层的纯函数测试。

这两件事都很便宜，但错得很安静：分类错 → coverage 的 source_mix 失真；
分层没生效 → 讨论层其实没查，而轨迹上看不出「没查」和「查了但空」的区别。
所以它们各自要有确定的、不依赖数据库的用例。
"""

from __future__ import annotations

from xing.agents.expander import _plan_retrievals
from xing.agents.schemas import OpinionQuery
from xing.search.base import SearchDoc
from xing.search.sources import classify, domain_of, registrable
from xing.services.trace_service import _doc


def test_classify_by_registrable_domain() -> None:
    """子域要归到主域上：``m.36kr.com`` 和 ``36kr.com`` 是同一家。"""
    assert classify("https://m.36kr.com/p/123") == ("MEDIA", "domain:36kr.com")
    assert classify("https://zhuanlan.zhihu.com/p/1") == ("UGC", "domain:zhihu.com")
    assert classify("https://www.kingandwood.com/cn/insights/1") == (
        "VESTED",
        "domain:kingandwood.com",
    )


def test_classify_official_by_suffix() -> None:
    """政府与高校不逐个列域名，靠 TLD 兜住。"""
    assert classify("https://www.court.gov.cn/zixun/1") == ("OFFICIAL", "suffix:.gov.cn")
    assert classify("https://www.tsinghua.edu.cn/info/1") == ("OFFICIAL", "suffix:.edu.cn")


def test_classify_unknown_is_honest() -> None:
    """认不出来就写 UNKNOWN，不硬塞进某一类。

    CSDN 这种既有个人博客又有厂商软文的站点，判成任何一类都是猜；
    「未分类」本身就是需要被看见的信号。
    """
    assert classify("https://blog.csdn.net/u/1") == ("UNKNOWN", "unclassified")
    assert classify("https://example.com/x") == ("UNKNOWN", "unclassified")
    assert classify(None) == ("UNKNOWN", "no-url")


def test_classify_falls_back_to_label_without_url() -> None:
    """模型端检索只给媒体名。标签能认出来就认，认不出仍然是 UNKNOWN。"""
    assert classify(None, "36氪") == ("MEDIA", "label:36氪")
    assert classify(None, "36kr.com") == ("MEDIA", "label:36kr.com")
    assert classify(None, "某地日报") == ("UNKNOWN", "no-url")


def test_registrable_handles_two_level_tld() -> None:
    assert registrable("m.36kr.com") == "36kr.com"
    assert registrable("news.gov.cn") == "news.gov.cn"
    assert registrable("a.b.example.co.uk") == "example.co.uk"
    assert domain_of("https://www.36kr.com:8443/p/1?x=1") == "36kr.com"


def test_plan_retrievals_adds_discussion_layer() -> None:
    """媒体层照模型给的查，讨论层用固定域名 + 时间窗补两条。

    讨论层不是「多搜几个词」，而是「换一批站点」—— 所以这里断言的关键是
    ``include_domains``，而不是 query 的措辞。
    """
    items = [
        OpinionQuery(query="AI 周报 形式主义", target="OPPOSE"),
        OpinionQuery(query="AI 周报 一线员工", target="VOICE"),
    ]
    plan = _plan_retrievals(items, "公司要求用 AI 写周报")

    assert [r.layer for r in plan] == ["MEDIA", "MEDIA", "DISCUSSION", "DISCUSSION"]
    assert [r.text for r in plan[:2]] == [i.query for i in items]
    assert all(not r.options.include_domains for r in plan[:2])

    for item in plan[2:]:
        assert item.options.include_domains, "讨论层必须限定站点，否则还是媒体"
        assert "zhihu.com" in item.options.include_domains
        assert item.options.time_range == "year"
        assert item.text.startswith("公司要求用 AI 写周报")


def test_plan_retrievals_without_core_keeps_media_only() -> None:
    """种子压不出核心词时不要拼出「 怎么看」这种 query。"""
    plan = _plan_retrievals([OpinionQuery(query="某事件 争议", target="DISPUTE")], "")
    assert [r.layer for r in plan] == ["MEDIA"]


def test_doc_identity_ignores_scheme_and_fragment() -> None:
    """同一篇材料被两条 query 命中时，去重靠的是 URL，不是标题。"""
    a = SearchDoc(title="t", snippet="s", url="https://example.com/a")
    b = SearchDoc(title="t2", snippet="s2", url="https://example.com/a")
    assert a.identity == b.identity


def test_classify_country_and_pattern_variants() -> None:
    """国别域名与律所域名：真实跑里前者的 UNKNOWN 占了三分之二。"""
    assert classify("https://www.mckinsey.com.cn/insights") == ("VESTED", "domain:mckinsey.com")
    assert classify("https://finance.sina.cn/tech/1") == ("MEDIA", "domain:sina.cn")
    assert classify("https://cn.weforum.org/stories/1") == (
        "OFFICIAL",
        "domain:weforum.org",
    )
    assert classify("https://nsd.pku.edu.cn/x") == ("OFFICIAL", "suffix:.edu.cn")
    assert classify("https://www.wsic.ac.cn/x") == ("OFFICIAL", "suffix:.ac.cn")
    assert classify("https://huichenglawyer.com/x") == ("VESTED", "pattern:lawyer")


def test_trace_doc_keeps_snapshot_and_reports_current_kind() -> None:
    """旧轨迹上的分类不能改写，但要能看出「按今天的规则会怎么判」。

    改写的后果是取证记录失去意义；只留旧值的后果是读的人以为分类器
    认不出律所，去补一条已经补过的规则 —— 两种都是错的，所以要两个答案。
    """
    stale = _doc(
        {
            "source_kind": "UNKNOWN",
            "kind_rule": "unclassified",
            "url": "https://huichenglawyer.com/x",
            "kept": True,
        },
        1,
    )
    assert stale.source_kind == "UNKNOWN", "当时判成什么就是什么"
    assert stale.source_kind_now == "VESTED", "但今天的规则会判成利益相关方"
    assert stale.kind_rule_now == "pattern:lawyer", "还要指出是哪条规则生效了"

    same = _doc(
        {"source_kind": "MEDIA", "url": "https://www.nytimes.com/a", "kept": True},
        1,
    )
    assert same.source_kind_now is None, "一致时不要制造噪音"
    assert same.kind_rule_now is None


def test_trace_doc_without_url_is_not_reclassified() -> None:
    """没有 URL 的材料（模型端检索）当时靠标签判定。

    现在拿不到那个标签，重算只会把它降成 UNKNOWN —— 那是凭空造出来的
    不一致，不是规则变化，所以宁可不重算。
    """
    doc = _doc({"source_kind": "MEDIA", "kind_rule": "label:36氪", "kept": True}, 1)
    assert doc.source_kind == "MEDIA"
    assert doc.source_kind_now is None
