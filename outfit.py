"""皮肤/时装（装扮）的识别与解析（1.0.22 起）。

需求原话：「当说『我想看看你的 xxxx（皮肤）』，可以搜索并加进去。例如
『我想看粉色妖精小姐 / 春婵丽娟』→ 就会出现**爱莉穿着这一套**的衣服；
也包括**流萤的那一套皮肤时装**。」

也就是说：**角色照旧由既有判定链决定**（主题跟踪 / 描述点名 / 本尊身份 / 记忆库），
本模块只补上「皮肤 / 时装」这一维：

- **识别**（:func:`extract_outfit_names`）：只从**用户消息**里找皮肤名（**不扫画面
  描述** —— 那是被改写的一侧），句式模式 + 引号模式各抓一遍，再过长度 / 虚词字符 /
  黑名单 / 通用词 / 「已经在角色表里」五道过滤；命中 ``[outfits] entries`` 的候选
  **优先**（表里有就等于已经知道该画什么，不必猜）；
- **解析**（表优先 → 模型兜底）：皮肤表命中 → **零模型调用**，直接用表里的英文关键词
  与中文描述；表未命中且 ``outfits.auto_resolve`` 为真 → 用**已配置的模型**（与角色
  解析**同一套** ``llm_api``：模型沿用 ``general.translate_model``、超时沿用
  ``general.llm_timeout``，不引入任何新依赖）解析成
  ``{"unknown", "outfit_cn", "outfit_en", "tag", "keywords", "confidence"}``；
- **校验与降级**（:func:`parse_outfit_resolution_payload`）：``unknown=true`` / JSON 坏 /
  ``confidence`` 低于 :data:`OUTFIT_RESOLVE_MIN_CONFIDENCE` / 过滤后既无 tag 又无
  keywords → 一律视为**解析失败**，调用方**什么都不注入**（绝不瞎编），
  日志 ``皮肤解析(LLM)失败：<原因>``；
- **缓存**（与 1.0.21 的角色解析同一套分层）：**读写在调用方**
  （``handler._resolve_outfit_with_cache``），本模块只提供存储 + TTL / 上限常量 +
  :func:`reset_outfit_cache`。为什么必须这样分层：解析函数是回归用例替换（打桩）的
  那一层（``tests/check_logic.py`` 的 ``FakeOutfit``），缓存若写在它内部，用例里
  「第二次还要不要问模型」永远绕过缓存 —— 与 C75 守的是同一条。

本模块**不 import handler**（避免循环依赖）；只有写回记忆库时才用 :mod:`memory`。

**1.0.23 的三件事**（判定链一行未改，皮肤这一维只多一条可选的路）：

- **皮肤名不再被当成表外角色名**：:func:`extract_outfit_names` 给出的候选就是
  「本次识别到的皮肤 / 时装名」，由调用方（``handler._scan_outfit_names`` →
  ``CharacterGuardHandler._scan_user_naming``）传给
  ``character.extract_outside_names(exclude=…)`` **豁免** —— 用户说
  「画粉色妖精小姐这套」时，「粉色妖精小姐」是**皮肤**，不是角色表外的角色名；
- **「没说名字的那一套」**（``outfits.infer_unnamed``，**默认关**）：
  :func:`find_unnamed_outfit_owner` 认出「<角色名>的那一套皮肤 / 时装」里的角色，
  :func:`infer_outfit_via_llm` 让模型给出这个角色最知名的一套（严格 JSON，
  比 1.0.22 多一个 ``inferred: true`` 字段），校验口径与 1.0.22 **完全相同**；
  关掉时一次模型都不问、一条日志都不打，行为与 1.0.22 逐字一致；
- **作品名去重**（同一次日志里的 ``… (Honkai: Star Rail) (Honkai: Star Rail)``）
  落在 :mod:`character`（``_normalize_game_field``），与本模块无关。

**1.0.28 把「泛指的穿着类型」和「具体皮肤名」分开**（需求原话：「当说的是
『我想看你的 xxx』（如**学生**、**居家**、**泳衣**），判断**不是皮肤**，转成
『穿对应的衣服』；否则衣服这个产出有时相当于黑箱，只能由 bot 决定 —— 要能
**看到注入了什么样的衣服**」）：

- **内置表** :data:`WEAR_TYPE_KEYWORDS`（中文词 → 2~5 个小写 ASCII 关键词，
  含常见同义写法：``学生`` / ``学生装`` / ``校服`` / ``JK`` / ``水手服`` →
  ``school uniform, pleated skirt``），用户可在 ``[outfits] wear_types`` 里
  按同样写法补充 / 覆盖（同名覆盖内置，见 :func:`wear_type_table`）；
- **识别** :func:`find_wear_type`：候选**包含**表中某个词即命中（``学生装`` /
  ``居家服`` / ``泳衣``），多个词命中时取**最长**的那个；**具体皮肤名优先**
  由调用方（``handler._resolve_outfit``：皮肤表 → 穿着类型 → 模型）保证，
  所以皮肤表里的「学生会长制服」不会被拆成"学生装"；
- **模型兜底** :func:`looks_like_wear` / :func:`looks_like_wear_sentence` 认出
  「看起来是穿着」的候选（含 ``衣/服/装/裙/…`` 或句式「穿 X」「换上 X」），
  它既不在皮肤表也不在内置表时，交给 :func:`resolve_wear_via_llm` 用
  **穿着专用提示词**转成服装关键词（不是穿着就让模型回 ``unknown=true``）；
- 校验口径与皮肤解析**完全相同**（:func:`parse_outfit_resolution_payload`：
  只收小写 ASCII 关键词，``unknown`` / JSON 坏 / ``confidence < 0.5`` /
  没有可用关键词 → **什么都不注入**），日志 ``穿着解析(LLM)失败：<原因>``。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ROLE, LLMPayload, Text

from . import memory as memory_api
from .character import (
    CharacterEntry,
    _as_confidence,
    _prepare_model_set,
    _resolve_response_text,
    _strip_json_object,
    find_roster_identity,
    iter_config_entries,
    normalize_prompt_text,
)

logger = get_logger("char_prompt_guard")

#: 皮肤名的长度范围（需求：2~16 个字符）
OUTFIT_NAME_MIN_LEN = 2
OUTFIT_NAME_MAX_LEN = 16

#: 解析来源标记（进日志与缓存）；``表`` = 皮肤表命中、``模型`` = 模型给出、``缓存`` = 命中缓存。
#: 1.0.23 起另有 :data:`OUTFIT_SOURCE_INFER`（用户没说皮肤名、由模型推断出一套）。
OUTFIT_SOURCE_TABLE = "表"
OUTFIT_SOURCE_MODEL = "模型"
OUTFIT_SOURCE_CACHE = "缓存"

#: 1.0.23：「没说名字的那一套」的来源标记。**不进** ``source`` 字段（那一列仍然写
#: ``模型`` / ``缓存``，日志前缀才是区分点），只作为 :class:`ResolvedOutfit.inferred`
#: 为真时日志里的「来源=…」文案与语义标识。
OUTFIT_SOURCE_INFER = "模型推断"

#: 1.0.28：「泛指的穿着类型」的两条路（**不进** ``source`` 字段，理由与
#: :data:`OUTFIT_SOURCE_INFER` 完全相同 —— ``source`` 那一列仍然写
#: ``模型`` / ``缓存``，日志前缀才是区分点）。它们填在
#: :attr:`ResolvedOutfit.wear` 上：
#:
#: - ``类型``：命中内置 / 配置的**穿着类型表**（零模型调用）；
#: - ``模型``：穿着类候选交给**穿着专用提示词**解析出来的。
OUTFIT_WEAR_TYPE = "类型"
OUTFIT_WEAR_MODEL = "模型"

#: 皮肤表条目里「角色名」这一列缺失时，写回记忆库用的兜底归属名（不好看但保证三元组非空）
OUTFIT_UNKNOWN_CHARACTER = "未知角色"


# ── 皮肤表（[outfits] entries）────────────────────────────────────────


@dataclass
class OutfitEntry:
    """皮肤表条目：``皮肤名 | 角色名(可空) | 英文关键词或 NAI 标签 | 中文描述(可空)``。"""

    name: str
    character: str = ""
    keywords: list[str] = field(default_factory=list)
    description: str = ""


def split_outfit_keywords(raw_text: str) -> list[str]:
    """把「英文关键词 / NAI 标签」那一列切成关键词列表（中英文逗号都认）。"""
    text = (raw_text or "").replace("，", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_outfits(raw_text: str) -> list[OutfitEntry]:
    """解析皮肤表文本（1.0.22）。

    每个条目一套皮肤：``皮肤名 | 角色名 | 英文关键词或 NAI 标签 | 中文描述``，
    后三项均可省略；条目之间可用换行或分号（``;`` / ``；``）分隔；整行以 ``#``
    开头则为注释（行尾 ``空白 + #`` 也算注释，规则与角色表完全一致，
    共用 :func:`character.iter_config_entries`）。

    示例（配置里默认只给这一行，照抄改掉即可）::

        粉色妖精小姐 | 爱莉希雅 | pink elf dress, fairy wings | 爱莉希雅的粉色妖精装
    """
    entries: list[OutfitEntry] = []
    for line in iter_config_entries(raw_text):
        line = line.strip()
        if not line:
            continue
        # 兼容中文输入法打出的全角分隔符
        line = line.replace("｜", "|")
        parts = [part.strip() for part in line.split("|")]
        name = parts[0] if parts else ""
        if not name:
            continue
        entries.append(
            OutfitEntry(
                name=name,
                character=parts[1] if len(parts) > 1 else "",
                keywords=split_outfit_keywords(parts[2]) if len(parts) > 2 else [],
                description=parts[3] if len(parts) > 3 else "",
            )
        )
    return entries


def _outfit_key(text: str) -> str:
    """比较用的皮肤名键：忽略大小写与**所有空白**（``粉色 妖精`` 与 ``粉色妖精`` 等价）。"""
    return re.sub(r"\s+", "", (text or "").lower())


def find_outfit_entry(name: str, entries: list[OutfitEntry]) -> OutfitEntry | None:
    """按皮肤名在皮肤表里找条目；没有返回 ``None``（1.0.22）。

    比较走 :func:`_outfit_key`（忽略大小写与空白），并且**允许只写一部分**：

    1. 完全相同（``粉色妖精小姐``）；
    2. 表里的名字包含用户写法（``粉色妖精`` → ``粉色妖精小姐``）——
       用户很少把一整套名字写全；
    3. 用户写法包含表里的名字（``粉色妖精小姐这套`` → ``粉色妖精小姐``）。

    找不到就返回 ``None``，由调用方决定是否交给模型兜底 ——
    **绝不做模糊猜测**（猜错会让下游把衣服画成另一套）。
    """
    target = _outfit_key(name)
    if not target:
        return None
    for entry in entries:
        if _outfit_key(entry.name) == target:
            return entry
    for entry in entries:
        key = _outfit_key(entry.name)
        if key and (key in target or target in key):
            return entry
    return None


# ── 皮肤名的识别（只从用户消息里找）──────────────────────────────────
#
# 为什么不扫画面描述：画面描述是**被改写**的那一侧（与 1.0.20 的表外点名同一口径）。
# 用户说「我想看你的粉色妖精小姐」时，皮肤名在**用户消息**里；画面描述里出现
# 「粉色长裙」这种字眼并不代表用户在点名一套皮肤。

#: 候选里**不允许**出现的标点（同时用作句式模式里 X 的边界）
_PUNCT_CHARS = "，。！？；：、,.!?;:…～~·「」『』《》‘’“”\"'（）()[]【】{}<>—－-/|\\"

#: 候选里**不允许**出现的虚词字符：代词 / 语气词 / 常见动词 / 量词副词。
#:
#: 句式模式没有引号那种"这是一个名字"的显式信号，不加这一道就会把整句话
#: （「我想看你的漂亮衣服」抓成「你的漂亮衣服」）当成皮肤名 —— 与 ``character``
#: 的「表外点名」同一思路：**宁少勿多**（漏了只是不注入，抓错会把衣服画错）。
_OUTFIT_STOP_CHARS = (
    "的了着过吧吗呢啊呀哦嘛呗啦咯喔"
    "我你您他她它这那哪谁什么"
    "是没能会想来看去用把被和与跟"
    "有又也都就还再很太更个张只些点们"
)

#: 句式模式里的 ``X``：2~16 个「非标点、非空白、非虚词」字符。
_OUTFIT_NAME_CLASS = "[^" + re.escape(_PUNCT_CHARS + _OUTFIT_STOP_CHARS) + r"\s]"

#: 皮肤名候选**允许**出现的字符：字母 / 数字 / 汉字 / 日文假名 / 韩文 / 中点。
#: 要求**整体匹配**：候选里只要出现别的字符（空白、标点、括号）就不算皮肤名。
_OUTFIT_NAME_PATTERN = re.compile(
    r"[0-9A-Za-z\u00b7\u30fb\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+"
)

#: 引号模式：「…」 / 『…』 / “…” / "…" 里的内容（引号本身是很强的"这是一个名字"信号）
_OUTFIT_QUOTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"「([^「」]{2,40})」"),
    re.compile(r"『([^『』]{2,40})』"),
    re.compile(r"“([^“”]{2,40})”"),
    re.compile(r"\"([^\"\n]{2,40})\""),
)

#: 句式模式（需求里点名的七种说法，``X`` = :data:`_OUTFIT_NAME_CLASS` 的 2~16 次）。
#:
#: 顺序有意从具体到宽泛：``我想看你的X`` 在 ``我想看X`` 前面，``你的`` 里的「你」
#: 又是虚词字符，因此「我想看你的粉色妖精小姐」只会抓到皮肤名本身。
_OUTFIT_SENTENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"我想看(?:看)?你的({_OUTFIT_NAME_CLASS}{{2,16}})"),
    re.compile(rf"看看你的({_OUTFIT_NAME_CLASS}{{2,16}})"),
    re.compile(rf"穿上?({_OUTFIT_NAME_CLASS}{{2,16}})"),
    re.compile(rf"换上({_OUTFIT_NAME_CLASS}{{2,16}})"),
    re.compile(rf"({_OUTFIT_NAME_CLASS}{{2,16}})(?:这|那)(?:一)?套"),
    re.compile(rf"({_OUTFIT_NAME_CLASS}{{2,16}})(?:皮肤|时装|衣服|裙子|装扮|造型)"),
    re.compile(rf"我想看({_OUTFIT_NAME_CLASS}{{2,16}})"),
)

#: 候选黑名单：指代词 / 泛指 / 画面元素本身（皮肤名不可能长这样）。
#:
#: 命中方式与 ``character`` 的表外点名一致：候选**与词完全相同**或**包含该词**。
OUTFIT_BLACKLIST: tuple[str, ...] = (
    # 人称代词 / 指示代词
    "你",
    "你们",
    "您",
    "我",
    "我们",
    "他",
    "他们",
    "她",
    "她们",
    "它",
    "这",
    "那",
    "哪",
    "谁",
    "自己",
    "本人",
    "本尊",
    "真身",
    "本体",
    # 画面元素本身（不是皮肤名）
    "图",
    "图片",
    "照片",
    "样子",
    "模样",
    "长相",
    "外观",
    "画面",
    "镜头",
    "东西",
    "人物",
    "角色",
    "什么",
    "怎么",
    "为什么",
    # 时间 / 泛指
    "今天",
    "现在",
    "刚才",
    "最后",
    "别的",
    "其它",
    "其他",
    "每个",
    "所有",
)

#: 「衣服 / 皮肤 / 时装」这类**通用词**：它们**不是**皮肤名。
#:
#: 用户说「穿衣服」「换上衣服」时不该拿它去问模型 —— 那只会解析出一堆通用标签，
#: 而且会与「穿(上)X」这条句式互相打架。规则：候选**恰好等于**通用词，
#: 或者以通用词结尾但前面的名字不足 2 个字（「新衣服」这种泛指）→ 丢弃。
#:
#: ``一套 / 这套 / 那套`` 也在这里：``X(皮肤|时装)`` 那条句式在
#: 「流萤的**那一套**皮肤」里会抓到「一套」，那不是皮肤名（用户没点名具体是哪一套）。
OUTFIT_GENERIC_WORDS: tuple[str, ...] = (
    "衣服",
    "服装",
    "服饰",
    "衣装",
    "衣裳",
    "皮肤",
    "时装",
    "裙子",
    "装扮",
    "造型",
    "外套",
    "穿搭",
    "套装",
    "全身",
    "一身",
    "这身",
    "那身",
    "一套",
    "这套",
    "那套",
    "两套",
    "几套",
    "哪套",
)


def _is_generic_outfit(candidate: str) -> bool:
    """候选是不是「通用词」（见 :data:`OUTFIT_GENERIC_WORDS`）。"""
    text = (candidate or "").strip()
    for word in OUTFIT_GENERIC_WORDS:
        if text == word:
            return True
        if text.endswith(word) and len(text) - len(word) < OUTFIT_NAME_MIN_LEN:
            return True
    return False


def _in_roster(candidate: str, roster: list[CharacterEntry]) -> bool:
    """候选是不是「角色表内」的名字（那些归角色逻辑，不算皮肤名）。

    与 ``character._in_roster`` 同一口径：完全相同（名字 / 别名 / 标准标签 /
    作品名）或**互为子串**（「爱莉」是「爱莉希雅」的简称）都算表内。
    """
    if not roster:
        return False
    target = normalize_prompt_text(candidate)
    if not target:
        return True
    for entry in roster:
        for raw in (entry.name, *entry.aliases, entry.tag, entry.game):
            known = normalize_prompt_text(str(raw or ""))
            if known and (known in target or target in known):
                return True
    return False


def _outfit_like(candidate: str, roster: list[CharacterEntry]) -> bool:
    """候选是否**像**一个皮肤名（长度 / 字符 / 虚词 / 黑名单 / 通用词 / 角色表）。

    与皮肤表的命中判定分开：**表里的皮肤名优先**（表命中时不过这一关 ——
    表是用户自己维护的、明确表示"这就是一套皮肤"），这里只管表外的候选。
    """
    text = (candidate or "").strip()
    if not (OUTFIT_NAME_MIN_LEN <= len(text) <= OUTFIT_NAME_MAX_LEN):
        return False
    if _OUTFIT_NAME_PATTERN.fullmatch(text) is None:
        return False
    if any(char in _OUTFIT_STOP_CHARS for char in text):
        return False
    for word in OUTFIT_BLACKLIST:
        if word and (word == text or word in text):
            return False
    if _is_generic_outfit(text):
        return False
    return not _in_roster(text, roster)


def extract_outfit_names(
    text: str,
    entries: list[OutfitEntry] | None = None,
    roster: list[CharacterEntry] | None = None,
) -> list[str]:
    """从一条**用户消息**里提取皮肤/时装名候选（1.0.22）。

    规则（对应需求里的两条提取模式 + 四条过滤）：

    1. **引号模式**：``「…」`` / ``『…』`` / ``“…”`` / ``"…"`` 里的内容；
    2. **句式模式**：``我想看(看)?你的X`` / ``看看你的X`` / ``穿(上)?X`` / ``换上X`` /
       ``X这套|这一套`` / ``X(皮肤|时装|衣服|裙子|装扮|造型)`` / ``我想看X``
       （``X`` = 2~16 个非标点、非空白、非虚词字符）；
    3. **过滤**：长度 2~16、整体是名字字符、不含虚词字符、不命中黑名单、
       不是「衣服/皮肤」这类通用词、**不在角色表里**（那些归角色逻辑）；
    4. **皮肤表命中优先**：只要有候选命中 ``entries``（大小写 / 空白不敏感，
       允许只写一部分：「粉色妖精」→「粉色妖精小姐」），就**只**返回这些候选 ——
       表是用户自己维护的确定答案，比表外的自由文本更可信。

    调用方（``handler._scan_outfit_names``）负责只喂**用户消息**（bot 的复述不算、
    画面描述不算），并沿用自我指代 / 表外点名的焦点范围。

    Args:
        text: 一条用户消息的正文（调用方应先用 :func:`character.message_text`）
        entries: 皮肤表（可为空）
        roster: 角色表（用于剔除"表内"的名字；为空时不做这一步）

    Returns:
        候选皮肤名列表（去重、保序；没有则空列表）
    """
    if not text:
        return []
    raw: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        name = (candidate or "").strip()
        if not (OUTFIT_NAME_MIN_LEN <= len(name) <= OUTFIT_NAME_MAX_LEN):
            return
        key = _outfit_key(name)
        if not key or key in seen:
            return
        seen.add(key)
        raw.append(name)

    for pattern in _OUTFIT_QUOTED_PATTERNS:
        for match in pattern.finditer(text):
            add(match.group(1))
    for pattern in _OUTFIT_SENTENCE_PATTERNS:
        for match in pattern.finditer(text):
            add(match.group(1))
    if not raw:
        return []

    table_hits = [
        name for name in raw if find_outfit_entry(name, entries or []) is not None
    ]
    if table_hits:
        return table_hits
    return [name for name in raw if _outfit_like(name, roster or [])]


# ── 「没说名字的那一套」（1.0.23，默认关）─────────────────────────────
#
# 用户需求原话：「『流萤的那一套皮肤』这种没点名字的说法不会注入……做一个（开关），
# 默认关就行，开了可以让模型去查（皮肤名字）然后注入提示词」。
#
# 识别部分**只认角色表内的角色名**（用现有角色表匹配）：`<角色名>的?(那)?一套(皮肤|时装|…)`
# 与 `<角色名>的(皮肤|时装)`。这里刻意**不做**任何"猜角色"的事 —— 猜错角色比不注入更糟。
# 是否真的问模型由调用方按 `outfits.infer_unnamed`（默认 false）决定。

#: 「没说名字的那一套」的句式尾巴：``的?(那)?一套(皮肤|时装|衣服|裙子|装扮|造型)``
#: 与 ``的(皮肤|时装)``。角色名由 :func:`_unnamed_outfit_pattern` 按角色表现拼。
_UNNAMED_OUTFIT_TAIL = (
    r"的?(?:那)?一套(?:皮肤|时装|衣服|裙子|装扮|造型)|的(?:皮肤|时装)"
)


def _unnamed_outfit_pattern(roster: list[CharacterEntry]) -> re.Pattern[str] | None:
    """拼出「<角色名>的那一套皮肤 / 时装」的正则（1.0.23）；角色表为空时返回 ``None``。

    角色名取角色表里的**名字与别名**（标准标签 / 作品名不参与 —— 用户不会用
    ``elysia \\(honkai impact 3rd\\)`` 这种写法指代一个角色）；名字长的排在前面，
    免得「爱莉」先把「爱莉希雅」吃掉。
    """
    names: list[str] = []
    seen: set[str] = set()
    for entry in roster:
        for raw in (entry.name, *entry.aliases):
            text = (raw or "").strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                names.append(text)
    if not names:
        return None
    names.sort(key=len, reverse=True)
    escaped = "|".join(re.escape(name) for name in names)
    return re.compile(rf"({escaped})(?:{_UNNAMED_OUTFIT_TAIL})")


def find_unnamed_outfit_owner(text: str, roster: list[CharacterEntry]) -> str:
    """从一条**用户消息**里找出「<角色名>的那一套皮肤 / 时装」说的是谁（1.0.23）。

    只认两种句式，且角色名必须是**角色表内**的角色（名字或别名）：

    - ``<角色名>的?(那)?一套(皮肤|时装|衣服|裙子|装扮|造型)``（「流萤的那一套皮肤」）；
    - ``<角色名>的(皮肤|时装)``（「流萤的皮肤」）。

    这正是 1.0.22 **刻意不做**的那种说法（用户没点名具体是哪一套，1.0.22 直接跳过）：
    1.0.23 起由 ``outfits.infer_unnamed``（**默认关**）决定要不要交给模型推断。
    本函数**只做识别**：返回角色表里的**正式名**（一条消息里多处命中时取最靠前的那个），
    没有这种说法时返回空串 —— 不猜角色、不调模型、不打日志。

    Args:
        text: 一条用户消息的正文（调用方应先用 :func:`character.message_text`）
        roster: 角色表（角色名必须是表内角色）

    Returns:
        角色表里的正式名；没有则空串
    """
    if not text or not roster:
        return ""
    pattern = _unnamed_outfit_pattern(roster)
    if pattern is None:
        return ""
    for match in pattern.finditer(text):
        # finditer 由左到右，第一个能对回角色表条目的就是最靠前的那个说法
        entry = find_roster_identity(match.group(1), roster)
        if entry is not None:
            return entry.name
    return ""


# ── 关键词 / 标签的规范（模型给的东西一律先过这一关）──────────────────

#: 皮肤关键词允许的字符：**小写**字母 / 数字 / 空格 / ``_`` / ``-`` / ``:`` / ``'``
#: 与转义括号 ``\\(`` ``\\)``。逗号**不在**这里 —— 它只作为关键词之间的分隔符
#: （见 :func:`validate_outfit_tag`），单个关键词里出现逗号说明模型没按格式写。
OUTFIT_KEYWORD_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789 _-:'\\()"
)

#: 单个关键词的长度上限（防模型把一整段描述塞进来）
OUTFIT_KEYWORD_MAX_CHARS = 80

#: 解析结果的 ``confidence`` 阈值（与角色解析同为 **0.5**）：低于它就当解析失败，
#: **什么都不注入** —— 宁可少一套皮肤，也不要按模型一半的把握把衣服画错。
OUTFIT_RESOLVE_MIN_CONFIDENCE = 0.5


def validate_outfit_keyword(word: str) -> tuple[bool, str]:
    """校验一条皮肤关键词是否「小写 ASCII 且不含可疑字符」（1.0.22）。

    规范（与需求一致）：只能含**小写**字母 / 数字 / 空格 / ``_`` / ``-`` / ``:`` /
    ``'`` 与**转义括号** ``\\(`` ``\\)``；大写字母、未转义括号、反斜杠的其它写法、
    中文或任何非 ASCII 字符一律非法。

    Returns:
        ``(是否合法, 原因)``；原因会进 DEBUG 日志（丢弃了几个关键词）。
    """
    text = (word or "").strip().strip(",").strip()
    if not text:
        return False, "关键词为空"
    if len(text) > OUTFIT_KEYWORD_MAX_CHARS:
        return False, f"关键词过长（{len(text)} 字符）"
    illegal = sorted(
        {char for char in text if char not in OUTFIT_KEYWORD_ALLOWED_CHARS}
    )
    if illegal:
        return False, f"关键词含非法字符 {' '.join(repr(char) for char in illegal)}"
    if text != text.lower():
        return False, "关键词含大写字母（必须全小写）"
    if "\\(" in text or "\\)" in text:
        # 1.0.26：`\\(…\\)` 是**角色标签**的形状（如 hanabi \\(honkai: star rail\\)），
        # 皮肤关键词绝不该长这样 —— 否则模型可以借"衣服关键词"这条通道把角色标签塞进
        # 画面，而两条一致性检查跑在皮肤注入**之前**，本轮结构上看不到它。
        return False, "关键词形如角色标签（含 \\( … \\)），已丢弃"
    if text.count("\\(") != text.count("\\)"):
        return False, "关键词里的括号没有成对转义"
    remainder = text.replace("\\(", "").replace("\\)", "")
    if "(" in remainder or ")" in remainder:
        return False, "关键词里的括号必须转义为 \\( 与 \\)"
    if "\\" in remainder:
        return False, "关键词含未转义的反斜杠"
    return True, ""


def validate_outfit_tag(tag: str) -> tuple[bool, str]:
    """校验「皮肤标签」——按逗号切分后逐条走 :func:`validate_outfit_keyword`。

    与角色标签（:func:`character.validate_tag`）的区别：皮肤关键词**没有**
    「``角色名 \\(作品名\\)``」这种格式要求（一套衣服本来就没有作品名括号），
    但仍要求「小写 ASCII + 逗号分隔的安全词」—— 模型给出的中文 / 大写 / 花括号
    一律会被这一关拦掉。
    """
    text = (tag or "").strip()
    if not text:
        return False, "标签为空"
    if len(text) > 240:
        return False, f"标签过长（{len(text)} 字符）"
    pieces = [piece.strip() for piece in re.split(r"[,，]", text) if piece.strip()]
    if not pieces:
        return False, "标签里没有可用的关键词"
    for piece in pieces:
        ok, reason = validate_outfit_keyword(piece)
        if not ok:
            return False, reason
    return True, ""


# ── 泛指的「穿着类型」（1.0.28）────────────────────────────────────────
#
# 需求原话：「当说的是『我想看你的 xxx』（如**学生**、**居家**、**泳衣**），
# 判断**不是皮肤**，转成"穿对应的衣服"；否则衣服这个产出有时相当于黑箱，
# 只能由 bot 决定 —— 要能**看到注入了什么样的衣服**。」
#
# 也就是说：用户说的不是**具体皮肤名**（「粉色妖精小姐」），而是**泛指的穿着
# 类型**（学生 / 居家 / 泳衣 / 睡衣 …）时，插件应当**确定性地**给出英文关键词，
# 而不是把这件事交给模型 —— 模型可能解析不出、也可能解析成别的东西，而且用户
# 在日志里看不到"到底注入了什么"。
#
# 最终优先级（见 ``handler._resolve_outfit``，注释与 README 都按这一份写）：
#   ① 皮肤表命中（``outfits.entries``，零模型调用）
#   ② 内置 / 配置的穿着类型表（:data:`WEAR_TYPE_KEYWORDS` + ``outfits.wear_types``，零模型调用）
#   ③ 皮肤模型解析（``outfits.auto_resolve``，针对**具体皮肤名**）
#   ④ 穿着模型解析（同样 ``auto_resolve``，针对**穿着类候选**，见 :func:`looks_like_wear`）
#   ⑤ ``outfits.infer_unnamed``（用户没说名字时由模型推断一套，**默认关**）
# ③ 与 ④ 互斥：候选"看起来是穿着"时走④（穿着专用提示词），否则走③（皮肤提示词）。

#: 内置的「穿着类型 → 英文关键词」表（1.0.28）。
#:
#: 键是**中文词**（用户会说的那种），值是 2~5 个**小写 ASCII** 英文关键词，风格与
#: 皮肤关键词一致（写成 NovelAI / Danbooru 能直接用的外观词）。匹配是**包含**式的
#: （:func:`find_wear_type`）：候选里出现该词就算命中（``学生装`` / ``居家服`` /
#: ``泳衣``），因此这里只放"指向明确、不会与具体皮肤名混淆"的词 —— **宁缺勿滥**。
#:
#: 常见**同义写法**都列进来了（``学生`` / ``学生装`` / ``校服`` / ``JK`` / ``水手服``
#: 映射到同一组关键词），免得同一个意思换个说法就白跑一次模型；命中的是**最长的**
#: 那个词，所以日志里写的是用户实际说的那个（``穿着类型解析(内置表): 学生装 → …``）。
#: 用户可以按同样的写法在 ``[outfits] wear_types`` 里补充 / 覆盖（同名覆盖内置，
#: 见 :func:`wear_type_table`）。
WEAR_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 学生 / 校服（5 种同义写法 → 同一组关键词）
    "学生": ("school uniform", "pleated skirt"),
    "学生装": ("school uniform", "pleated skirt"),
    "校服": ("school uniform", "pleated skirt"),
    "JK": ("school uniform", "pleated skirt"),
    "水手服": ("sailor dress", "school uniform"),
    # 制服（泛指的制服；「学生会长制服」这种具体皮肤名由皮肤表优先）
    "制服": ("uniform", "military uniform", "necktie"),
    # 居家（4 种同义写法）
    "居家": ("loungewear", "casual", "home wear", "t-shirt"),
    "家居": ("loungewear", "casual", "home wear", "t-shirt"),
    "居家服": ("loungewear", "casual", "home wear", "t-shirt"),
    "家居服": ("loungewear", "casual", "home wear", "t-shirt"),
    # 睡衣
    "睡衣": ("pajamas", "sleepwear"),
    "睡裙": ("nightgown", "sleepwear"),
    # 泳装
    "泳衣": ("swimsuit",),
    "泳装": ("swimsuit",),
    "比基尼": ("bikini", "swimsuit"),
    # 传统服饰
    "浴衣": ("yukata", "japanese clothes", "floral print"),
    "和服": ("kimono", "japanese clothes", "obi"),
    "汉服": ("hanfu", "chinese clothes", "long sleeves"),
    "旗袍": ("qipao", "chinese clothes", "side slit"),
    # 职业 / 风格
    "女仆": ("maid", "maid headdress", "apron"),
    "女仆装": ("maid", "maid headdress", "apron"),
    "护士": ("nurse uniform", "nurse cap"),
    "护士服": ("nurse uniform", "nurse cap"),
    "西装": ("suit", "necktie", "blazer"),
    "运动": ("sportswear", "track suit", "sneakers"),
    "运动服": ("tracksuit", "sportswear", "sneakers"),
    "洛丽塔": ("lolita fashion", "frilled dress", "petticoat"),
    "兔女郎": ("bunny girl", "leotard", "rabbit ears"),
    # 婚服（4 种同义写法 → 同一组关键词；「礼服」是更泛的那一档）
    "婚纱": ("wedding dress", "bridal veil", "white dress"),
    "婚服": ("wedding dress", "bridal veil", "white dress"),
    "结婚礼服": ("wedding dress", "bridal veil", "white dress"),
    "新娘装": ("wedding dress", "bridal veil", "white dress"),
    "礼服": ("evening dress", "formal dress", "gown"),
}

#: 穿着类型的**来源**（只进日志括注）：命中 :data:`WEAR_TYPE_KEYWORDS` 写 ``内置表``，
#: 命中 ``[outfits] wear_types`` 写 ``配置表``。
WEAR_SOURCE_BUILTIN = "内置表"
WEAR_SOURCE_CONFIG = "配置表"


@dataclass(frozen=True)
class WearTypeMatch:
    """一次「穿着类型」命中（1.0.28）。

    Attributes:
        word: 命中的中文词（**表里的那一列**，不是用户的原话；进日志与注入说明）
        keywords: 对应的英文关键词（已过 :func:`validate_outfit_keyword`）
        source: :data:`WEAR_SOURCE_BUILTIN` / :data:`WEAR_SOURCE_CONFIG`
            （日志里写明这次命中来自哪张表）
    """

    word: str
    keywords: tuple[str, ...]
    source: str = WEAR_SOURCE_BUILTIN


def parse_wear_types(raw_text: str) -> dict[str, tuple[str, ...]]:
    """解析 ``[outfits] wear_types`` 文本（1.0.28）。

    每行 ``中文词 | 英文关键词, 英文关键词``（第二列中英文逗号都认）；多行或分号
    （``;`` / ``；``）分隔、整行 ``#`` 开头为注释 —— 分隔与注释规则与角色表 /
    皮肤表**完全一致**（共用 :func:`character.iter_config_entries`）。

    第二列逐条过 :func:`validate_outfit_keyword`（只留小写 ASCII 的安全词），
    **一个合法关键词都不剩的行整行丢弃** —— 用户写错一行不会把这条链路弄坏，
    也不会把内置项顶掉（见 :func:`wear_type_table`）。
    """
    table: dict[str, tuple[str, ...]] = {}
    for line in iter_config_entries(raw_text):
        line = line.strip()
        if not line:
            continue
        # 兼容中文输入法打出的全角分隔符
        line = line.replace("｜", "|")
        parts = [part.strip() for part in line.split("|")]
        word = parts[0] if parts else ""
        if not word:
            continue
        keywords: list[str] = []
        for keyword in split_outfit_keywords(parts[1] if len(parts) > 1 else ""):
            ok, reason = validate_outfit_keyword(keyword)
            if not ok:
                logger.debug(f"穿着类型关键词被丢弃（{reason}）：{keyword!r}（{word}）")
                continue
            if keyword not in keywords:
                keywords.append(keyword)
        if not keywords:
            logger.debug(f"穿着类型「{word}」没有可用的英文关键词，整行丢弃")
            continue
        table[word] = tuple(keywords)
    return table


def wear_type_table(raw_text: str = "") -> dict[str, WearTypeMatch]:
    """内置表 + 用户配置**合并**后的穿着类型表（1.0.28）。

    ``raw_text`` 为空（``[outfits] wear_types`` 留空，默认）时只有内置表。
    合并口径：**同名（忽略大小写与空白）由用户配置覆盖内置**，其余内置项照旧 ——
    所以"留空则只用内置表"与"填了就合并"是同一条路径的两个端点。
    来源标记（:data:`WEAR_SOURCE_BUILTIN` / :data:`WEAR_SOURCE_CONFIG`）在这里定下来，
    日志才能写清 ``穿着类型解析(内置表)`` / ``穿着类型解析(配置表)``。
    """
    merged: dict[str, WearTypeMatch] = {
        word: WearTypeMatch(word=word, keywords=keywords)
        for word, keywords in WEAR_TYPE_KEYWORDS.items()
    }
    if not raw_text:
        return merged
    by_key = {_outfit_key(word): word for word in merged}
    for word, keywords in parse_wear_types(raw_text).items():
        key = _outfit_key(word)
        if not key:
            continue
        old = by_key.get(key)
        if old is not None:
            logger.debug(f"穿着类型「{word}」覆盖内置项「{old}」")
            merged.pop(old, None)
        by_key[key] = word
        merged[word] = WearTypeMatch(
            word=word, keywords=keywords, source=WEAR_SOURCE_CONFIG
        )
    return merged


def find_wear_type(
    name: str, table: dict[str, WearTypeMatch] | None = None
) -> WearTypeMatch | None:
    """候选里**包含**某个穿着类型词 → 命中（1.0.28）；没有返回 ``None``。

    - 匹配是**包含**式：``学生装`` / ``居家服`` / ``泳衣`` 都算命中（需求里的容错口径）；
    - 多个词同时命中时取**最长的**那个（``结婚礼服`` 压过 ``礼服``、``运动服``
      压过 ``运动``）；同样长时取表里**先出现**的那个；
    - ``table`` 为空（``None``）时用内置表 :data:`WEAR_TYPE_KEYWORDS`；
      调用方（``handler``）传的是"内置 + 配置合并"后的表（带缓存）；
    - **具体皮肤名优先**由调用方保证（``handler._resolve_outfit`` 先查皮肤表）：
      这里只负责"像不像一个穿着类型"，不做任何猜测。
    """
    target = _outfit_key(name)
    if not target:
        return None
    lookup = (
        {
            word: WearTypeMatch(word=word, keywords=keywords)
            for word, keywords in WEAR_TYPE_KEYWORDS.items()
        }
        if table is None
        else table
    )
    best: WearTypeMatch | None = None
    best_len = 0
    for word, match in lookup.items():
        key = _outfit_key(word)
        if not key or key not in target:
            continue
        if len(key) > best_len:
            best = match
            best_len = len(key)
    return best


#: 1.0.28：候选里的「穿着特征字」—— 命中任一个字就认为这次说的**可能是穿着**，
#: 于是走「穿着模型解析」（提示词要求模型把它转成服装关键词），而不是「皮肤解析」。
#:
#: 需求里列的多字词（``礼服`` / ``装束`` / ``制服``）都被这里的单字覆盖
#: （``服`` / ``装`` / ``制``），句式信号见 :func:`looks_like_wear_sentence`。
WEAR_LOOK_CHARS: tuple[str, ...] = (
    "衣",
    "服",
    "装",
    "裙",
    "裤",
    "袜",
    "鞋",
    "帽",
    "泳",
    "袍",
    "衫",
    "校",
    "居",
    "睡",
    "浴",
    "婚",
    "制",
    "套",
)

#: 1.0.28：「这次说的是穿着」的**句式**信号（需求原话：「穿 X」「换上 X」「X 服/装/衣」）。
#:
#: ``X`` 与皮肤名候选用的是同一个字符类（:data:`_OUTFIT_NAME_CLASS`），因此这里认的
#: 就是"皮肤名识别真正会抓到的那些说法"。候选本身没有穿着特征字时（例如「换上战甲」），
#: 靠它把候选归到"穿着类"。
_WEAR_SENTENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"穿(?:上)?{_OUTFIT_NAME_CLASS}{{2,16}}"),
    re.compile(rf"换上{_OUTFIT_NAME_CLASS}{{2,16}}"),
    re.compile(rf"{_OUTFIT_NAME_CLASS}{{2,16}}(?:衣服|服装|衣装|服饰)"),
)


def looks_like_wear(name: str) -> bool:
    """候选本身**看起来是穿着**吗（1.0.28）：命中任一 :data:`WEAR_LOOK_CHARS`。

    只是"归到哪一条模型提示词"的判据（穿着词 vs 具体皮肤名），**不是**命中判定：
    命中之后照样要过 :func:`validate_outfit_keyword`，模型说不是穿着（``unknown``）
    就什么都不注入。
    """
    text = _outfit_key(name)
    if not text:
        return False
    return any(char in text for char in WEAR_LOOK_CHARS)


def looks_like_wear_sentence(text: str) -> bool:
    """用户这句话是不是「穿 X」/「换上 X」/「X 服(装|衣)」这种说法（1.0.28）。

    ``text`` 应当是**用户消息正文**（调用方传 ``OutfitScope.source``：
    抓到候选的那条消息）。没有这些说法时返回 ``False`` —— 此时按"具体皮肤名"处理。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    return any(pattern.search(raw) is not None for pattern in _WEAR_SENTENCE_PATTERNS)


