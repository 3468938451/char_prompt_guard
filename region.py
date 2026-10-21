"""分区覆盖（**实验性功能**，默认关闭）：指定「头/头发/眼睛 用 A 的、身子/体型 用 B 的」。

⚠️ **实验性功能：打开后生图效果可能不理想。** 分区拼接只是"按词重画"，模型可能把两个
角色的特征混在一起（串味）、比例异常或细节崩坏；默认关闭，普通使用完全不受影响。

需求原话：「可以指定**头/头发/眼睛**用 A 的、**身子/体型**用 B 的这类分区拼接；
也允许单分区（只换头）」，入口语言形如
``头是流萤的，身子是爱莉的`` / ``头换成流萤`` / ``用爱莉的身子配流萤的头``；
**识别不到就不做任何事**。

本模块只做三件**纯字符串 / 纯内存**的事 —— **不调模型、不读上文、不写记忆库、
不改别的插件配置**：

1. **解析**（:func:`parse_region_overrides`）：识别「**分区词** + **角色名** +
   **归属句式**」的**三重条件**（缺一不认），得到
   ``{head: 流萤, body: 爱莉希雅}``（键是分区、值是**角色表里的正式名** ——
   用户写别名「爱莉」也归一到「爱莉希雅」）；
2. **拼装**（:func:`build_region_swap`）：按 ``[region_override] regions`` 给出的
   顺序（默认 ``head → body → outfit → pose → background``）取各分区的特征词，
   拼成一段正向说明（**head 在最先**），并算出**负向**词：
   被换掉的那个角色在**同一个分区**里的原特征（``negative_swap``）与防串味词
   （``anti_mix``）；
3. **降级**：``[region_override] entries`` 里找不到该角色、或条目里没填这个分区时
   **退回「不分区」**，由调用方打一条 DEBUG（其它注入照旧，内容不动）。

三重条件的实现口径（见 :data:`_REGION_PATTERNS`）::

    分区词  是   角色     的     头是流萤的 / 头是流萤
    分区词  换成 角色           头换成流萤 / 头换成了流萤
    分区词  用   角色     的     头用流萤的
    用     角色   的    分区词   用爱莉的身子
    （配/搭配/配上/和/跟/与/加）角色 的  分区词
                                 配流萤的头 / 和爱莉的身子

三条都**必须在同一处紧邻出现**才算命中 —— 所以「流萤的头饰很好看」（分区词后面是
「饰」不是归属句式）、「头疼」（没有角色也没有归属句式）、「画流萤」（没有分区词）
都**不会**触发。``用 … 的 <分区词>`` 这一族还要过一道**尾边界**：分区词后面那一个字
必须是标点 / 空白 / 串尾 / emoji、句末语气词，或"前后分区"用的连接字
（``配`` / ``搭`` / ``和`` / ``跟`` / ``与`` / ``加``），**实词汉字不放行** ——
「用流萤的头饰」不会把「头」拆出来（与 :mod:`outfit` 的归属句式同一套思路）。
**单字**分区词（``头`` / ``脸``）另有一道**前置边界**：它前面那个字必须是串首 / 标点 /
空白 / 助词（``的`` / ``了`` / ``是`` / ``把`` / ``将`` …），否则视为复合词的一部分 ——
「馒头是流萤的」「石头是爱莉的」「回头是爱莉的」都不命中（见
:func:`_region_head_ok`）。

本模块**不 import handler**（避免循环依赖），只依赖 :mod:`character` 的角色表工具。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .character import CharacterEntry, find_roster_identity, normalize_prompt_text

# ── 分区（固定五个，配置里的 regions 只决定"允许哪些 / 什么顺序"）───────────

REGION_HEAD = "head"
REGION_BODY = "body"
REGION_OUTFIT = "outfit"
REGION_POSE = "pose"
REGION_BACKGROUND = "background"

#: 五个分区的**默认顺序**，也就是注入顺序（``head`` 在最先）。
DEFAULT_REGIONS: tuple[str, ...] = (
    REGION_HEAD,
    REGION_BODY,
    REGION_OUTFIT,
    REGION_POSE,
    REGION_BACKGROUND,
)

#: 日志与提示词里的中文分区名（用户核对"哪个分区用了谁"全靠它）。
REGION_LABELS: dict[str, str] = {
    REGION_HEAD: "头",
    REGION_BODY: "身子",
    REGION_OUTFIT: "衣服",
    REGION_POSE: "姿势",
    REGION_BACKGROUND: "背景",
}

#: 每个分区认哪些中文说法（**长的排前面**：``头发`` 必须先于 ``头`` 匹配，
#: 否则「用爱莉的头发」会被拆成「头」+ 落单的「发」）。
REGION_WORDS: dict[str, tuple[str, ...]] = {
    REGION_HEAD: ("头发", "发型", "头部", "脑袋", "眼睛", "眼眸", "双眼", "五官", "脸", "头"),
    REGION_BODY: ("身子", "身体", "体型", "身材", "躯干", "体格"),
    REGION_OUTFIT: ("衣服", "服装", "穿着", "打扮", "装扮", "皮肤", "时装", "穿搭"),
    REGION_POSE: ("姿势", "动作", "姿态", "站姿", "坐姿"),
    REGION_BACKGROUND: ("背景", "场景"),
}

#: 条目列表里每个分区对应的字段名（与 :data:`DEFAULT_REGIONS` 一致）。
REGION_FIELDS: tuple[str, ...] = DEFAULT_REGIONS

#: ``anti_mix`` 追加的防串味负向词（多角色分区拼接时）。
ANTI_MIX_TERMS: tuple[str, ...] = ("mixed attributes", "blended eyes")

#: ``stylized_swap`` 追加的「接缝 / 过渡」类描述词（正向，默认不出现）。
STYLIZED_SWAP_TERMS: tuple[str, ...] = (
    "stitched neck",
    "visible seam",
    "head slightly tilted",
)

#: natural 模式追加段的开头（幂等标记：同一张图第二次触发时不再重复追加）。
REGION_NATURAL_MARKER = "（分区覆盖："
#: natural 模式的整段模板（``{parts}`` 由 :data:`REGION_PART_TEMPLATE` 拼出来）。
REGION_NATURAL_TEMPLATE = "（分区覆盖：{parts}）"
#: natural 模式里单个分区的一段：``头=流萤：silver hair, long hair``。
REGION_PART_TEMPLATE = "{label}={name}：{keywords}"
#: 分区之间的分隔符（中文分号，与既有说明段的风格一致）。
REGION_PART_SEPARATOR = "；"
#: ``stylized_swap`` 那一段的标签（不算分区，只描述"接缝 / 过渡"）。
REGION_STYLIZED_LABEL = "接缝过渡"

# ── 归属句式的词法 ────────────────────────────────────────────────────

#: 角色候选里不允许出现的标点（与 :mod:`character` / :mod:`outfit` 同一批字符）。
_REGION_PUNCT = "，。！？；：、,.!?;:…～~·「」『』《》‘’“”\"'（）()[]【】{}<>—－-/|\\"

#: 角色候选的字符类：**不含标点、空白与「的」** —— 「的」是归属句式的分隔符，
#: 把它排除掉之后 ``头是流萤的身子`` 里的角色就只会是「流萤」。
_REGION_NAME_CLASS = "[^" + re.escape(_REGION_PUNCT + "的") + r"\s]"

#: 句末语气词（与 :mod:`outfit` 的 ``_OUTFIT_TAIL_PARTICLES`` 同一批）：
#: 它们跟在分区词后面时**不影响**归属（「配流萤的头吧」照旧命中）。
_REGION_TAIL_PARTICLES = "吧吗呢啊呀哦嘛呗啦咯喔"

#: ``用 … 的 <分区词>`` 那一族允许的尾字符：标点 / 空白 + 句末语气词 +
#: "前后分区"的连接字（``用爱莉的身子配流萤的头`` 里「身子」后面是「配」）。
_REGION_TAIL_ALLOW_CHARS = frozenset(
    "，,。.、！!？?～~；;：: \t\n" + _REGION_TAIL_PARTICLES + "配搭和跟与加"
)

#: **单字**分区词（``头`` / ``脸``）前面允许跟的字：这些是"把 / 将 / 让 / 的"这类
#: 介词、助词与常见动词，其余汉字一律视为**复合词的一部分**（``馒头`` / ``石头`` /
#: ``回头`` 里的「头」不是分区词）。多字分区词（``头发`` / ``身子`` / ``眼睛`` …）
#: 不做这个前置检查 —— 它们极少作为别的词的结尾出现。
_REGION_HEAD_ALLOW_CHARS = frozenset(
    "的了是把将让给用换要画在跟和与加配搭" + _REGION_TAIL_PARTICLES
)

_WORD_TO_REGION: dict[str, str] = {
    word: region for region, words in REGION_WORDS.items() for word in words
}

#: 分区词的正则交替（**最长的排最前面**）。
_REGION_ALTERNATION = "|".join(
    re.escape(word)
    for word in sorted(_WORD_TO_REGION, key=len, reverse=True)
)

#: 五条归属句式。每条都同时带 ``word``（分区词）与 ``role``（角色名）两个命名分组，
#: **三重条件**因此由正则本身保证：分区词、角色名、归属动词缺一个都匹配不上。
_REGION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 头是流萤的 / 头是流萤（「是…的」）
    re.compile(rf"(?P<word>{_REGION_ALTERNATION})是(?P<role>{_REGION_NAME_CLASS}{{1,12}})(?:的)?"),
    # 头换成流萤 / 头换成了流萤（「换成」）
    re.compile(rf"(?P<word>{_REGION_ALTERNATION})换成(?:了)?(?P<role>{_REGION_NAME_CLASS}{{1,12}})"),
    # 头用流萤的（「用…的」，分区词在前）
    re.compile(rf"(?P<word>{_REGION_ALTERNATION})用(?P<role>{_REGION_NAME_CLASS}{{1,12}})的"),
    # 用爱莉的身子（「用…的」，分区词在后 → 要过尾边界）
    re.compile(rf"用(?P<role>{_REGION_NAME_CLASS}{{1,12}})的(?P<word>{_REGION_ALTERNATION})"),
    # 配流萤的头 / 搭配流萤的头 / 和爱莉的身子（前后分区拼接时的连接词）
    re.compile(
        rf"(?:搭配|配上|配|和|跟|与|加)(?P<role>{_REGION_NAME_CLASS}{{1,12}})"
        rf"的(?P<word>{_REGION_ALTERNATION})"
    ),
)


def _is_cjk(char: str) -> bool:
    """判断一个字符是不是汉字（尾边界用：非汉字一律放行）。"""
    return "\u4e00" <= char <= "\u9fff"


def _region_head_ok(text: str, index: int, word: str) -> bool:
    """**单字**分区词（``头`` / ``脸``）的前置边界：它不是复合词的结尾。

    ``馒头是流萤的`` / ``石头是爱莉的`` / ``回头是爱莉的`` 里那个「头」不是分区词；
    ``我的头是流萤的`` / ``用爱莉的头`` 里前面是助词「的」，照旧命中。
    多字分区词直接放行（``头发`` / ``身子`` … 极少作为别的词的结尾）。
    """
    if len(word) > 1 or index <= 0:
        return True
    char = text[index - 1]
    if char in _REGION_HEAD_ALLOW_CHARS:
        return True
    return not _is_cjk(char)


def _region_tail_ok(text: str, index: int) -> bool:
    """``…的<分区词>`` 的尾边界：分区词后面那一格允许跟什么。

    - 串尾 → 放行；
    - 标点 / 空白 / 句末语气词 / 连接字（见 :data:`_REGION_TAIL_ALLOW_CHARS`）→ 放行；
    - 非汉字（emoji / 英文 / 数字）→ 放行；
    - **实词汉字 → 不放行**（「用流萤的**头饰**」不会把「头」拆出来）。
    """
    if index >= len(text):
        return True
    char = text[index]
    if char in _REGION_TAIL_ALLOW_CHARS:
        return True
    return not _is_cjk(char)


# ── 条目列表（[region_override] entries）──────────────────────────────


@dataclass(frozen=True)
class RegionEntry:
    """分区覆盖的**一个条目**：某个角色在各分区里的特征词。

    Attributes:
        name: 角色名（**角色表里的名字或别名**；别名由
            :func:`find_region_entry` 归一到同一个角色）
        features: ``分区 → 特征词元组``；没填的分区不出现在这里
    """

    name: str = ""
    features: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def keywords(self, region: str) -> tuple[str, ...]:
        """取某个分区的特征词（没填就是空元组）。"""
        return tuple(self.features.get(region, ()))

    def filled_regions(self) -> tuple[str, ...]:
        """填了内容的分区（按默认顺序）。"""
        return tuple(region for region in REGION_FIELDS if self.features.get(region))


def split_region_keywords(raw_text: str) -> list[str]:
    """把一条「英文关键词」切成列表（中英文逗号都认，去空白、去空项）。"""
    text = (raw_text or "").replace("，", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _entry_text(item: Any, key: str) -> str:
    """读条目里的一个字段：既支持配置节的 SectionBase 实例，也支持普通字典。"""
    value = getattr(item, key, None)
    if value is None and isinstance(item, dict):
        value = item.get(key)
    return str(value if value is not None else "").strip()


def parse_region_entries(raw_entries: Any) -> list[RegionEntry]:
    """把 ``[region_override] entries`` 列表归一成 :class:`RegionEntry`。

    配置里的 ``entries`` 是**独立条目列表**（框架的数组节 ``[[region_override.entries]]``），
    每项形如 ``name = 流萤`` + ``head/body/outfit/pose/background = 英文关键词``。
    这里对"SectionBase 实例 / 字典"两种形状都接受，并且：

    - ``name`` 为空的条目整条忽略；
    - 同名（归一后相同）的条目**只保留第一个**；
    - 各分区的关键词按中英文逗号切开，空项丢弃。

    条目列表为空 / 形状不认识时返回空列表（调用方据此**退回不分区**）。
    """
    if not raw_entries:
        return []
    if isinstance(raw_entries, dict):
        items: list[Any] = [raw_entries]
    elif isinstance(raw_entries, (list, tuple)):
        items = list(raw_entries)
    else:
        items = [raw_entries]

    entries: list[RegionEntry] = []
    seen: set[str] = set()
    for item in items:
        name = _entry_text(item, "name")
        if not name:
            continue
        key = normalize_prompt_text(name)
        if key in seen:
            continue
        seen.add(key)
        features: dict[str, tuple[str, ...]] = {}
        for region in REGION_FIELDS:
            keywords = split_region_keywords(_entry_text(item, region))
            if keywords:
                features[region] = tuple(keywords)
        entries.append(RegionEntry(name=name, features=features))
    return entries


def find_region_entry(
    name: str,
    entries: list[RegionEntry],
    roster: list[CharacterEntry] | None = None,
) -> RegionEntry | None:
    """按「角色名 → 角色表别名」找条目；找不到返回 ``None``。

    两轮比对：

    1. **条目名与候选名相同**（归一后比较，忽略大小写 / 空白 / 反斜杠）；
    2. 候选名是**角色表里的别名**时，把条目名也当角色表身份解析一次 ——
       条目写 ``elysia``、用户说「爱莉」也能对上同一个角色。
    """
    target = normalize_prompt_text(name)
    if not target:
        return None
    for entry in entries:
        if normalize_prompt_text(entry.name) == target:
            return entry
    if not roster:
        return None
    wanted = find_roster_identity(name, roster)
    if wanted is None:
        return None
    wanted_key = normalize_prompt_text(wanted.name)
    for entry in entries:
        hit = find_roster_identity(entry.name, roster)
        if hit is not None and normalize_prompt_text(hit.name) == wanted_key:
            return entry
    return None


# ── 解析（三重条件）──────────────────────────────────────────────────


def parse_regions(raw: Any) -> tuple[str, ...]:
    """解析 ``[region_override] regions``（允许参与拼接的分区及其顺序）。

    接受字符串（``"head,body,outfit"``，中英文逗号 / 分号 / 空白都认）或列表；
    只收五个合法分区名，非法值静默丢弃，重复的只留第一次出现的位置。
    **整份都非法 / 为空时退回 :data:`DEFAULT_REGIONS`**（保证开箱可用）。
    """
    if isinstance(raw, str):
        parts: list[str] = re.split(r"[,，;；\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        parts = [str(part) for part in raw]
    else:
        parts = []
    regions: list[str] = []
    for part in parts:
        key = str(part).strip().lower()
        if key in DEFAULT_REGIONS and key not in regions:
            regions.append(key)
    return tuple(regions) or DEFAULT_REGIONS


def _resolve_role(
    role: str,
    roster: list[CharacterEntry],
    entries: list[RegionEntry],
) -> str:
    """把归属句式里的角色候选解析成**正式名**；解析不出返回空串。

    只认**精确同一性**（角色表的名字 / 别名 / 标签，或条目列表里的名字），
    绝不做"最像的那个"这种猜测 —— 猜错就会把分区贴到别的角色身上。
    """
    raw = (role or "").strip()
    if not raw or raw in _WORD_TO_REGION:
        return ""
    hit = find_roster_identity(raw, roster)
    if hit is not None:
        return hit.name
    entry = find_region_entry(raw, entries, roster)
    if entry is not None:
        return entry.name
    return ""


def parse_region_overrides(
    text: str,
    roster: list[CharacterEntry],
    entries: list[RegionEntry] | None = None,
) -> dict[str, str]:
    """从一句话里解析「哪个分区用谁」—— 返回 ``{分区: 正式角色名}``。

    **三重条件**（角色名 + 分区词 + 归属句式）由 :data:`_REGION_PATTERNS` 保证，
    三条缺一不认；解析不出角色的匹配**直接丢弃**（绝不留一个"某人的头"）。

    其它口径：

    - 同一个分区被指定给两个不同角色（``头是流萤的，头是爱莉的``）→ 该分区视为
      **有歧义**、整条丢弃（"宁少勿多"，绝不随机挑一个）；
    - 返回值按**原文出现顺序**排列，值一律是角色表里的**正式名**
      （``爱莉`` → ``爱莉希雅``）；
    - ``用 … 的 <分区词>`` 那一族要过尾边界（见 :func:`_region_tail_ok`）；
    - 认不到任何东西时返回**空字典**，调用方据此**什么都不做**。
    """
    if not text:
        return {}
    table = list(entries or ())

    matches: list[tuple[int, str, str]] = []
    for pattern in _REGION_PATTERNS:
        for match in pattern.finditer(text):
            word = match.group("word") or ""
            region = _WORD_TO_REGION.get(word)
            role = (match.group("role") or "").strip()
            if not region or not role:
                continue
            # 单字分区词（头 / 脸）的前置边界：排除「馒头 / 石头 / 回头」这类复合词
            if not _region_head_ok(text, match.start("word"), word):
                continue
            # 分区词在匹配末尾时（``用 A 的 <分区词>`` / ``配 A 的 <分区词>``）才查尾边界；
            # 分区词在开头时它后面紧跟的必须就是归属动词，边界天然成立。
            if match.end("word") == match.end() and not _region_tail_ok(
                text, match.end("word")
            ):
                continue
            matches.append((match.start(), region, role))

    assignments: dict[str, str] = {}
    ambiguous: set[str] = set()
    for _position, region, role in sorted(matches, key=lambda item: item[0]):
        if region in ambiguous:
            continue
        name = _resolve_role(role, roster, table)
        if not name:
            continue
        if region in assignments:
            if assignments[region] != name:
                ambiguous.add(region)
                assignments.pop(region, None)
            continue
        assignments[region] = name
    return assignments


# ── 拼装（正向 / 负向）────────────────────────────────────────────────


@dataclass(frozen=True)
class RegionPart:
    """正向拼装里的一段：某个分区的特征词来自哪个角色。

    Attributes:
        region: 分区名（:data:`DEFAULT_REGIONS` 之一）
        character: 这个分区用谁的特征词（角色表里的正式名）
        keywords: 特征词（英文，按条目里的顺序）
    """

    region: str
    character: str
    keywords: tuple[str, ...] = ()

    def label(self) -> str:
        """中文分区名（日志与提示词里的「头 / 身子 / 衣服 …」）。"""
        return REGION_LABELS.get(self.region, self.region)


@dataclass(frozen=True)
class RegionSwap:
    """一次分区覆盖的完整结果（正向段 + 负向词 + 被跳过的信息）。

    Attributes:
        parts: 正向各段（**已按 regions 顺序排好，head 在最前**）
        negatives: 负向词（被换掉那个角色在同一分区的原特征 + 防串味词）；
            去重、且**不会**与任何正向特征词重复
        stylized: ``stylized_swap`` 的"接缝 / 过渡"类描述词（默认空）
        skipped: 被跳过的分区 / 角色及原因（进 DEBUG，供核对"为什么没拼"）
        source: 这句话来自哪条用户消息（日志用）
    """

    parts: tuple[RegionPart, ...] = ()
    negatives: tuple[str, ...] = ()
    stylized: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    source: str = ""

    @property
    def ok(self) -> bool:
        """是否有可注入的正向段（空 = 退回"不分区"）。"""
        return bool(self.parts)

    def positive_keywords(self) -> list[str]:
        """tags 模式要追加的关键词（按分区顺序，head 在最前，去重）。"""
        keywords = [keyword for part in self.parts for keyword in part.keywords]
        keywords.extend(self.stylized)
        result: list[str] = []
        seen: set[str] = set()
        for keyword in keywords:
            key = normalize_prompt_text(keyword)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(keyword)
        return result

    def natural_clause(self) -> str:
        """natural 模式的追加段。

        形如::

            （分区覆盖：头=流萤：silver hair, long hair, blue eyes, headband；
              身子=爱莉希雅：slender, flat chest, pale skin）

        ``stylized_swap`` 打开时在末尾多一段 ``接缝过渡：stitched neck, …``。
        """
        segments = [
            REGION_PART_TEMPLATE.format(
                label=part.label(),
                name=part.character,
                keywords=", ".join(part.keywords),
            )
            for part in self.parts
        ]
        if self.stylized:
            segments.append(
                f"{REGION_STYLIZED_LABEL}：{', '.join(self.stylized)}"
            )
        return REGION_NATURAL_TEMPLATE.format(
            parts=REGION_PART_SEPARATOR.join(segments)
        )

    def summary(self) -> str:
        """一行式摘要（``头=流萤；身子=爱莉希雅``），日志与 DEBUG 共用。"""
        return "；".join(
            f"{part.label()}={part.character}" for part in self.parts
        )

    def log_line(self) -> str:
        """成功那一行日志（含每段特征词、负向词与来源，用户可逐项核对）。"""
        body = "；".join(
            f"{part.label()}={part.character}"
            f"（{', '.join(part.keywords)}）"
            for part in self.parts
        )
        negative = f"；负向={', '.join(self.negatives)}" if self.negatives else ""
        source = f"；来源={self.source[:40]!r}" if self.source else ""
        return f"分区覆盖: {body}{negative}{source}"

    def skip_line(self) -> str:
        """退回「不分区」那一行 DEBUG（其它注入照旧，内容不动）。"""
        reasons = "、".join(self.skipped) or "没有可用的分区条目"
        return f"分区覆盖=跳过（{reasons}）：退回不分区，其它注入照旧"


def _negative_terms(
    parts: tuple[RegionPart, ...],
    assignments: dict[str, str],
    entries: list[RegionEntry],
    roster: list[CharacterEntry] | None,
) -> tuple[str, ...]:
    """算出 ``negative_swap`` 的负向词。

    规则一句话：**某个分区用了 A 的特征词，就把"同一条话里出现的其它角色"在
    **同一个分区**的原特征词放进负向** —— 那些正是被换掉的部分。

    两条硬口径：

    - **与正向重复的词一律不放进负向**（``long hair`` 既是 A 的头、也是 B 的头时，
      放进去等于让同一个词同时被加强和压制）；
    - 顺序稳定（按分区、再按角色出现顺序），去重按归一化文本。
    """
    positive_keys = {
        normalize_prompt_text(keyword)
        for part in parts
        for keyword in part.keywords
    }
    others = list(dict.fromkeys(assignments.values()))
    terms: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for other in others:
            if normalize_prompt_text(other) == normalize_prompt_text(part.character):
                continue
            entry = find_region_entry(other, entries, roster)
            if entry is None:
                continue
            for keyword in entry.keywords(part.region):
                key = normalize_prompt_text(keyword)
                if not key or key in seen or key in positive_keys:
                    continue
                seen.add(key)
                terms.append(keyword)
    return tuple(terms)


def build_region_swap(
    assignments: dict[str, str],
    entries: list[RegionEntry],
    *,
    roster: list[CharacterEntry] | None = None,
    regions: tuple[str, ...] = DEFAULT_REGIONS,
    negative_swap: bool = True,
    anti_mix: bool = True,
    stylized_swap: bool = False,
    source: str = "",
) -> RegionSwap:
    """把 ``{分区: 角色}`` 拼成一次可注入的结果（正向段 + 负向词）。

    正向：只收 ``regions`` 里列出的分区、按 ``regions`` 的顺序排（**head 在最前**）；
    ``entries`` 里没有该角色、或条目里没填这个分区时**跳过该分区**并把原因记进
    ``skipped``（调用方据此打 DEBUG 并退回"不分区"）。

    负向：``negative_swap`` → :func:`_negative_terms`（被换掉的角色在同分区的原特征）；
    ``anti_mix`` 且这条话里出现 **≥2 个不同角色** → 追加
    :data:`ANTI_MIX_TERMS`（``mixed attributes, blended eyes``）。

    正向：``stylized_swap`` → 追加 :data:`STYLIZED_SWAP_TERMS`
    （``stitched neck, visible seam, head slightly tilted``），默认不出现。
    """
    allowed = tuple(regions) or DEFAULT_REGIONS
    skipped: list[str] = []
    ordered: list[str] = []
    for region in assignments:
        if region in allowed:
            ordered.append(region)
        else:
            skipped.append(
                f"{REGION_LABELS.get(region, region)}（该分区未在 regions 里启用）"
            )
    ordered.sort(key=allowed.index)

    parts: list[RegionPart] = []
    for region in ordered:
        character = assignments[region]
        label = REGION_LABELS.get(region, region)
        entry = find_region_entry(character, entries, roster)
        if entry is None:
            skipped.append(f"{character}/{label}（entries 里没有该角色的条目）")
            continue
        keywords = entry.keywords(region)
        if not keywords:
            skipped.append(f"{character}/{label}（条目里没填这个分区）")
            continue
        parts.append(
            RegionPart(region=region, character=character, keywords=keywords)
        )

    if not parts:
        return RegionSwap(skipped=tuple(skipped), source=source)

    negatives: list[str] = []
    if negative_swap:
        negatives.extend(_negative_terms(tuple(parts), assignments, entries, roster))
    if anti_mix and len(set(assignments.values())) >= 2:
        seen = {normalize_prompt_text(term) for term in negatives}
        for term in ANTI_MIX_TERMS:
            key = normalize_prompt_text(term)
            if key and key not in seen:
                seen.add(key)
                negatives.append(term)

    stylized: tuple[str, ...] = STYLIZED_SWAP_TERMS if stylized_swap else ()
    return RegionSwap(
        parts=tuple(parts),
        negatives=tuple(negatives),
        stylized=stylized,
        skipped=tuple(skipped),
        source=source,
    )


__all__ = [
    "ANTI_MIX_TERMS",
    "DEFAULT_REGIONS",
    "REGION_BACKGROUND",
    "REGION_BODY",
    "REGION_FIELDS",
    "REGION_HEAD",
    "REGION_LABELS",
    "REGION_NATURAL_MARKER",
    "REGION_NATURAL_TEMPLATE",
    "REGION_OUTFIT",
    "REGION_PART_SEPARATOR",
    "REGION_PART_TEMPLATE",
    "REGION_POSE",
    "REGION_STYLIZED_LABEL",
    "REGION_WORDS",
    "STYLIZED_SWAP_TERMS",
    "RegionEntry",
    "RegionPart",
    "RegionSwap",
    "build_region_swap",
    "find_region_entry",
    "parse_region_entries",
    "parse_region_overrides",
    "parse_regions",
    "split_region_keywords",
]
