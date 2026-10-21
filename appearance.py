"""外观覆盖（**实验性功能**，默认关闭）：把「眼睛 / 发色」换成指定颜色。

⚠️ **实验性功能：默认关闭。** 本模块只做一件很窄的事 —— 用户**明确说**要换眼睛或
发色的颜色时，把那个颜色词**加重**放到提示词最前面，并（可选）把画面里已有的
角色标签**降权**。它解决的是这条实测问题：

    角色标签 ``firefly (honkai: star rail)`` 自带「蓝眼」先验，权重高过描述里
    那句裸词 ``purple eyes``，于是「眼睛换成本尊的紫红」出图仍是蓝眼。

**为什么不塞负向**：``nai_artist`` 的 Action（``share_photo`` / ``share_drawing`` /
``share_visual``）只有 ``content``（photo 另有 ``form``），**没有负向参数**；
它的负向词来自它自己的 ``character.negative_tags`` 配置，而本插件**不碰别的插件
配置**（既不读也不写、更不落盘）。所以「把蓝眼写进负向」这条路在 nai_artist 上
**不存在**，只能靠正向加权 + 角色标签降权。详见 README 的「外观覆盖」一节。

本模块与前一代的 :mod:`region`（分区覆盖）**完全分开**：不同的配置节
（``[appearance]`` vs ``[region_override]``）、不同的解析、不同的注入位置
（外观覆盖**放最前**，分区覆盖追加在最后）。共享的只有
:func:`character.normalize_prompt_text` 这类纯文本工具。

三件事，全是**纯字符串 / 纯内存**（不调模型、不读上文、不写记忆库）：

1. **解析**（:func:`parse_appearance_overrides`）：只认「**外观词** + **更换动词**
   + **颜色词**」的紧邻三连（缺一不认），得到 ``{eyes: "purple-red"}``；
   颜色一律归一到英文修饰词（``purple-red``），供拼装时接 ``eyes`` / ``hair``；
2. **拼装**（:func:`build_appearance_override`）：按 :data:`DEFAULT_APPLY_TO` 的顺序
   生成**加权词**（``1.4::purple-red eyes::``，语法见 :func:`format_weight`）；
3. **降权**（:func:`downweight_character_tag`）：把画面里**第一处**角色标准标签
   改写成降权写法（``0.8::firefly \\(honkai: star rail\\)::``），**只改这一处**，
   其它标签一个字都不动；``tag_weight >= 1.0`` 时整步不执行。

**权重语法为什么要可配**（:func:`parse_weight_syntax`）：

- ``nai``（**默认**）：``1.4::purple-red eyes::`` —— 这是 nai_artist **自己的**
  翻译提示词里唯一认可的数值写法（``prompt_builder.py`` 的 ``<weighting>`` 一节
  明确写「必须严格写成 ``N::tag::``」「不要写成 ``1.5:gentle smile:`` 或其他形式」），
  所以默认用它；
- ``paren``：``((purple-red eyes:1.4))`` —— WebUI / A1111 风格的括号写法。
  nai_artist 的翻译模型被要求**改用** ``N::tag::``，因此这种写法有被它改写或
  丢弃的风险，只作为兼容写法保留；
- ``none``：不加权重，**只把词放最前** —— 与 ``eye_weight = 1.0`` 等价。

**已知边界（如实记录，别指望它 100% 生效）**：``nai_artist`` **不是**把 Action 的
``content`` 原样转发给 NAI API 的 —— 它先把 ``content`` 交给一个**翻译模型**
（``prompt_builder.translate_to_nai_tags``）改写成最终 ``positive_tags``，再原样
发给 NAI。所以「加权词最前」到底能不能保住权重，最终由那个翻译模型决定；
本模块能做的是把意图写得足够明确（加权 + 角色标签降权），而不是保证。

识别不到就**零动作**（不解析、不打日志、不注入）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .character import CharacterEntry, content_contains_tag, normalize_prompt_text

# ── 允许覆盖的两个键（配置里的 apply_to 只决定"允许哪些"）───────────────

APPEARANCE_EYES = "eyes"
APPEARANCE_HAIR = "hair"

#: 默认允许覆盖的键，也是拼装顺序（眼睛 → 发色）。
DEFAULT_APPLY_TO: tuple[str, ...] = (APPEARANCE_EYES, APPEARANCE_HAIR)

#: 日志与提示词里的中文名（用户核对"换了哪儿"全靠它）。
APPEARANCE_LABELS: dict[str, str] = {
    APPEARANCE_EYES: "眼睛",
    APPEARANCE_HAIR: "发色",
}

#: 每个键认哪些中文说法（**长的排前面**：``眼睛颜色`` 必须先于 ``眼睛`` 匹配）。
APPEARANCE_WORDS: dict[str, tuple[str, ...]] = {
    APPEARANCE_EYES: (
        "眼睛颜色",
        "眼睛色",
        "眼瞳色",
        "眼瞳",
        "瞳色",
        "眼眸",
        "眸子",
        "瞳孔",
        "双眼",
        "眼睛",
    ),
    APPEARANCE_HAIR: (
        "头发颜色",
        "头发色",
        "发色",
        "发色系",
        "发型颜色",
        "头发",
        "发型",
    ),
}

#: 更换动词（**长的排前面**：``换成了`` 必须先于 ``换成``）。
CHANGE_VERBS: tuple[str, ...] = (
    "替换成了",
    "替换为",
    "替换成",
    "换成了",
    "换为",
    "换成",
    "改成了",
    "改成",
    "改为",
    "改回",
    "变成了",
    "变成",
    "变为",
    "调成了",
    "调成",
    "染成了",
    "染成",
    "恢复成",
    "恢复为",
)

#: 颜色词表：``中文写法 → (眼睛用的英文修饰词, 发色用的英文修饰词)``。
#: 两个英文修饰词分开是为了 ``金`` —— 眼睛是 ``golden eyes``、头发是 ``blonde hair``。
COLORS: dict[str, tuple[str, str]] = {
    # 紫系
    "紫罗兰": ("violet", "violet"),
    "薰衣草": ("lavender", "lavender"),
    "紫红": ("purple-red", "purple-red"),
    "紫色": ("purple", "purple"),
    "紫": ("purple", "purple"),
    "洋红": ("magenta", "magenta"),
    # 红系
    "绯红": ("crimson", "crimson"),
    "深红": ("crimson", "crimson"),
    "酒红": ("wine red", "wine red"),
    "鲜红": ("red", "red"),
    "血红": ("red", "red"),
    "红色": ("red", "red"),
    "红": ("red", "red"),
    # 粉系
    "粉红": ("pink", "pink"),
    "粉色": ("pink", "pink"),
    "粉": ("pink", "pink"),
    "桃色": ("peach", "peach"),
    "桃": ("peach", "peach"),
    # 橙 / 金 / 黄
    "橙色": ("orange", "orange"),
    "橙": ("orange", "orange"),
    "琥珀": ("amber", "amber"),
    "金黄": ("golden", "blonde"),
    "金色": ("golden", "blonde"),
    "金": ("golden", "blonde"),
    "黄色": ("yellow", "blonde"),
    "黄": ("yellow", "blonde"),
    # 银 / 白 / 黑 / 灰
    "银白": ("silver", "silver"),
    "银色": ("silver", "silver"),
    "银": ("silver", "silver"),
    "白色": ("white", "white"),
    "白": ("white", "white"),
    "黑色": ("black", "black"),
    "黑": ("black", "black"),
    "灰色": ("grey", "grey"),
    "灰": ("grey", "grey"),
    # 棕 / 茶
    "咖啡": ("brown", "brown"),
    "栗色": ("chestnut", "chestnut"),
    "茶色": ("brown", "brown"),
    "棕色": ("brown", "brown"),
    "棕": ("brown", "brown"),
    # 蓝 / 青 / 绿
    "藏青": ("navy blue", "navy blue"),
    "深蓝": ("dark blue", "dark blue"),
    "天蓝": ("sky blue", "sky blue"),
    "水蓝": ("aqua", "aqua"),
    "蓝色": ("blue", "blue"),
    "蓝": ("blue", "blue"),
    "薄荷": ("mint green", "mint green"),
    "青色": ("cyan", "cyan"),
    "青": ("cyan", "cyan"),
    "绿色": ("green", "green"),
    "绿": ("green", "green"),
}

# ── 权重语法（见文件头「权重语法为什么要可配」）─────────────────────────

WEIGHT_SYNTAX_NAI = "nai"
WEIGHT_SYNTAX_PAREN = "paren"
WEIGHT_SYNTAX_NONE = "none"

#: 合法取值，顺序即默认值（第一个是默认）。
WEIGHT_SYNTAXES: tuple[str, ...] = (
    WEIGHT_SYNTAX_NAI,
    WEIGHT_SYNTAX_PAREN,
    WEIGHT_SYNTAX_NONE,
)

#: 默认权重语法：``nai``（``1.4::purple-red eyes::``）。
DEFAULT_WEIGHT_SYNTAX = WEIGHT_SYNTAX_NAI

#: 覆盖词的正向权重默认值（配置项的默认值也取它）。
DEFAULT_OVERRIDE_WEIGHT = 1.4
#: 角色标签降权的默认值（``1.0`` = 不降权）。
DEFAULT_TAG_WEIGHT = 0.8

# ── 归属句式的词法 ────────────────────────────────────────────────────

#: 动词与颜色之间允许出现的「谁的」前缀（「把眼睛换成本尊的紫红」）。
#: 两段可选：代词 / 本尊这类**不是颜色**的词，以及一个 1~6 字的「X的」
#: （「眼睛换成流萤的紫红」里的「流萤的」）。两段都不吃颜色本身 ——
#: 颜色匹配失败时正则自然回溯，最终仍由 :data:`COLORS` 决定。
_POSSESSIVE_PATTERN = (
    r"(?:本尊|你本人|你自己|她自己|他自己|自己|角色|我|你|她|他|它)?"
    r"(?:的|地|得)?"
    r"(?:[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff]{1,6}的)?"
)

#: 外观词与动词之间允许出现的「的颜色」这类填充（「把眼睛的颜色换成紫红」）。
_FILLER_PATTERN = r"(?:的颜色|色彩|颜色|色)?"

#: 颜色交替（**最长的排最前面**，否则「紫红」会被「紫」抢先匹配）。
_COLOR_ALTERNATION = "|".join(
    re.escape(color) for color in sorted(COLORS, key=len, reverse=True)
)

#: 外观词交替（同样长的排前面）。
_WORD_TO_KEY: dict[str, str] = {
    word: key for key, words in APPEARANCE_WORDS.items() for word in words
}
_WORD_ALTERNATION = "|".join(
    re.escape(word) for word in sorted(_WORD_TO_KEY, key=len, reverse=True)
)

#: 动词交替（长的排前面）。
_VERB_ALTERNATION = "|".join(
    re.escape(verb) for verb in sorted(CHANGE_VERBS, key=len, reverse=True)
)

#: **唯一一条**归属句式：``<外观词>[的颜色]<更换动词>[谁的]<颜色>``。
#: 三段紧密相邻 —— 所以「眼睛很好看」「换成别的吧」「紫色的花」都**不会**命中。
_APPEARANCE_PATTERN = re.compile(
    rf"(?P<word>{_WORD_ALTERNATION})"
    rf"{_FILLER_PATTERN}"
    rf"(?P<verb>{_VERB_ALTERNATION})"
    rf"{_POSSESSIVE_PATTERN}"
    rf"(?P<color>{_COLOR_ALTERNATION})"
)


# ── 配置项解析 ────────────────────────────────────────────────────────


def parse_apply_to(raw: object) -> tuple[str, ...]:
    """解析 ``[appearance] apply_to``（允许覆盖哪些键）。

    接受字符串（``"eyes,hair"`` / ``"eyes hair"``，中英文逗号 / 分号 / 空白都认）
    或列表；只收两个合法键，非法值静默丢弃，重复的只留第一次出现的位置。
    **整份都非法 / 为空时退回 :data:`DEFAULT_APPLY_TO`**（保证开箱可用）。
    """
    if isinstance(raw, str):
        parts: list[str] = re.split(r"[,，;；\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        parts = [str(part) for part in raw]
    else:
        parts = []
    keys: list[str] = []
    for part in parts:
        key = str(part).strip().lower()
        if key in DEFAULT_APPLY_TO and key not in keys:
            keys.append(key)
    return tuple(keys) or DEFAULT_APPLY_TO


def parse_weight_syntax(raw: object) -> str:
    """解析 ``[appearance] weight_syntax``；非法值退回 :data:`DEFAULT_WEIGHT_SYNTAX`。"""
    key = str(raw or "").strip().lower()
    return key if key in WEIGHT_SYNTAXES else DEFAULT_WEIGHT_SYNTAX


def parse_weight(raw: object, fallback: float) -> float:
    """把配置里的权重读成 ``float``；读不到 / 非法 / 非正数时用 ``fallback``。"""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not (0.0 < value <= 10.0):
        return fallback
    return value


def _format_number(value: float) -> str:
    """权重的数字写法：``1.4`` / ``0.8`` / ``1``（去掉多余的 0）。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_weight(
    term: str, weight: float, syntax: str, *, kind: str = "positive"
) -> str:
    """按 ``syntax`` 给 ``term`` 加权重。

    - ``kind="positive"``（覆盖词）：``nai`` → ``1.4::word::``；
      ``paren`` → ``((word:1.4))``；``none`` → ``word``；
    - ``kind="downweight"``（角色标签降权）：``nai`` → ``0.8::word::``；
      ``paren`` → ``(word:0.8)``；``none`` → ``word``；
    - ``weight == 1.0`` 时**一律**退化为裸词（「不加权」的唯一含义）。
    """
    if not term:
        return ""
    if syntax == WEIGHT_SYNTAX_NONE or weight == 1.0:
        return term
    number = _format_number(weight)
    if syntax == WEIGHT_SYNTAX_PAREN:
        return (
            f"(({term}:{number}))" if kind == "positive" else f"({term}:{number})"
        )
    return f"{number}::{term}::"