def wear_like_candidate(name: str, source_text: str = "") -> bool:
    """这次识别到的候选是不是**穿着类**（1.0.28）—— ③/④ 两条模型路的分岔口。

    命中任一条即算穿着类：

    1. 候选本身含穿着特征字（:func:`looks_like_wear`，例如「雨衣」「白色长裙」）；
    2. 抓到候选的那条用户消息是「穿 X」/「换上 X」这种说法
       （:func:`looks_like_wear_sentence`，例如「换上战甲」）。

    **具体皮肤名不会被误判**：皮肤表（①）与穿着类型表（②）都在它之前，
    走不到这里；走到这里的候选本来就要问模型，只是问哪一套提示词的问题。
    """
    return looks_like_wear(name) or looks_like_wear_sentence(source_text)


# ── 解析结果 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedOutfit:
    """一次「皮肤/时装 / 穿着」解析的结果（1.0.22；1.0.23 与 1.0.28 各加字段）。

    Attributes:
        name: 皮肤名（表里的正式名 / 模型给的中文名 / 用户说法）；**1.0.28 的穿着
            两条路**分别填命中的中文词（``学生``）与用户的**原词**（``雨衣``）
        character: 归属角色名（表里的第 2 列 / 焦点范围内的角色线索；可空）。
            **穿着类型没有归属列**，因此穿着那两条路一律留空（见
            ``handler._apply_outfit`` 的归属校验）
        keywords: 英文关键词（已逐条过 :func:`validate_outfit_keyword`）
        description: 中文描述（表里的第 4 列；模型与穿着那两条路都不提供）
        source: ``表`` / ``模型`` / ``缓存``（失败时为空串）
        confidence: 模型自评的把握（0~1；表命中按 1.0）
        reason: 失败原因（成功时为空串）
        raw_name: 用户消息里的原始写法（推断路径下是**角色名**）
        inferred: 1.0.23 —— 这一套是**模型推断**出来的（用户没说皮肤名，
            只说了「<角色>的那一套皮肤」）。日志前缀与「来源=」都据此变化，
            调用方也用它决定「不写回记忆库」（见 ``handler._resolve_outfit_with_cache``）
        wear: 1.0.28 —— 这一套是**泛指的穿着**，不是具体皮肤名。
            ``""`` = 不是穿着；:data:`OUTFIT_WEAR_TYPE` = 命中内置 / 配置的穿着
            类型表（零模型调用）；:data:`OUTFIT_WEAR_MODEL` = 穿着专用提示词
            解析出来的。它决定日志前缀（``穿着类型解析`` / ``穿着解析``）与注入
            措辞（``（穿着：“…”：…）``），也是**归属校验**与幂等标记的判据。
            为什么不用 ``source``：命中缓存时 ``source`` 会被改写成 ``缓存``，
            而"这是穿着"必须能挺过改写（否则缓存命中会打出 ``皮肤解析(缓存)``）
        wear_source: 1.0.28 —— 穿着类型命中来自哪张表
            （:data:`WEAR_SOURCE_BUILTIN` / :data:`WEAR_SOURCE_CONFIG`），
            只用于 ``穿着类型解析(<来源>)`` 那个括注
    """

    name: str = ""
    character: str = ""
    keywords: list[str] = field(default_factory=list)
    description: str = ""
    source: str = ""
    confidence: float = 0.0
    reason: str = ""
    raw_name: str = ""
    inferred: bool = False
    wear: str = ""
    wear_source: str = ""

    @property
    def ok(self) -> bool:
        """是否有可注入的内容（关键词非空才算成功）。"""
        return bool(self.keywords)

    @property
    def is_wear(self) -> bool:
        """是不是「泛指的穿着」（1.0.28 的两条路之一）。"""
        return bool(self.wear)

    @property
    def is_wear_type(self) -> bool:
        """是不是命中**穿着类型表**（1.0.28 第 ② 档，零模型调用）。"""
        return self.wear == OUTFIT_WEAR_TYPE

    def keywords_text(self) -> str:
        """关键词合成一行英文（逗号分隔）。"""
        return ", ".join(self.keywords)

    def log_line(self) -> str:
        """可读日志行（成功与失败两种形状）。

        成功（用户点名了具体皮肤）：
        ``皮肤解析(模型): 粉色妖精小姐 → pink elf dress, fairy wings（角色=爱莉希雅，confidence=0.9）``
        成功（1.0.23 模型推断，用户没点名）：
        ``皮肤推断(模型): 流萤 → 白色花嫁 → white dress（来源=模型推断，confidence=0.8）``
        成功（1.0.28 命中穿着类型表，零模型调用）：
        ``穿着类型解析(内置表): 学生 → school uniform, pleated skirt``
        成功（1.0.28 穿着类候选交给模型）：
        ``穿着解析(模型): 雨衣 → raincoat（confidence=0.9）``（命中缓存写 ``穿着解析(缓存)``）
        失败：``皮肤解析(LLM)失败：<原因>``（推断路径 ``皮肤推断(LLM)失败：…``，
        穿着模型路径 ``穿着解析(LLM)失败：…``）

        「模型推断」与「穿着」都必须一眼可见：前者是**猜**出来的（用户没说具体是
        哪一套），后者是**泛指的穿着**（不是某一套皮肤）—— 与「表命中」「用户点名
        后解析」都是不同的可信度 / 语义，用户据此核对"到底注入了什么衣服"。
        """
        if not self.ok:
            if self.is_wear:
                return f"穿着解析(LLM)失败：{self.reason or '未知原因'}"
            kind = "皮肤推断" if self.inferred else "皮肤解析"
            return f"{kind}(LLM)失败：{self.reason or '未知原因'}"
        if self.is_wear_type:
            return (
                f"穿着类型解析({self.wear_source or WEAR_SOURCE_BUILTIN}): "
                f"{self.name or self.raw_name} → {self.keywords_text()}"
            )
        if self.is_wear:
            return (
                f"穿着解析({self.source or OUTFIT_SOURCE_MODEL}): "
                f"{self.raw_name or self.name} → {self.keywords_text()}"
                f"（confidence={self.confidence:g}）"
            )
        if self.inferred:
            return (
                f"皮肤推断({self.source or OUTFIT_SOURCE_MODEL}): "
                f"{self.character or '未指定'} → {self.name or '未命名'} → "
                f"{self.keywords_text()}"
                f"（来源={OUTFIT_SOURCE_INFER}，confidence={self.confidence:g}）"
            )
        character = self.character or "未指定"
        return (
            f"皮肤解析({self.source}): {self.raw_name or self.name} → "
            f"{self.keywords_text()}（角色={character}，confidence={self.confidence:g}）"
        )

    def table_line(self) -> str:
        """「建议加入皮肤表」那一行的建议内容（可直接粘贴进配置）。

        形如 ``粉色妖精小姐 | 爱莉希雅 | pink elf dress, fairy wings | <中文描述>``。
        """
        if not self.ok:
            return ""
        return (
            f"{self.name} | {self.character} | {self.keywords_text()} | "
            f"{self.description}"
        )


