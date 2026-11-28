"""来源分类：一条材料是「谁在说话」。

为什么必须分：展开的 ``camp_counts`` 现在把一篇律所文章和一条员工抱怨算成等权
的一票。真实数据里 19 个域名只有 2 个是社媒，其余是媒体、专栏、律所、云厂商
—— 于是 ``GROUNDED`` 实际只证明了「有文章这么说过」，没证明「这是舆论」。

分类只做一件事：按域名把材料归到五个来源类型之一，**并如实说明命中了哪条规则**。
规则是数据，不是判断 —— 判不准就 ``UNKNOWN``，不猜（和 ``_label`` 取不到就返回
None 是同一个原则）。改这张表就能改口径，不需要动链路。

    MEDIA     媒体/资讯/专栏：有人在编辑，但不是在表达自己的处境
    UGC       用户生成内容：一个个具体的人在说话，带互动量
    OFFICIAL  政府、法院、统计、高校等机构口径
    VESTED    利益相关方：律所、厂商、咨询、公关稿 —— 立场与生意绑定
    UNKNOWN   认不出，或能认出但不属于上面任何一类（如内容农场）
"""

from __future__ import annotations

SOURCE_KINDS = ("MEDIA", "UGC", "OFFICIAL", "VESTED", "UNKNOWN")

#: 讨论层只在这些站点里检索。它们共同的属性是「发言单位是人，不是文章」。
#: 交给 Tavily 的 ``include_domains``，所以必须是裸域名（不带协议、不带路径）。
DISCUSSION_DOMAINS = (
    "weibo.com",
    "zhihu.com",
    "xiaohongshu.com",
    "douban.com",
    "tieba.baidu.com",
    "v2ex.com",
    "bilibili.com",
    "hupu.com",
    "x.com",
    "reddit.com",
)