# ── 解析（外观词 + 更换动词 + 颜色 三重条件）───────────────────────────


def parse_appearance_overrides(
    text: str, apply_to: tuple[str, ...] | None = None
) -> dict[str, str]:
    """从一句话里解析「哪个外观换成什么颜色」—— 返回 ``{键: 英文修饰词}``。

    例如::

        眼睛换成本尊的紫红  → {"eyes": "purple-red"}
        发色换成银白        → {"hair": "silver"}
        把瞳孔的颜色改成金  → {"eyes": "golden"}

    **三重条件**（外观词 + 更换动词 + 颜色词）由 :data:`_APPEARANCE_PATTERN`
    保证，三段必须紧邻，缺一不认。其它口径：

    - 只解析 ``apply_to`` 里允许的键（默认 :data:`DEFAULT_APPLY_TO`）；
    - 同一个键被指定成两个不同颜色（``眼睛换成紫，眼睛换成蓝``）→ 该键视为
      **有歧义**、整条丢弃（"宁少勿多"，绝不随机挑一个）；
    - 返回值按**原文出现顺序**排列；
    - 认不到任何东西时返回**空字典**，调用方据此**什么都不做**。
    """
    if not text:
        return {}
    allowed = tuple(apply_to or DEFAULT_APPLY_TO)

    assignments: dict[str, str] = {}
    ambiguous: set[str] = set()
    matches = sorted(
        _APPEARANCE_PATTERN.finditer(text), key=lambda match: match.start()
    )
    for match in matches:
        word = match.group("word") or ""
        key = _WORD_TO_KEY.get(word)
        if not key or key not in allowed or key in ambiguous:
            continue
        color = (match.group("color") or "").strip()
        pair = COLORS.get(color)
        if pair is None:
            continue
        english = pair[0] if key == APPEARANCE_EYES else pair[1]
        if not english:
            continue
        if key in assignments:
            if normalize_prompt_text(assignments[key]) != normalize_prompt_text(
                english
            ):
                ambiguous.add(key)
                assignments.pop(key, None)
            continue
        assignments[key] = english
    return assignments