def _as_bool(value: Any) -> bool:
    """把 ``unknown`` 读成布尔（模型偶尔写字符串 ``"true"``）。"""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def parse_outfit_resolution_payload(
    payload: Any,
) -> tuple[ResolvedOutfit | None, str, float]:
    """校验并转换模型返回的皮肤解析 JSON（1.0.22）。

    期望结构::

        {"unknown": false, "outfit_cn": "粉色妖精小姐", "outfit_en": "Pink Elf Lady",
         "tag": "pink elf dress, fairy wings",
         "keywords": ["pink elf dress", "fairy wings"], "confidence": 0.9}

    1.0.23 起结构上多一个**可选**字段 ``"inferred": true``（用户没说皮肤名、由模型
    推断出一套时）：读取它只为让日志能标注「来源=模型推断」，校验规则一个字没变
    （缺字段 / 非布尔时按 ``false`` 处理，与 1.0.22 完全一致）。

    校验规则（任何一条不满足都返回**失败原因**，由调用方"什么都不注入"）：

    1. 顶层必须是对象；``unknown`` 为真 → ``模型表示不认识这套皮肤``；
    2. ``confidence`` 低于 :data:`OUTFIT_RESOLVE_MIN_CONFIDENCE` → 失败
       （缺字段 / 非数字按 1.0 处理：模型没给把握时不为难它）；
    3. ``keywords`` 逐项 + ``tag`` 按逗号切分后逐项过 :func:`validate_outfit_keyword`
       —— **非 ASCII / 大写 / 可疑字符的项一律丢弃**（不是整条判失败：
       模型给三条关键词、其中一条是中文时，剩下两条照样能用）；
    4. 过滤后既没有 tag 也没有 keywords → 失败（**绝不瞎编关键词**）。

    皮肤名依次取 ``outfit_cn`` → ``outfit_en``，都取不到时由调用方回落到用户原话。

    Returns:
        ``(结果或 None, 失败原因, confidence)``
    """
    if not isinstance(payload, dict):
        return None, "返回的不是 JSON 对象", 0.0

    if _as_bool(payload.get("unknown")):
        return None, "模型表示不认识这套皮肤（unknown=true）", 0.0

    confidence = _as_confidence(payload.get("confidence"))
    if confidence < OUTFIT_RESOLVE_MIN_CONFIDENCE:
        return (
            None,
            f"confidence={confidence:g} 低于阈值 {OUTFIT_RESOLVE_MIN_CONFIDENCE:g}",
            confidence,
        )

    raw_keywords = payload.get("keywords")
    pieces: list[str] = []
    if isinstance(raw_keywords, list):
        pieces.extend(str(item or "") for item in raw_keywords)
    elif isinstance(raw_keywords, str):
        pieces.append(raw_keywords)
    tag = str(payload.get("tag") or "")
    if tag.strip():
        pieces.append(tag)

    keywords: list[str] = []
    dropped = 0
    for piece in pieces:
        for word in re.split(r"[,，]", piece):
            word = word.strip()
            if not word:
                continue
            ok, reason = validate_outfit_keyword(word)
            if not ok:
                dropped += 1
                logger.debug(f"皮肤关键词被丢弃（{reason}）：{word!r}")
                continue
            if word not in keywords:
                keywords.append(word)
    if not keywords:
        suffix = f"（丢弃了 {dropped} 个非 ASCII / 非法关键词）" if dropped else ""
        return None, f"模型没给出任何可用的关键词{suffix}", confidence

    name = (
        str(payload.get("outfit_cn") or "").strip()
        or str(payload.get("outfit_en") or "").strip()
    )
    return (
        ResolvedOutfit(
            name=name,
            keywords=keywords,
            source=OUTFIT_SOURCE_MODEL,
            confidence=confidence,
            inferred=_as_bool(payload.get("inferred")),
        ),
        "",
        confidence,
    )