#: 精确域名规则。键是「可注册域名」（``m.36kr.com`` → ``36kr.com``）。
_DOMAINS: dict[str, str] = {
    # ---- 媒体 ----------------------------------------------------
    "36kr.com": "MEDIA",
    "jiemian.com": "MEDIA",
    "21jingji.com": "MEDIA",
    "yicai.com": "MEDIA",
    "caixin.com": "MEDIA",
    "huxiu.com": "MEDIA",
    "tmtpost.com": "MEDIA",
    "thepaper.cn": "MEDIA",
    "infzm.com": "MEDIA",
    "lifeweek.com.cn": "MEDIA",
    "stcn.com": "MEDIA",
    "cls.cn": "MEDIA",
    "wallstreetcn.com": "MEDIA",
    "sohu.com": "MEDIA",
    "sina.com.cn": "MEDIA",
    "163.com": "MEDIA",
    "qq.com": "MEDIA",
    "ifeng.com": "MEDIA",
    "ithome.com": "MEDIA",
    "cctv.com": "MEDIA",
    "xinhuanet.com": "MEDIA",
    "people.com.cn": "MEDIA",
    "chinanews.com.cn": "MEDIA",
    "ftchinese.com": "MEDIA",
    "fortunechina.com": "MEDIA",
    "sina.cn": "MEDIA",
    "guancha.cn": "MEDIA",
    "bjnews.com.cn": "MEDIA",
    "ynet.com": "MEDIA",
    "pingwest.com": "MEDIA",
    "geekpark.net": "MEDIA",
    "qdaily.com": "MEDIA",
    "hbrchina.org": "MEDIA",
    "chinanews.com": "MEDIA",
    "jkb.com.cn": "MEDIA",
    "reuters.com": "MEDIA",
    "bloomberg.com": "MEDIA",
    "nytimes.com": "MEDIA",
    "wsj.com": "MEDIA",
    "cnbc.com": "MEDIA",
    "bbc.com": "MEDIA",
    "apnews.com": "MEDIA",
    "techcrunch.com": "MEDIA",
    "theverge.com": "MEDIA",
    "wired.com": "MEDIA",
    "fortune.com": "MEDIA",
    "economist.com": "MEDIA",
    # ---- UGC ------------------------------------------------------
    "weibo.com": "UGC",
    "weibo.cn": "UGC",
    "zhihu.com": "UGC",
    "xiaohongshu.com": "UGC",
    "douban.com": "UGC",
    "tieba.baidu.com": "UGC",
    "v2ex.com": "UGC",
    "bilibili.com": "UGC",
    "douyin.com": "UGC",
    "kuaishou.com": "UGC",
    "hupu.com": "UGC",
    "juejin.cn": "UGC",
    "segmentfault.com": "UGC",
    "stackoverflow.com": "UGC",
    "github.com": "UGC",
    "medium.com": "UGC",
    "substack.com": "UGC",
    "x.com": "UGC",
    "twitter.com": "UGC",
    "reddit.com": "UGC",
    "producthunt.com": "UGC",
    "1point3acres.com": "UGC",
    "freecodecamp.org": "UGC",
    # ---- 国际组织与多边机构 ---------------------------------------
    # 它们发的是机构口径（报告、决议、统计），不是记者的报道。
    "un.org": "OFFICIAL",
    "imf.org": "OFFICIAL",
    "worldbank.org": "OFFICIAL",
    "weforum.org": "OFFICIAL",
    "oecd.org": "OFFICIAL",
    "who.int": "OFFICIAL",
    # ---- 利益相关方 -----------------------------------------------
    # 律所：写的是客户的法律风险，不是公众怎么看。
    "law.asia": "VESTED",
    "legal500.com": "VESTED",
    "chambers.com": "VESTED",
    "kingandwood.com": "VESTED",
    "zhonglun.com": "VESTED",
    "junhe.com": "VESTED",
    "fangdalaw.com": "VESTED",
    "haiwenlaw.com": "VESTED",
    # 厂商：自家产品的可行性报告。
    "aliyun.com": "VESTED",
    "huaweicloud.com": "VESTED",
    "tencent.com": "VESTED",
    "aws.amazon.com": "VESTED",
    "cloud.google.com": "VESTED",
    "azure.microsoft.com": "VESTED",
    "openai.com": "VESTED",
    "anthropic.com": "VESTED",
    # 咨询与调研：卖的是方法论与报告。
    "mckinsey.com": "VESTED",
    "bcg.com": "VESTED",
    "bain.com": "VESTED",
    "deloitte.com": "VESTED",
    "pwc.com": "VESTED",
    "ey.com": "VESTED",
    "kpmg.com": "VESTED",
    "accenture.com": "VESTED",
    "gartner.com": "VESTED",
    "idc.com": "VESTED",
    "forrester.com": "VESTED",
    # 通稿渠道：付费发布的稿子。
    "prnewswire.com": "VESTED",
    "businesswire.com": "VESTED",
    "globenewswire.com": "VESTED",
}

#: 按后缀判定。政府与法院不逐个列域名，靠 TLD 兜住绝大多数。
_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".gov.cn", "OFFICIAL"),
    (".gov", "OFFICIAL"),
    (".mil", "OFFICIAL"),
    (".edu.cn", "OFFICIAL"),
    (".edu", "OFFICIAL"),
    (".ac.cn", "OFFICIAL"),
    (".ac.uk", "OFFICIAL"),
)

#: 域名里出现这些词基本可以确定是律所/法律服务机构。不追求完备 ——
#: 认不出就是 UNKNOWN，比猜错更安全。
_VESTED_PATTERNS = ("lawyer", "lawfirm", "attorney", "lawfirm")