def find_appearance_colors(
    text: str, apply_to: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """返回这句话里被「外观覆盖」句式命中的**中文颜色词**（去重、保序）。

    与 :func:`parse_appearance_overrides` **同一条正则**，只是取出 ``color`` 那一段。
    用途只有一个：这些词是**颜色**，不是角色名 —— 「发色换成银白」里的「银白」
    如果被「表外点名」链路当成角色表外的角色名，会白发一次模型解析（还有可能让
    ``fix`` 去动画面里正确的角色标签）。所以调用方把它们并进表外点名的
    ``exclude``（与 1.0.23 的「皮肤名豁免」同一套做法）。
    """
    if not text:
        return ()
    allowed = tuple(apply_to or DEFAULT_APPLY_TO)
    words: list[str] = []
    for match in _APPEARANCE_PATTERN.finditer(text):
        key = _WORD_TO_KEY.get(match.group("word") or "")
        if key is None or key not in allowed:
            continue
        color = (match.group("color") or "").strip()
        if color and color not in words:
            words.append(color)
    return tuple(words)


# ── 拼装 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppearancePart:
    """拼装里的一段：某个外观键换成了什么颜色词。

    Attributes:
        key: :data:`DEFAULT_APPLY_TO` 之一（``eyes`` / ``hair``）
        term: 未加权的英文词（``purple-red eyes``）
        weighted: 按配置语法加过权的写法（``1.4::purple-red eyes::``）
    """

    key: str
    term: str
    weighted: str

    def label(self) -> str:
        """中文外观名（``眼睛`` / ``发色``）。"""
        return APPEARANCE_LABELS.get(self.key, self.key)


@dataclass(frozen=True)
class AppearanceOverride:
    """一次外观覆盖的完整结果（加权词 + 降权意图）。

    Attributes:
        parts: 各段（**按 :data:`DEFAULT_APPLY_TO` 顺序排好，眼睛在最前**）
        weight: 覆盖词的正向权重（配置 ``eye_weight``）
        tag_weight: 角色标签降权权重（配置 ``tag_weight``；``>= 1.0`` 表示不降权）
        weight_syntax: 权重语法（``nai`` / ``paren`` / ``none``）
        source: 这句话来自哪条用户消息（日志用）
        exempt_words: 命中的**中文颜色词**（供「表外点名」链路豁免，见
            :func:`find_appearance_colors`）
    """

    parts: tuple[AppearancePart, ...] = ()
    weight: float = DEFAULT_OVERRIDE_WEIGHT
    tag_weight: float = DEFAULT_TAG_WEIGHT
    weight_syntax: str = DEFAULT_WEIGHT_SYNTAX
    source: str = ""
    exempt_words: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """是否有可注入的加权词（空 = 退回"不覆盖"）。"""
        return bool(self.parts)

    @property
    def downweights(self) -> bool:
        """本次是否**真的**会去降权角色标签（``tag_weight < 1.0`` 且语法不是 none）。"""
        return self.tag_weight < 1.0 and self.weight_syntax != WEIGHT_SYNTAX_NONE

    def terms(self) -> list[str]:
        """未加权的英文词（幂等判断用）。"""
        return [part.term for part in self.parts]

    def weighted_terms(self) -> list[str]:
        """加了权的写法（注入用）。"""
        return [part.weighted for part in self.parts]

    def is_applied(self, content: str) -> bool:
        """内容里是否**已经**带着这次的加权词（幂等：多图轮的第 2、3 张不重复追加）。"""
        return all(content_contains_tag(content, part.weighted) for part in self.parts)

    def prefix_text(self, separator: str = ", ") -> str:
        """要放到提示词**最前面**的那一段（``1.4::purple-red eyes::``）。"""
        return separator.join(part.weighted for part in self.parts if part.weighted)

    def summary(self) -> str:
        """一行式摘要（``眼睛 → purple-red eyes``），日志与 DEBUG 共用。"""
        return "；".join(f"{part.label()} → {part.term}" for part in self.parts)

    def log_line(self) -> str:
        """解析成功那一行 INFO（格式按需求原话，用户对着日志就能核对）。"""
        body = "；".join(
            f"{part.label()} → {part.term}（权重 {_format_number(self.weight)}）"
            for part in self.parts
        )
        tail = (
            f"；角色标签降权 {_format_number(self.tag_weight)}"
            if self.downweights
            else ""
        )
        return f"外观覆盖：{body}{tail}"


def build_appearance_override(
    assignments: dict[str, str],
    *,
    apply_to: tuple[str, ...] | None = None,
    weight: float = DEFAULT_OVERRIDE_WEIGHT,
    tag_weight: float = DEFAULT_TAG_WEIGHT,
    weight_syntax: str = DEFAULT_WEIGHT_SYNTAX,
    source: str = "",
    exempt_words: tuple[str, ...] = (),
) -> AppearanceOverride:
    """把 ``{键: 英文修饰词}`` 拼成一次可注入的结果（只算正向加权词）。

    按 ``apply_to`` 的顺序取（默认 ``eyes → hair``，**眼睛在最前**）；
    不在 ``apply_to`` 里、或英文词为空的键直接跳过。
    一个都没拼出来时返回**空结果**（``ok`` 为假），调用方据此零动作。
    """
    allowed = tuple(apply_to or DEFAULT_APPLY_TO)
    parts: list[AppearancePart] = []
    for key in allowed:
        modifier = (assignments.get(key) or "").strip()
        if not modifier:
            continue
        term = f"{modifier} {key}"
        parts.append(
            AppearancePart(
                key=key,
                term=term,
                weighted=format_weight(term, weight, weight_syntax),
            )
        )
    return AppearanceOverride(
        parts=tuple(parts),
        weight=weight,
        tag_weight=tag_weight,
        weight_syntax=weight_syntax,
        source=source,
        exempt_words=tuple(exempt_words),
    )


# ── 角色标签降权 ──────────────────────────────────────────────────────

#: 标签里**允许带转义反斜杠**的字符（配置里常写成 ``firefly \(honkai: star rail\)``）。
_ESCAPABLE_CHARS = "()[]<>"


def _tag_pattern(tag: str) -> re.Pattern[str]:
    """把角色标签编译成「反斜杠可有可无」的正则（大小写不敏感）。

    角色表里的标签通常写成 ``firefly \\(honkai: star rail\\)``（NAI 转义写法），
    但画面里也可能是不带反斜杠的 ``firefly (honkai: star rail)`` —— 两种都要认出来。
    """
    chunks: list[str] = []
    for char in tag:
        if char == "\\":
            continue
        if char in _ESCAPABLE_CHARS:
            chunks.append(r"\\?" + re.escape(char))
        else:
            chunks.append(re.escape(char))
    return re.compile("".join(chunks), re.IGNORECASE)


def downweight_character_tag(
    content: str,
    roster: list[CharacterEntry] | None,
    *,
    weight: float,
    weight_syntax: str = DEFAULT_WEIGHT_SYNTAX,
) -> tuple[str, str]:
    """把画面里**第一处**角色标准标签改写成降权写法。

    「第一处」的判定：先按角色表顺序找出**标准标签字面出现在内容里**的角色，
    再取其中**在内容里位置最靠前**的那一处，只改写它**第一次**出现的地方 ——
    需求原话是「只改这一处，其它标签不动」，所以绝不做全局替换。

    Returns:
        ``(新内容, 改写后的标签文本)``；没改时返回 ``(原内容, "")``。
    """
    if not content or not roster:
        return content, ""
    if weight >= 1.0 or weight_syntax == WEIGHT_SYNTAX_NONE:
        return content, ""

    best: tuple[int, str, str] | None = None
    for entry in roster:
        tag = entry.resolved_tag()
        if not tag:
            continue
        match = _tag_pattern(tag).search(content)
        if match is None:
            continue
        if best is not None and match.start() >= best[0]:
            continue
        # 保留**画面里原本那个写法**（含转义反斜杠），只加权重外壳
        best = (match.start(), match.group(0), tag)

    if best is None:
        return content, ""
    start, literal, _tag = best
    weighted = format_weight(literal, weight, weight_syntax, kind="downweight")
    if not weighted or weighted == literal:
        return content, ""
    merged = f"{content[:start]}{weighted}{content[start + len(literal):]}"
    return merged, weighted


__all__ = [
    "APPEARANCE_EYES",
    "APPEARANCE_HAIR",
    "APPEARANCE_LABELS",
    "APPEARANCE_WORDS",
    "CHANGE_VERBS",
    "COLORS",
    "DEFAULT_APPLY_TO",
    "DEFAULT_OVERRIDE_WEIGHT",
    "DEFAULT_TAG_WEIGHT",
    "DEFAULT_WEIGHT_SYNTAX",
    "WEIGHT_SYNTAXES",
    "WEIGHT_SYNTAX_NAI",
    "WEIGHT_SYNTAX_NONE",
    "WEIGHT_SYNTAX_PAREN",
    "AppearanceOverride",
    "AppearancePart",
    "build_appearance_override",
    "downweight_character_tag",
    "find_appearance_colors",
    "format_weight",
    "parse_appearance_overrides",
    "parse_apply_to",
    "parse_weight",
    "parse_weight_syntax",
]