# ── 解析（LLM）：把皮肤名解析成英文关键词 ────────────────────────────

_OUTFIT_SYSTEM_PROMPT = """你是角色装扮（皮肤 / 时装 / 装扮）资料助手，服务于 NovelAI 生图提示词补全。

任务：用户会给你一个**皮肤 / 时装 / 装扮的名称**（可能是中文、日文罗马音、别称或玩家俗称），
以及它可能归属的角色名。请判断它指的是哪一套具体的皮肤 / 时装，并给出可以直接写进
NovelAI（Danbooru）提示词的**英文外观关键词**。

输出格式（**只输出一个 JSON 对象**，不要 markdown 代码块，不要任何解释文字）：
{"unknown": false, "outfit_cn": "皮肤中文名", "outfit_en": "皮肤英文名",
 "tag": "关键词1, 关键词2", "keywords": ["关键词1", "关键词2"], "confidence": 0.9}

字段说明：
- unknown：布尔值。**你不认识这套皮肤、或没有把握它长什么样时填 true**，
  其余字段留空即可 —— 宁可不答，也不要猜。
- outfit_cn / outfit_en：这套皮肤的中文名与英文名（没有通用英文名时留空）。
- tag：把关键词写成**一段逗号分隔的字符串**（与 keywords 等价，写一种即可）。
- keywords：描述这套装扮**外观**的英文关键词列表，例如服装款式 / 材质 / 颜色 /
  配饰 / 风格。硬性要求：**全部小写 ASCII**，只用小写字母、数字、空格、下划线、
  连字符、冒号与单引号，括号必须转义写成 `\\(` 与 `\\)`；
  不要写角色标签，也不要写画质词（masterpiece / best quality 之类）。
  正确示例：["pink elf dress", "fairy wings"]、["white wedding dress", "veil"]。
  错误示例（一律不要）：["粉色长裙"]（非 ASCII）、["Pink Elf Dress"]（大写）、
  ["pink dress (fairy)"]（括号未转义）。
- confidence：你对这次判断的把握，0 到 1 之间的小数。

硬性规则：
- 只输出 JSON，不要 markdown 代码块，不要前后缀说明；
- 不确定就 unknown=true —— 猜错会让下游把衣服画成另一套，比不答更糟；
- 只描述**外观**，不要编造出处作品、不要把角色名当关键词、不要输出中文关键词。"""