#: 中文站名 → 类型。模型端检索（qwen）只给「媒体名」，不给 URL，
#: 而它给的名字通常是中文 —— 没有这张表，那一半材料会全掉进 UNKNOWN。
_LABEL_ALIASES: dict[str, str] = {
    "36氪": "MEDIA",
    "界面新闻": "MEDIA",
    "财新": "MEDIA",
    "虎嗅": "MEDIA",
    "澎湃": "MEDIA",
    "第一财经": "MEDIA",
    "经济观察": "MEDIA",
    "南方周末": "MEDIA",
    "新华社": "MEDIA",
    "人民日报": "MEDIA",
    "央视": "MEDIA",
    "路透": "MEDIA",
    "彭博": "MEDIA",
    "知乎": "UGC",
    "微博": "UGC",
    "小红书": "UGC",
    "豆瓣": "UGC",
    "贴吧": "UGC",
    "抖音": "UGC",
    "快手": "UGC",
    "哔哩哔哩": "UGC",
    "B站": "UGC",
    "脉脉": "UGC",
    "V2EX": "UGC",
}

#: 需要按三级后缀取「可注册域名」的 TLD。
_TWO_LEVEL = (
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "ac.cn",
    "co.uk",
    "co.jp",
    "com.hk",
    "com.tw",
    "com.au",
)


def domain_of(url: str | None) -> str | None:
    """取主机名，去掉 ``www.`` 与端口。取不到就返回 None，不编。"""
    if not url:
        return None
    host = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    host = host.split("@")[-1].split(":")[0].strip().lower()
    host = host.removeprefix("www.")
    return host or None


def registrable(host: str) -> str:
    """``m.36kr.com`` → ``36kr.com``；``news.gov.cn`` → ``news.gov.cn``。"""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _country_variants(base: str) -> list[str]:
    """同一主体的国别写法：``mckinsey.com.cn`` → ``mckinsey.com``。"""
    out: list[str] = []
    if base.endswith(".com.cn"):
        out.append(base[: -len(".com.cn")] + ".com")
    elif base.endswith(".cn"):
        out.append(base[: -len(".cn")])
    if base.endswith(".co.uk"):
        out.append(base[: -len(".co.uk")] + ".com")
    return [item for item in out if "." in item]


def classify(url: str | None, source_label: str | None = None) -> tuple[str, str]:
    """返回 ``(kind, rule)``。``rule`` 是命中的规则，写进轨迹供人核对与调整。

    没有 URL 时（模型端检索只给媒体名）退化到标签匹配：标签里出现已知站点的
    主域名首段就算命中，规则记成 ``label:xxx``。命中不了就是 UNKNOWN ——
    「认不出来」本身就是要给人看的信息，不是需要抹平的缺陷。
    """
    host = domain_of(url)
    if host:
        base = registrable(host)
        kind = _DOMAINS.get(base)
        if kind:
            return kind, f"domain:{base}"
        # 同一家站的国别域名：mckinsey.com.cn 与 mckinsey.com 是一个主体。
        for alt in _country_variants(base):
            kind = _DOMAINS.get(alt)
            if kind:
                return kind, f"domain:{alt}"
        for token in _VESTED_PATTERNS:
            if token in host:
                return "VESTED", f"pattern:{token}"
        for suffix, suffix_kind in _SUFFIXES:
            if host.endswith(suffix):
                return suffix_kind, f"suffix:{suffix}"
        return "UNKNOWN", "unclassified"

    if source_label:
        label = source_label.lower()
        for alias, alias_kind in _LABEL_ALIASES.items():
            if alias.lower() in label:
                return alias_kind, f"label:{alias}"
        for base, kind in _DOMAINS.items():
            if base.split(".")[0] in label:
                return kind, f"label:{base}"
    return "UNKNOWN", "no-url"


def kinds_of(urls: list[str | None]) -> dict[str, int]:
    """一批材料的来源分布，按出现次数降序。"""
    counts: dict[str, int] = {}
    for url in urls:
        kind, _ = classify(url)
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


__all__ = [
    "DISCUSSION_DOMAINS",
    "SOURCE_KINDS",
    "classify",
    "domain_of",
    "kinds_of",
    "registrable",
]