_OUTFIT_USER_TEMPLATE = """皮肤 / 时装名称：{name}
{character}{context}
请按要求输出 JSON。"""


#: 「没说名字的那一套」用的系统提示（1.0.23）：任务从「解析一个皮肤名」变成
#: 「给出这个角色最知名的一套」。与 1.0.22 的提示词**同源**（同一套字段与同一条
#: 「不确定就 unknown=true」的硬规则），只多一个 ``inferred: true`` 与更严的把握要求。
_OUTFIT_INFER_SYSTEM_PROMPT = """你是角色装扮（皮肤 / 时装 / 装扮）资料助手，服务于 NovelAI 生图提示词补全。

任务：用户**没有点名**具体是哪一套皮肤 / 时装，只说「<角色>的那一套皮肤 / 时装」。
请给出这个角色**最知名 / 最常被画**的那一套，并给出可以直接写进 NovelAI（Danbooru）
提示词的**英文外观关键词**。

输出格式（**只输出一个 JSON 对象**，不要 markdown 代码块，不要任何解释文字）：
{"unknown": false, "inferred": true, "outfit_cn": "皮肤中文名", "outfit_en": "皮肤英文名",
 "tag": "关键词1, 关键词2", "keywords": ["关键词1", "关键词2"], "confidence": 0.8}

字段说明：
- inferred：本次是**推断**（用户没点名），固定填 true。
- unknown：布尔值。**你没有把握说出这个角色最出名的那一套时填 true**，其余字段留空
  即可 —— 宁可不答，也不要猜；猜错会让下游把衣服画成另一套，比不答更糟。
- outfit_cn / outfit_en：这套皮肤的中文名与英文名（没有通用英文名时留空）。
- tag：把关键词写成**一段逗号分隔的字符串**（与 keywords 等价，写一种即可）。
- keywords：描述这套装扮**外观**的英文关键词列表，例如服装款式 / 材质 / 颜色 /
  配饰 / 风格。硬性要求：**全部小写 ASCII**，只用小写字母、数字、空格、下划线、
  连字符、冒号与单引号，括号必须转义写成 `\\(` 与 `\\)`；
  不要写角色标签，也不要写画质词（masterpiece / best quality 之类）。
  正确示例：["white wedding dress", "veil"]、["pink elf dress", "fairy wings"]。
  错误示例（一律不要）：["白色长裙"]（非 ASCII）、["White Dress"]（大写）、
  ["white dress (wedding)"]（括号未转义）。
- confidence：你对这次判断的把握，0 到 1 之间的小数；**没有把握就给低分**。

硬性规则：
- 只输出 JSON，不要 markdown 代码块，不要前后缀说明；
- 不要把角色名当关键词，不要输出角色标签（角色那一侧由别的逻辑负责）；
- 只描述**外观**，不要编造出处作品、不要输出中文关键词。"""

_OUTFIT_INFER_USER_TEMPLATE = """角色名：{character}
{context}用户**没有点名**具体是哪一套皮肤 / 时装，只说「{character} 的那一套皮肤 / 时装」。
请给出这个角色最知名 / 最常被画的那一套，并按要求输出 JSON。"""


async def resolve_outfit_via_llm(
    name: str,
    *,
    character: str = "",
    translate_model: str = "",
    timeout: float = 0.0,
    context_text: str = "",
) -> ResolvedOutfit:
    """用已配置的模型把皮肤名解析成英文关键词（1.0.22）。

    与 :func:`character.resolve_character_via_llm` **共用同一套** ``llm_api``、
    同一个模型（``general.translate_model``，留空回退 ``UTILS_SMALL``）与同一个
    超时（``general.llm_timeout``）；DeepSeek 的请求级兼容（关 thinking）也走
    ``character._prepare_model_set``，因此不引入任何新依赖。

    **本函数只做「纯解析」：不读缓存、不写缓存、也不打日志** —— 「这一次要不要问
    模型、命中缓存怎么打日志」属于调用方（``handler._resolve_outfit_with_cache``），
    否则回归用例（``tests/check_logic.py`` 的 ``FakeOutfit``）替换掉本函数时会把
    缓存一起替换掉（与 C75 同一条理由）。

    失败（``unknown=true`` / JSON 坏 / 关键词全被过滤 / confidence 低于阈值 /
    超时 / 任何异常）同样返回 :class:`ResolvedOutfit`，只是 ``ok`` 为假
    —— **本函数不抛异常**，调用方据此什么都不注入。

    Args:
        name: 用户消息里的皮肤名（原始写法）
        character: 归属角色线索（帮助模型确定是哪一套；可空）
        translate_model: 解析用模型名；空字符串回退 ``UTILS_SMALL``
        timeout: 超时秒数；``<=0`` 表示不限时
        context_text: 可选的用户原话（提供线索）

    Returns:
        :class:`ResolvedOutfit`（``ok`` 为假表示解析失败，``reason`` 写明原因）
    """
    raw_name = (name or "").strip()
    if not raw_name:
        return ResolvedOutfit(reason="皮肤名为空")
    raw_character = (character or "").strip()

    async def _run() -> str:
        request = llm_api.create_llm_request(
            model_set=_prepare_model_set(translate_model),
            request_name="char_prompt_guard_outfit",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_OUTFIT_SYSTEM_PROMPT)))
        context = (context_text or "").strip()
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(
                    _OUTFIT_USER_TEMPLATE.format(
                        name=raw_name,
                        character=(
                            f"归属角色（线索）：{raw_character}\n"
                            if raw_character
                            else ""
                        ),
                        context=(
                            f"用户原话（线索）：{context[:200]}\n" if context else ""
                        ),
                    )
                ),
            )
        )
        response = await request.send(stream=False)
        return await _resolve_response_text(response)

    try:
        if timeout and timeout > 0:
            raw_text = await asyncio.wait_for(_run(), timeout=timeout)
        else:
            raw_text = await _run()
    except TimeoutError:
        return ResolvedOutfit(
            name=raw_name,
            character=raw_character,
            reason=f"模型超时（{timeout:g}s）",
            raw_name=raw_name,
        )
    except Exception as e:  # noqa: BLE001 —— 解析失败只返回失败结果（放行生图）
        return ResolvedOutfit(
            name=raw_name,
            character=raw_character,
            reason=f"调用失败({type(e).__name__}: {e})",
            raw_name=raw_name,
        )

    try:
        payload = json.loads(_strip_json_object(raw_text))
    except Exception as e:  # noqa: BLE001 —— JSON 坏 = 解析失败
        return ResolvedOutfit(
            name=raw_name,
            character=raw_character,
            reason=f"JSON 解析失败({type(e).__name__}: {e})",
            raw_name=raw_name,
        )

    outfit, reason, confidence = parse_outfit_resolution_payload(payload)
    if outfit is None:
        return ResolvedOutfit(
            name=raw_name,
            character=raw_character,
            reason=reason,
            confidence=confidence,
            raw_name=raw_name,
        )
    # 皮肤名缺失时回落用户原话；归属角色取模型没给就用线索
    return replace(
        outfit,
        name=outfit.name or raw_name,
        character=outfit.character or raw_character,
        raw_name=raw_name,
    )


async def infer_outfit_via_llm(
    character: str,
    *,
    translate_model: str = "",
    timeout: float = 0.0,
    context_text: str = "",
) -> ResolvedOutfit:
    """用户没说皮肤名时，让模型推断该角色最知名的一套（1.0.23，``infer_unnamed``）。

    与 :func:`resolve_outfit_via_llm` **完全同一套**管道：同一个 ``llm_api``、
    同一个模型（``general.translate_model``，留空回退 ``UTILS_SMALL``）、同一个超时
    （``general.llm_timeout``）、同一套 JSON 提取与
    :func:`parse_outfit_resolution_payload` 校验（只收小写 ASCII 关键词；
    ``unknown=true`` / JSON 坏 / ``confidence`` 低于 :data:`OUTFIT_RESOLVE_MIN_CONFIDENCE` /
    无有效关键词 → **解析失败**）。差别只有两处：

    1. 提示词换成 :data:`_OUTFIT_INFER_SYSTEM_PROMPT`（要求模型给出该角色
       **最知名 / 最常被画**的一套，并在 JSON 里带上 ``"inferred": true``）；
    2. 返回结果的 ``inferred`` 一律为真 —— 这是**推断**，即使模型漏填那个字段，
       调用方也必须能按「模型推断」记录日志并据此**不写回记忆库**。

    与 :func:`resolve_outfit_via_llm` 一样：**本函数只做「纯解析」**（不读缓存、
    不写缓存、不打成功日志），缓存与日志都在调用方
    （``handler._resolve_outfit_with_cache`` 的 ``inferred=True`` 分支）。

    Args:
        character: 角色名（来自 :func:`find_unnamed_outfit_owner`，一定是角色表内角色）
        translate_model: 解析用模型名；空字符串回退 ``UTILS_SMALL``
        timeout: 超时秒数；``<=0`` 表示不限时
        context_text: 可选的用户原话（提供线索）

    Returns:
        :class:`ResolvedOutfit`（``ok`` 为假表示解析失败，``reason`` 写明原因）
    """
    raw_character = (character or "").strip()
    if not raw_character:
        return ResolvedOutfit(reason="角色名为空", inferred=True)

    def _failure(reason: str, confidence: float = 0.0) -> ResolvedOutfit:
        return ResolvedOutfit(
            character=raw_character,
            reason=reason,
            confidence=confidence,
            raw_name=raw_character,
            inferred=True,
        )

    async def _run() -> str:
        request = llm_api.create_llm_request(
            model_set=_prepare_model_set(translate_model),
            request_name="char_prompt_guard_outfit_infer",
        )
        request.add_payload(
            LLMPayload(ROLE.SYSTEM, Text(_OUTFIT_INFER_SYSTEM_PROMPT))
        )
        context = (context_text or "").strip()
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(
                    _OUTFIT_INFER_USER_TEMPLATE.format(
                        character=raw_character,
                        context=(
                            f"用户原话（线索）：{context[:200]}\n" if context else ""
                        ),
                    )
                ),
            )
        )
        response = await request.send(stream=False)
        return await _resolve_response_text(response)

    try:
        if timeout and timeout > 0:
            raw_text = await asyncio.wait_for(_run(), timeout=timeout)
        else:
            raw_text = await _run()
    except TimeoutError:
        return _failure(f"模型超时（{timeout:g}s）")
    except Exception as e:  # noqa: BLE001 —— 推断只多一条路，失败只返回失败结果
        return _failure(f"调用失败({type(e).__name__}: {e})")

    try:
        payload = json.loads(_strip_json_object(raw_text))
    except Exception as e:  # noqa: BLE001 —— JSON 坏 = 解析失败
        return _failure(f"JSON 解析失败({type(e).__name__}: {e})")

    outfit, reason, confidence = parse_outfit_resolution_payload(payload)
    if outfit is None:
        return _failure(reason, confidence)
    # 皮肤名缺失时回落「<角色>的那一套」；归属角色一律用调用方给的角色名
    return replace(
        outfit,
        name=outfit.name or f"{raw_character}的那一套",
        character=outfit.character or raw_character,
        raw_name=raw_character,
        inferred=True,
    )


# ── 解析（LLM）：把「穿着类候选」转成服装关键词（1.0.28）──────────────
#
# 需求：内置词表与皮肤表都**没有**的穿着词（「雨衣」「白色长裙」「换上战甲」），
# 也要自动用大模型找出提示词并注入 —— 这条是 1.0.28 的第 ④ 档，**只在候选
# "看起来是穿着"**（:func:`wear_like_candidate`）时走，且同样受
# ``outfits.auto_resolve`` 管；具体皮肤名仍然走 ③（:func:`resolve_outfit_via_llm`）。
#
# 提示词与皮肤那套**刻意分开**：任务从「这是哪一套皮肤」变成「把这次说的穿着
# 转成 nai_artist 能用的服装关键词」，并明确要求"**不是穿着就返回 unknown=true**"
# —— 两者的失败口径都是"什么都不注入"（绝不瞎编）。

_WEAR_SYSTEM_PROMPT = """你是服装 / 穿着资料助手，服务于 NovelAI 生图提示词补全。

任务：用户说了一个**泛指的穿着**（**不是**某个角色的皮肤 / 时装名），例如
「学生装」「居家服」「白色长裙」「雨衣」「换上战甲」。请把它转成可以直接写进
NovelAI（Danbooru）提示词的**英文服装关键词**。

输出格式（**只输出一个 JSON 对象**，不要 markdown 代码块，不要任何解释文字）：
{"unknown": false, "tag": "关键词1, 关键词2", "keywords": ["关键词1", "关键词2"], "confidence": 0.9}

字段说明：
- unknown：布尔值。**用户说的不是穿着、或你看不出这是什么衣服时填 true**，
  其余字段留空即可 —— 宁可不答，也不要猜。
- tag：把关键词写成**一段逗号分隔的字符串**（与 keywords 等价，写一种即可）。
- keywords：把这次说的穿着转成英文服装关键词（款式 / 材质 / 颜色 / 配饰），
  例如 学生装 → ["school uniform", "pleated skirt"]、
  居家服 → ["loungewear", "casual", "t-shirt"]、
  白色长裙 → ["white dress", "long skirt"]、雨衣 → ["raincoat", "hood"]。
  硬性要求：**全部小写 ASCII**，只用小写字母、数字、空格、下划线、连字符、
  冒号与单引号；**不要**写角色标签（形如 `\\(…\\)` 的括号一律不要）、
  不要写画质词（masterpiece / best quality 之类）。
  错误示例（一律不要）：["雨衣"]（非 ASCII）、["Raincoat"]（大写）、
  ["raincoat (yellow)"]（括号未转义）。
- confidence：你对这次判断的把握，0 到 1 之间的小数。

硬性规则：
- 只输出 JSON，不要 markdown 代码块，不要前后缀说明；
- 只描述**服装外观**，不要编造出处作品、不要把角色名当关键词、不要输出中文关键词；
- 不确定就 unknown=true —— 猜错会让下游把衣服画错，比不答更糟。"""

_WEAR_USER_TEMPLATE = """用户说的穿着：{name}
{context}请按要求输出 JSON。"""


async def resolve_wear_via_llm(
    name: str,
    *,
    translate_model: str = "",
    timeout: float = 0.0,
    context_text: str = "",
) -> ResolvedOutfit:
    """把「穿着类候选」交给模型转成服装关键词（1.0.28，第 ④ 档）。

    与 :func:`resolve_outfit_via_llm` / :func:`infer_outfit_via_llm` **共用同一套
    管道**：同一个 ``llm_api``、同一个模型（``general.translate_model``，留空回退
    ``UTILS_SMALL``）、同一个超时（``general.llm_timeout``）、同一套 JSON 提取与
    :func:`parse_outfit_resolution_payload` 校验（只收小写 ASCII 关键词；
    ``unknown=true`` / JSON 坏 / ``confidence`` 低于
    :data:`OUTFIT_RESOLVE_MIN_CONFIDENCE` / 无有效关键词 → **解析失败**）。
    差别只有提示词（:data:`_WEAR_SYSTEM_PROMPT`：**不是穿着就 unknown=true**）
    与返回结果的 :attr:`ResolvedOutfit.wear`（一律 :data:`OUTFIT_WEAR_MODEL`，
    于是日志前缀是 ``穿着解析(模型)`` / ``穿着解析(LLM)失败``）。

    与另外两个一样：**本函数只做「纯解析」**（不读缓存、不写缓存、不打日志），
    缓存与日志都在调用方（``handler._resolve_wear_with_cache``）—— 用例替换
    （打桩）的就是本函数，缓存写在它内部时会一起被替换掉（与 C75 同一条理由）。

    Args:
        name: 用户消息里的原词（原始写法，进日志与注入说明）
        translate_model: 解析用模型名；空字符串回退 ``UTILS_SMALL``
        timeout: 超时秒数；``<=0`` 表示不限时
        context_text: 可选的用户原话（提供线索）

    Returns:
        :class:`ResolvedOutfit`（``ok`` 为假表示解析失败，``reason`` 写明原因）
    """
    raw_name = (name or "").strip()
    if not raw_name:
        return ResolvedOutfit(reason="穿着词为空", wear=OUTFIT_WEAR_MODEL)

    def _failure(reason: str, confidence: float = 0.0) -> ResolvedOutfit:
        return ResolvedOutfit(
            name=raw_name,
            reason=reason,
            confidence=confidence,
            raw_name=raw_name,
            wear=OUTFIT_WEAR_MODEL,
        )

    async def _run() -> str:
        request = llm_api.create_llm_request(
            model_set=_prepare_model_set(translate_model),
            request_name="char_prompt_guard_wear",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_WEAR_SYSTEM_PROMPT)))
        context = (context_text or "").strip()
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(
                    _WEAR_USER_TEMPLATE.format(
                        name=raw_name,
                        context=(
                            f"用户原话（线索）：{context[:200]}\n" if context else ""
                        ),
                    )
                ),
            )
        )
        response = await request.send(stream=False)
        return await _resolve_response_text(response)

    try:
        if timeout and timeout > 0:
            raw_text = await asyncio.wait_for(_run(), timeout=timeout)
        else:
            raw_text = await _run()
    except TimeoutError:
        return _failure(f"模型超时（{timeout:g}s）")
    except Exception as e:  # noqa: BLE001 —— 解析失败只返回失败结果（放行生图）
        return _failure(f"调用失败({type(e).__name__}: {e})")

    try:
        payload = json.loads(_strip_json_object(raw_text))
    except Exception as e:  # noqa: BLE001 —— JSON 坏 = 解析失败
        return _failure(f"JSON 解析失败({type(e).__name__}: {e})")

    outfit, reason, confidence = parse_outfit_resolution_payload(payload)
    if outfit is None:
        return _failure(reason, confidence)
    # 穿着这条路**不用**模型给的中文 / 英文名：注入说明里写用户的原词
    # （`（穿着：“雨衣”：raincoat）`），用户对着日志与画面就能核对。
    return replace(
        outfit,
        name=raw_name,
        character="",
        raw_name=raw_name,
        inferred=False,
        wear=OUTFIT_WEAR_MODEL,
        wear_source="",
    )


# ── 解析结果的进程内缓存（存储与 TTL 在本模块，读写在调用方）──────────

#: 成功结果的缓存有效期（秒）：24 小时。皮肤外观是**静态知识**，同一天里没必要
#: 反复问模型；重启进程即清空。
OUTFIT_CACHE_TTL_SECONDS = 24 * 60 * 60.0

#: 解析失败的**负缓存**有效期（秒）：比成功短得多。
#: 超时 / 网络抖动是暂时的，不该让同一套皮肤 24 小时内都不再尝试。
OUTFIT_FAILURE_TTL_SECONDS = 10 * 60.0

#: 缓存条数上限（超出即整表清空，避免无上限增长）
OUTFIT_CACHE_LIMIT = 128

#: 1.0.23：「没说名字的那一套」的缓存键后缀 —— ``<角色名>|__infer__``。
#:
#: 为什么与皮肤名共用一张表：TTL 语义完全一样（皮肤外观是静态知识），
#: 分开存只会多一套要维护的过期逻辑；后缀保证它与「皮肤名|角色名」两类键
#: **永远不会互相覆盖**（「流萤」这个角色名不会与任何一套皮肤名撞键）。
OUTFIT_INFER_CACHE_SUFFIX = "__infer__"

#: 1.0.28：「穿着类候选交给模型」的缓存键后缀 —— ``<原词>|__wear__``。
#:
#: 与 :data:`OUTFIT_INFER_CACHE_SUFFIX` 同一个理由（共用一张缓存表、同一套 TTL，
#: 后缀保证三类键互不覆盖）；但要与「皮肤名 + 角色名」分开：同一个词
#: （例如「学生装」）在穿着这条路上是按**原词**缓存的，不掺角色。
OUTFIT_WEAR_CACHE_SUFFIX = "__wear__"

#: 缓存：``(皮肤名 + 角色名)`` 归一化键 → ``(写入时间, 结果)``
_outfit_cache: dict[str, tuple[float, ResolvedOutfit]] = {}


def outfit_cache_key(name: str, character: str = "") -> str:
    """缓存键：``皮肤名 + 角色名``（走 :func:`character.normalize_prompt_text`）。

    为什么要带角色名：同一套衣服名（「婚纱」「女仆装」）落在不同角色身上就是
    不同的外观关键词，只按皮肤名缓存会让第二个角色直接吃到第一个角色的答案。
    """
    skin = normalize_prompt_text(name) or (name or "").strip().lower()
    owner = normalize_prompt_text(character)
    return f"{skin}|{owner}"


def outfit_infer_cache_key(character: str) -> str:
    """「没说名字的那一套」的缓存键：``<角色名>|__infer__``（1.0.23）。

    这里**不用** :func:`outfit_cache_key` 拼（它会把下划线归一化成空格，
    于是后缀变成 ``infer``）：键的形状是给用户看日志 / 排查用的，
    固定成 ``<角色名>|__infer__`` 更好认；角色名那一半的归一化口径与
    :func:`outfit_cache_key` 保持一致。
    """
    owner = normalize_prompt_text(character) or (character or "").strip().lower()
    return f"{owner}|{OUTFIT_INFER_CACHE_SUFFIX}"


def outfit_wear_cache_key(name: str) -> str:
    """「穿着类候选交给模型」的缓存键：``<原词>|__wear__``（1.0.28）。

    与 :func:`outfit_infer_cache_key` 同一套写法（归一化口径一致，但后缀固定成
    下划线包起来的 ``__wear__``，好认也便于排查）。穿着关键词是**通用服装词**
    （「雨衣 → raincoat」与角色无关），因此键里只有原词、不带角色。
    """
    key = normalize_prompt_text(name) or (name or "").strip().lower()
    return f"{key}|{OUTFIT_WEAR_CACHE_SUFFIX}"


def outfit_cache_get(
    name: str,
    character: str = "",
    *,
    now: float | None = None,
    key: str = "",
) -> ResolvedOutfit | None:
    """按（皮肤名 + 角色名）读缓存；未命中或已过期返回 ``None``（1.0.22）。

    成功结果缓存 24 小时（:data:`OUTFIT_CACHE_TTL_SECONDS`）、失败负缓存 10 分钟
    （:data:`OUTFIT_FAILURE_TTL_SECONDS`）。**命中时把 ``source`` 改写成** ``缓存``
    再返回：调用方直接打 ``result.log_line()``，日志里就是 ``皮肤解析(缓存): …``。

    ``key`` 不为空时**直接用它当缓存键**（1.0.23 的「皮肤推断」用
    :func:`outfit_infer_cache_key` 拼出来的 ``<角色名>|__infer__``）——
    只有调用方知道该用哪种键，TTL 与过期逻辑仍共用这一处。
    """
    cache_key = key or outfit_cache_key(name, character)
    stamp_and_result = _outfit_cache.get(cache_key)
    if stamp_and_result is None:
        return None
    stamp, result = stamp_and_result
    ttl = OUTFIT_CACHE_TTL_SECONDS if result.ok else OUTFIT_FAILURE_TTL_SECONDS
    if ttl <= 0 or ((time.time() if now is None else float(now)) - stamp) >= ttl:
        _outfit_cache.pop(cache_key, None)
        return None
    return replace(
        result,
        source=OUTFIT_SOURCE_CACHE,
        raw_name=result.raw_name or name,
        character=result.character or character,
    )


def outfit_cache_put(
    name: str,
    result: ResolvedOutfit,
    *,
    character: str = "",
    now: float | None = None,
    key: str = "",
) -> None:
    """按（皮肤名 + 角色名）写缓存（成功 / 失败都写；超上限时整表清空）。

    ``key`` 的语义与 :func:`outfit_cache_get` 完全一致（读写必须用同一个键，
    否则会写出「读不回来」的缓存）。
    """
    if len(_outfit_cache) >= OUTFIT_CACHE_LIMIT:
        _outfit_cache.clear()
    stamp = time.time() if now is None else float(now)
    cache_key = key or outfit_cache_key(name, character)
    _outfit_cache[cache_key] = (stamp, result)


def reset_outfit_cache() -> None:
    """清空皮肤解析结果的进程内缓存（**用例用**；运行期不需要手动调用）。

    ``tests/check_logic.py`` 的 ``FakeOutfit`` 进出 ``with`` 块各清一次，
    免得一条用例的解析结果把下一条的「该不该问模型」判断带偏。
    """
    _outfit_cache.clear()


# ── 写回记忆库（沿用 memory.py 现有写通道）────────────────────────────

#: 写回记忆时用的 core_tag 首项（定义在 :mod:`memory`，与「角色指向」并列，
#: 方便在记忆库里把两类结论分开检索）
OUTFIT_CORE_TAG = memory_api.OUTFIT_CORE_TAG


def outfit_write_title(outfit: ResolvedOutfit) -> str:
    """写回记忆的标题：``皮肤指向：粉色妖精小姐 → 爱莉希雅``。"""
    owner = outfit.character or OUTFIT_UNKNOWN_CHARACTER
    return f"{OUTFIT_CORE_TAG}：{outfit.name} → {owner}"


async def write_outfit_to_memory(config: Any, outfit: ResolvedOutfit) -> None:
    """把一次成功的皮肤解析写回记忆库（1.0.22）；失败只记日志。

    走 :meth:`memory.MemoryClient.write`（**现有写回通道** → ``POST /api/memories``，
    ``core_tags`` / ``diffusion_tags`` / ``opposing_tags`` 三元组同时非空），
    标题 ``皮肤指向：<皮肤名> → <角色名>``、``core_tags`` 首项 ``皮肤指向``。

    两道开关**同时**为真才写：``outfits.write_back``（这一维自己的开关）与
    ``memory.write_back``（"要不要往记忆库里写东西"的总开关）。
    记忆库不可用 / 地址非法 / 写入失败：只记 DEBUG，**绝不影响生图**。
    """
    if not outfit.ok:
        return
    if not memory_api.memory_enabled(config):
        return
    outfits_section = getattr(config, "outfits", None)
    if getattr(outfits_section, "write_back", True) is False:
        return
    memory_section = getattr(config, "memory", None)
    if getattr(memory_section, "write_back", True) is False:
        return
    try:
        client = memory_api.build_client(config)
        if client.invalid_base_url:
            return
        memory_id = await client.write(
            trigger_word=outfit.name,
            character=memory_api.MemoryCharacter(
                name=outfit.character or OUTFIT_UNKNOWN_CHARACTER,
                aliases=(),
                tag=outfit.keywords_text(),
            ),
            scenario=f"皮肤/时装解析：{outfit.name}",
            title=outfit_write_title(outfit),
            core_tag=OUTFIT_CORE_TAG,
        )
        if memory_id:
            logger.debug(f"皮肤解析结果已写回记忆库：{outfit.name}（id={memory_id}）")
    except Exception as e:  # noqa: BLE001 —— 写回失败只记日志，绝不影响生图
        logger.debug(f"皮肤解析结果写回记忆库失败（已忽略）: {type(e).__name__}: {e}")


__all__ = [
    "OUTFIT_BLACKLIST",
    "OUTFIT_CACHE_LIMIT",
    "OUTFIT_CACHE_TTL_SECONDS",
    "OUTFIT_CORE_TAG",
    "OUTFIT_FAILURE_TTL_SECONDS",
    "OUTFIT_GENERIC_WORDS",
    "OUTFIT_INFER_CACHE_SUFFIX",
    "OUTFIT_KEYWORD_ALLOWED_CHARS",
    "OUTFIT_KEYWORD_MAX_CHARS",
    "OUTFIT_NAME_MAX_LEN",
    "OUTFIT_NAME_MIN_LEN",
    "OUTFIT_RESOLVE_MIN_CONFIDENCE",
    "OUTFIT_SOURCE_CACHE",
    "OUTFIT_SOURCE_INFER",
    "OUTFIT_SOURCE_MODEL",
    "OUTFIT_SOURCE_TABLE",
    "OUTFIT_UNKNOWN_CHARACTER",
    "OUTFIT_WEAR_CACHE_SUFFIX",
    "OUTFIT_WEAR_MODEL",
    "OUTFIT_WEAR_TYPE",
    "WEAR_LOOK_CHARS",
    "WEAR_SOURCE_BUILTIN",
    "WEAR_SOURCE_CONFIG",
    "WEAR_TYPE_KEYWORDS",
    "OutfitEntry",
    "ResolvedOutfit",
    "WearTypeMatch",
    "extract_outfit_names",
    "find_outfit_entry",
    "find_unnamed_outfit_owner",
    "find_wear_type",
    "infer_outfit_via_llm",
    "looks_like_wear",
    "looks_like_wear_sentence",
    "outfit_cache_get",
    "outfit_cache_key",
    "outfit_cache_put",
    "outfit_infer_cache_key",
    "outfit_wear_cache_key",
    "outfit_write_title",
    "parse_outfit_resolution_payload",
    "parse_outfits",
    "parse_wear_types",
    "reset_outfit_cache",
    "resolve_outfit_via_llm",
    "resolve_wear_via_llm",
    "split_outfit_keywords",
    "validate_outfit_keyword",
    "validate_outfit_tag",
    "wear_like_candidate",
    "wear_type_table",
    "write_outfit_to_memory",
]
