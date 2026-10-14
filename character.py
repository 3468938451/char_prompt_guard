"""角色识别与提取：角色表解析、文本匹配、LLM 提取。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ROLE, LLMPayload, TaskType, Text
from src.kernel.llm import ModelSet

logger = get_logger("char_prompt_guard")


@dataclass
class CharacterEntry:
    """角色表条目。"""

    name: str
    game: str = ""
    aliases: list[str] = field(default_factory=list)
    tag: str = ""

    def keywords(self) -> list[str]:
        """返回用于匹配的关键词列表（角色名 + 别名，去重）。"""
        seen: set[str] = set()
        keywords: list[str] = []
        for keyword in [self.name, *self.aliases]:
            keyword = keyword.strip()
            if not keyword:
                continue
            key = keyword.lower()
            if key in seen:
                continue
            seen.add(key)
            keywords.append(keyword)
        return keywords

    def resolved_tag(self) -> str:
        """返回可写入 NAI 提示词的角色标签。

        只使用角色表显式声明的标签：中文作品名机翻成英文极易产出错误标签，
        因此宁缺毋滥，未声明时返回空字符串（调用方应跳过注入）。
        """
        return self.tag.strip()


def iter_config_entries(raw_text: str) -> list[str]:
    """将「列表型」配置文本切分为条目，兼容多种书写方式。

    角色表、目标 Action 表等配置项共用此逻辑，支持：

    - 多行：每行一个条目；
    - 单行分号分隔：``甲 | 乙 ; 丙 | 丁``；
    - 字面量 ``\\n``（例如从 JSON 复制粘贴进来）。

    注释规则：

    - **整行**以 ``#`` 开头：整行忽略（行内的分号不参与切分，
      因此注释里可以自由书写分隔符说明）；
    - **行尾**注释：``空白 + #`` 之后的内容会被丢弃，例如
      ``爱莉希雅 | 崩坏三 | elysia  # 主力角色``。只有在 ``#``
      前面有空白时才当注释，避免误伤取值里自带的 ``#``。
    """
    normalized = (
        (raw_text or "").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r", "")
    )
    entries: list[str] = []
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 行尾注释：只在「空白 + #」处切分，注释一直吃到行尾
        line = re.split(r"\s+#", line, maxsplit=1)[0]
        for segment in re.split(r"[;；]", line):
            if segment.strip():
                entries.append(segment)
    return entries


def parse_roster(raw_text: str) -> list[CharacterEntry]:
    """解析角色表文本。

    每个条目一个角色：``角色名 | 游戏名 | 别名1,别名2 | 角色标签``。
    游戏名、别名与角色标签均可省略；条目之间可用换行或分号分隔；
    整行以 ``#`` 开头则为注释，整行忽略。

    角色标签用于「标签模式」的生图插件（提示词本身就是 NAI tag），
    例如 ``elysia \\(honkai impact 3rd\\)``。
    """
    entries: list[CharacterEntry] = []
    for line in iter_config_entries(raw_text):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 兼容中文输入法打出的全角分隔符
        line = line.replace("｜", "|")
        parts = [part.strip() for part in line.split("|")]
        name = parts[0] if parts else ""
        if not name:
            continue
        game = parts[1] if len(parts) > 1 else ""
        aliases: list[str] = []
        if len(parts) > 2:
            aliases = [
                alias.strip()
                for alias in parts[2].replace("，", ",").split(",")
                if alias.strip()
            ]
        entries.append(
            CharacterEntry(
                name=name,
                game=game,
                aliases=aliases,
                tag=parts[3] if len(parts) > 3 else "",
            )
        )
    return entries


def normalize_prompt_text(text: str) -> str:
    """归一化提示词文本，便于比较角色标签是否已存在。

    去掉 NAI 转义反斜杠与下划线，统一小写并压缩空白，使
    ``elysia \\(honkai impact 3rd\\)`` 与 ``elysia (Honkai Impact 3rd)``
    被视为同一个标签。
    """
    lowered = (text or "").lower().replace("\\", "").replace("_", " ")
    return re.sub(r"\s+", " ", lowered).strip()


def content_contains_tag(content: str, tag: str) -> bool:
    """判断提示词中是否已包含某个角色标签（宽松比较）。"""
    normalized_tag = normalize_prompt_text(tag)
    if not normalized_tag:
        return False
    return normalized_tag in normalize_prompt_text(content)


def _keyword_hits(text: str, keyword: str) -> bool:
    """检查关键词是否命中文本。

    纯 ASCII 关键词使用单词边界匹配，避免 "rem" 误命中 "remember"；
    非 ASCII（中文/日文等）关键词直接做子串匹配。
    """
    if not keyword or not text:
        return False
    lowered = keyword.lower()
    if keyword.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None
    return lowered in text.lower()


def entry_key(entry: CharacterEntry) -> str:
    """返回用于集合去重的角色身份键。"""
    key = normalize_prompt_text(entry.tag) or normalize_prompt_text(entry.name)
    return key or entry.name.strip()


def character_in_texts(entry: CharacterEntry, texts: list[str]) -> bool:
    """判断角色是否已经出现在给定文本中（角色名/别名/标准标签任一中即算）。"""
    for text in texts:
        if not text:
            continue
        for keyword in entry.keywords():
            if _keyword_hits(text, keyword):
                return True
        tag = entry.resolved_tag()
        if tag and content_contains_tag(text, tag):
            return True
    return False


def find_characters_in_text(
    text: str, roster: list[CharacterEntry]
) -> list[CharacterEntry]:
    """在文本中查找所有命中的角色（按角色表顺序，去重）。"""
    if not text:
        return []
    hits: list[CharacterEntry] = []
    seen: set[str] = set()
    for entry in roster:
        for keyword in entry.keywords():
            if not _keyword_hits(text, keyword):
                continue
            key = entry_key(entry)
            if key not in seen:
                seen.add(key)
                hits.append(entry)
            break
    return hits


def find_roster_identity(
    name: str, roster: list[CharacterEntry]
) -> CharacterEntry | None:
    """按「角色名 → 别名 → 标签」在角色表里找与 ``name`` 相同的角色（1.0.13）。

    用于「本尊身份」定位：``general.true_self`` 或框架人格配置的 nickname 填的是
    角色表里的名字（也可以是别名或标准标签）。比较走
    :func:`normalize_prompt_text`（忽略大小写、反斜杠与下划线），因此
    ``hanabi \\(honkai: star rail\\)`` 与 ``hanabi (Honkai: Star rail)`` 视为同一个。

    三个维度分三轮比对：**角色名**优先于**别名**、别名优先于**标签** ——
    否则「A 的别名恰好等于 B 的名字」时，会随机命中角色表里靠前的那个 A。

    **只做同一性匹配，不做任何猜测**：对不上就返回 ``None``，由调用方走「不补」。

    Returns:
        命中的角色表条目；没命中返回 ``None``
    """
    target = normalize_prompt_text(name)
    if not target or not roster:
        return None
    for entry in roster:
        if normalize_prompt_text(entry.name) == target:
            return entry
    for entry in roster:
        if any(normalize_prompt_text(alias) == target for alias in entry.aliases):
            return entry
    for entry in roster:
        if normalize_prompt_text(entry.resolved_tag()) == target:
            return entry
    return None


def find_characters_in_history(
    messages: list[dict[str, Any]],
    roster: list[CharacterEntry],
    limit: int = 0,
) -> list[CharacterEntry]:
    """扫描消息，返回被提及过的角色（按最近一次提及时间倒序，去重）。

    Args:
        messages: 消息字典列表（按时间升序）
        roster: 角色表
        limit: 最多返回多少个；0 表示不限制

    Returns:
        角色列表，最近提及的排在前面
    """
    last_index: dict[str, int] = {}
    by_key: dict[str, CharacterEntry] = {}
    for index, message in enumerate(messages):
        # 走 message_text：引用/媒体块里的文字不算「提及过这个角色」
        # （否则引用块会把 multi_only 的「多人场景」判断也带偏）
        text = message_text(message)
        if not text:
            continue
        for entry in find_characters_in_text(text, roster):
            key = entry_key(entry)
            last_index[key] = index
            by_key[key] = entry

    ordered = sorted(last_index.items(), key=lambda item: item[1], reverse=True)
    result = [by_key[key] for key, _ in ordered]
    return result[:limit] if limit > 0 else result


# ── 「这次该补谁」的确定性判定（用户点名 / 主角粘性）──────────────────

#: 「追加」语义词：本条之外还要再加上「上一条点名消息」里的角色。
#: 用户原话例如「画爱莉希雅」→「还有伊甸」「一起画」「加上她」「and eden」。
ADDITIVE_WORDS: tuple[str, ...] = (
    "还有",
    "一起",
    "俩",
    "都",
    "加上",
    "和她",
    "and",
    "both",
)

#: 「改口」语义词：只取本条点名的角色，上一条点名的作废。
#: 用户原话例如「算了」「还是画伊甸吧」「不是她」「换成伊甸」。
#: 注意（1.0.12 起）：``还是`` **必须判语境**才算改口，见
#: :func:`contains_corrective_word` —— 选择疑问句「是 A 还是 B？」不是改口。
CORRECTIVE_WORDS: tuple[str, ...] = (
    "算了",
    "还是",
    "不是",
    "换",
    "改成",
)

#: 「还是」的改口语境前置词：在「还是」**前面 20 个字符**内出现这些词，
#: 说明用户在否定上一个方案（「算了，还是画伊甸吧」「不要爱莉了，还是画伊甸」）。
#:
#: ``别`` 额外排除了「特别 / 区别 / 告别 / 个别」这类复合词（负向后顾）：
#: 「我特别想知道是爱莉还是你」里的「别」不是改口信号，实测这类句子会被误判
#: 成改口，与下面要修的事故同源。
_CORRECTIVE_LEAD_PATTERN = re.compile(
    r"算了|不要|不画|不拍|改|换"
    r"|(?<![特分区个级类派差告性鉴离作干话诀永惜送久])别"
)

#: 「还是」的改口语境句式：``还是(画|要|来|看|拍|换)…吧``，用户在重新指定对象。
_CORRECTIVE_HAISHI_PATTERN = re.compile(
    r"还是\s*(?:画|要|来|看|拍|换)[^。！？!?，,；;：:\n]{0,12}?吧"
)

#: 「还是」前置语境的观察窗口（字符数）
_CORRECTIVE_LEAD_WINDOW = 20

#: 自我指代词：用户要的是「bot 自己 / 本尊」，而不是角色表里的某个角色。
#:
#: 「本尊是谁」只存在于调用方（聊天器）的内部设定里，插件看不到；硬按「主角粘性」
#: 沿用旧点名会补错人 —— 实测事故：用户说「那你再来图，要你自己～」，画面描述其实
#: 已经写对了外观，插件却把一小时前点名的「爱莉希雅」补了进去。
#:
#: 词表只收「明确指向 bot 自身」的说法，**不含「你」**（太泛：画「你」也可能指
#: 角色表里的某个角色，收进来会把正常的「想看看你～」也变成不补）。
SELF_REFERENCE_WORDS: tuple[str, ...] = (
    "本尊",
    "你本人",
    "你自己",
    "真正的你",
    "真身",
    "脱壳",
    "摘壳",
    "卸壳",
    "本体",
)

#: 判定依据文案（会原样出现在 DEBUG 日志里，便于用户核对）
BASIS_USER_NAMED = "用户点名"
BASIS_LATEST_MENTION = "最近提及"
BASIS_NONE = "无"

#: 消息被明确标成 bot/助手时的角色取值（框架自带的 history 字典里**没有**
#: ``is_bot`` / ``role`` 字段，只有 ``sender_id`` / ``sender_name``；真实的
#: bot 判定要靠 ``adapter_api.get_bot_info_by_platform`` 拿 bot_id 比对，
#: 见 ``handler._resolve_bot_sender_ids``。识别不到时一律按「用户消息」处理，
#: 宁可多扫一条，也不要让整条判定链路静默失效）
_BOT_ROLE_VALUES = frozenset({"bot", "assistant", "model", "system", "ai"})

#: 框架自带的 bot 发送者标记：``messages.person_id`` 对 bot 消息写的就是 ``bot``，
#: 而适配器解析出的 bot_id 往往是 QQ 号之类，两者格式不同 —— 只比 bot_id 会漏判。
_BOT_SENDER_MARKERS = frozenset(
    {"bot", "system", "self", "assistant", "机器人", "系统"}
)

#: 明确标成用户/成员时的角色取值
_USER_ROLE_VALUES = frozenset({"user", "human", "member", "customer", "friend"})


def message_text(message: dict[str, Any]) -> str:
    """取出消息的可读正文（与框架 ``_rows_to_message_dicts`` 的字段一致）。

    1.0.12 起**先剥掉中括号块**（``[回复…]`` / ``[图片(…)]`` / ``[视频(…)]`` /
    ``[语音(…)]`` / ``[表情(…)]`` …）再返回：这些块里的文字是别人说的话或媒体
    占位，不能当成用户本次的点名，详见 :func:`strip_quoted_blocks`。
    """
    return strip_quoted_blocks(
        str(message.get("processed_plain_text") or message.get("content") or "")
    )


def message_time(message: dict[str, Any]) -> float | None:
    """取出消息时间戳；取不到或是非法值时返回 ``None``。"""
    try:
        value = float(message.get("time"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def is_known_bot_message(
    message: dict[str, Any], bot_ids: frozenset[str] | set[str] = frozenset()
) -> bool:
    """判断消息是否**可以确定**来自 bot 自己。

    bot 的复述（「好的，画爱莉希雅～」）不能算用户点名，否则用户上一句刚改口、
    bot 复述一句旧角色，点名信号就被顶掉了。判定依据依次是：

    1. ``is_bot is True``；
    2. ``role`` / ``sender_role`` / ``message_role`` 是 bot / assistant 之类；
    3. ``sender_id`` 命中 ``bot_ids``（由 ``adapter_api`` 解析出的本机 bot id）。
    """
    if message.get("is_bot") is True:
        return True
    for key in ("role", "sender_role", "message_role"):
        if str(message.get(key) or "").strip().lower() in _BOT_ROLE_VALUES:
            return True
    for key in ("sender_id", "person_id", "sender_name", "user_name"):
        if str(message.get(key) or "").strip().lower() in _BOT_SENDER_MARKERS:
            return True
    sender_id = str(message.get("sender_id") or "").strip()
    return bool(sender_id and bot_ids and sender_id in bot_ids)


def is_attributed_user_message(
    message: dict[str, Any], bot_ids: frozenset[str] | set[str] = frozenset()
) -> bool:
    """判断消息是否**可以确定**来自用户。

    只用于区分日志里的「判定依据=用户点名」与「判定依据=最近提及」：
    能确定发送者身份时叫用户点名，只能确认「不是 bot」时退一步叫最近提及。
    """
    if message.get("is_bot") is False:
        return True
    for key in ("role", "sender_role", "message_role"):
        if str(message.get(key) or "").strip().lower() in _USER_ROLE_VALUES:
            return True
    if is_known_bot_message(message, bot_ids):
        # 框架写 person_id="bot" 的消息绝不能算用户点名（实测就是这么补错的）
        return False
    sender_id = str(message.get("sender_id") or "").strip()
    if sender_id.lower() in _BOT_SENDER_MARKERS:
        return False
    return bool(sender_id and bot_ids and sender_id not in bot_ids)


def order_messages_newest_first(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把消息窗口整理成「从新到旧」。

    框架侧的顺序是确定的：``get_recent_messages(limit_mode="latest")`` 先按
    ``-time`` 取最近 N 条，再在 ``_query_messages`` 里把结果重新按 ``time``
    升序排序，所以窗口是「旧 → 新」，**最后一条最新**（``handler._collect_context``
    也是把触发消息 append 在末尾）。

    即便如此这里还是做一次显式校验：只有确认时间戳单调不减时才直接倒序；
    时间戳乱序时按时间排序兜底，避免把最旧的消息当成「最近」。
    """
    if not messages:
        return []
    stamps = [message_time(item) for item in messages]
    known = [stamp for stamp in stamps if stamp is not None]
    if len(known) == len(stamps) and len(known) >= 2:
        ascending = all(
            stamps[index] <= stamps[index + 1]  # type: ignore[operator]
            for index in range(len(stamps) - 1)
        )
        if ascending:
            return list(reversed(messages))
        indexed = sorted(enumerate(messages), key=lambda item: stamps[item[0]] or 0.0)
        return [message for _, message in reversed(indexed)]
    if len(known) >= 2:
        # 部分消息没有时间戳：按「有时间戳的排后面（更新）」的稳定顺序兜底
        indexed = sorted(
            enumerate(messages),
            key=lambda item: (
                stamps[item[0]] is not None,
                stamps[item[0]] or 0.0,
                item[0],
            ),
        )
        return [message for _, message in reversed(indexed)]
    # 完全没有可用时间戳：按框架约定（升序）倒序
    return list(reversed(messages))


def contains_any_word(text: str, words: tuple[str, ...]) -> bool:
    """判断文本里是否出现给定词表中的任意一个词。

    纯 ASCII 词（``and`` / ``both``）用单词边界匹配，避免 ``and`` 命中
    ``candy``；中文词直接子串匹配。
    """
    return any(_keyword_hits(text, word) for word in words)


def _haishi_is_corrective(text: str) -> bool:
    """判断文本里的「还是」是否处于**改口语境**。

    只有下面两种语境才算改口，其余（尤其是「是 A 还是 B？」这种选择疑问句）不算：

    1. 「还是」前面 :data:`_CORRECTIVE_LEAD_WINDOW` 个字符内出现
       ``算了 / 不要 / 别 / 不画 / 不拍 / 改 / 换``（「算了，还是画伊甸吧」）；
    2. 「还是」后面形如 ``还是(画|要|来|看|拍|换)…吧``（「还是画伊甸吧」）。
    """
    for match in re.finditer("还是", text):
        head = text[max(0, match.start() - _CORRECTIVE_LEAD_WINDOW) : match.start()]
        if _CORRECTIVE_LEAD_PATTERN.search(head):
            return True
        if _CORRECTIVE_HAISHI_PATTERN.match(text, match.start()):
            return True
    return False


def contains_corrective_word(text: str) -> bool:
    """判断文本里是否出现「改口」语义（只取本条点名的角色，上一条作废）。

    1.0.11 及以前只要出现「还是」就算改口，于是选择疑问句被判成了改口：
    实测事故原文 ``[回复<某人>：那你告诉我，是爱莉还是你？][图片(xxx)]`` 里，
    引用块中的「还是」把 LLM 正确认出的「花火」顶掉，最后补成了「爱莉希雅」。

    1.0.12 起的口径：

    - ``算了 / 不是 / 换 / 改成`` 行为**完全不变**（出现即算改口）；
    - ``还是`` **必须判语境**（见 :func:`_haishi_is_corrective`）；不构成改口语境时
      本条判定退回「不确定 → 交 LLM 仲裁」，而不是强行认定用户改了口。
    """
    if not text:
        return False
    others = tuple(word for word in CORRECTIVE_WORDS if word != "还是")
    if contains_any_word(text, others):
        return True
    return _haishi_is_corrective(text)


def find_self_reference_word(text: str) -> str:
    """返回文本里命中的第一个自我指代词；没有命中返回空串。

    见 :data:`SELF_REFERENCE_WORDS`：用户要的是「bot 本尊」时，插件不该按粘性
    沿用旧点名（「本尊是谁」只有调用方知道）。
    """
    if not text:
        return ""
    for word in SELF_REFERENCE_WORDS:
        if word in text:
            return word
    return ""


def strip_quoted_blocks(text: str) -> str:
    """剥掉 ``[回复…]`` / ``[图片(…)]`` 这类中括号块（嵌套时按**最外层**整块剥掉）。

    为什么要在「点名识别」之前剥掉：框架与适配器会把**引用**与**媒体占位**渲染成
    中括号块，块里的文字要么是**别人说的话**，要么只是文件名 —— 它们不是本次用户
    自己的点名。实测事故（1.0.12 要修的）：用户消息

        [回复<的饮水思源(2224586893)>：那你告诉我，是爱莉还是你？][图片(xxx)]

    里，引用块中的「还是」被当成用户的改口词，把 LLM 正确认出的「花火」顶掉，
    最终补成了引用块里提到的「爱莉希雅」。

    规则：

    - 半角 ``[`` 到配对的 ``]`` 整块剥掉，**嵌套时按最外层**整体剥掉；
    - 找不到配对 ``]`` 的孤立 ``[`` 按普通字符保留（绝不能把它后面的正文一起吃掉）。

    这里刻意**不按前缀白名单**（回复/图片/…）过滤：不同适配器的块名不可穷举，而
    「多剥一个块」最多是漏掉一次点名（宁可不补），「少剥一个块」却会补错角色。
    """
    if not text or "[" not in text:
        return text
    result: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "[":
            result.append(char)
            index += 1
            continue
        depth = 1
        cursor = index + 1
        while cursor < length and depth > 0:
            if text[cursor] == "[":
                depth += 1
            elif text[cursor] == "]":
                depth -= 1
            cursor += 1
        if depth > 0:
            # 没有配对的 ]：按普通字符处理，继续往后扫（别把正文一起吃掉）
            result.append(char)
            index += 1
            continue
        result.append(" ")
        index = cursor
    return re.sub(r"\s+", " ", "".join(result)).strip()


def merge_entries(*groups: list[CharacterEntry]) -> list[CharacterEntry]:
    """按给定顺序合并多组角色并去重（保留先出现的那个）。"""
    seen: set[str] = set()
    merged: list[CharacterEntry] = []
    for group in groups:
        for entry in group:
            key = entry_key(entry)
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return merged


@dataclass
class NamingDecision:
    """「这次该补谁」的确定性判定结果。

    Attributes:
        kind: ``single`` / ``multi`` / ``uncertain`` / ``none``
        characters: 判定出的角色（``uncertain`` 时是「宁少勿多」的保守结果）
        basis: 判定依据，见 :data:`BASIS_USER_NAMED` 等
        stamp: 依据那条消息的时间戳（用于日志里打印时刻）
        note: 追加说明（会进 DEBUG 日志）
        stale: 是否因为「超出粘性时效」而判成无信号（此时不得回退到旧的角色集合）
    """

    kind: str = "none"
    characters: list[CharacterEntry] = field(default_factory=list)
    basis: str = BASIS_NONE
    stamp: float | None = None
    note: str = ""
    stale: bool = False

    @property
    def multi(self) -> bool:
        """本次判定是否属于「多人同框」。"""
        return self.kind == "multi"

    def names(self) -> str:
        """角色名的可读列表。"""
        return "、".join(entry.name or entry.tag or "?" for entry in self.characters)

    def label(self) -> str:
        """生成 DEBUG 日志用的判定依据文案。"""
        if self.kind == "none" or not self.characters:
            return f"判定依据=无(不补){f'（{self.note}）' if self.note else ''}"
        clock = (
            time.strftime("%H:%M:%S", time.localtime(self.stamp))
            if self.stamp
            else "未知时刻"
        )
        if self.basis == BASIS_USER_NAMED:
            head = f"判定依据=用户点名({clock}):{self.names()}"
        else:
            head = f"判定依据=最近提及:{self.names()}"
        if self.kind == "uncertain":
            head += "（换人/追加不确定）"
        return f"{head}{f'（{self.note}）' if self.note else ''}"


def resolve_naming_decision(
    messages: list[dict[str, Any]],
    roster: list[CharacterEntry],
    *,
    now: float | None = None,
    stickiness_hours: float = 6.0,
    multi_window_minutes: float = 5.0,
    bot_ids: frozenset[str] | set[str] = frozenset(),
) -> NamingDecision:
    """按「用户点名优先 + 主角粘性」判定本次该补哪些角色。

    判定优先级（与 README「判定优先级」一节一致）：**描述点名 > 用户点名(粘性)
    > LLM 仲裁 > 不补**，本函数负责其中的「用户点名/粘性」这一层。

    规则：

    1. 只看**不是 bot 自己**的消息（bot 的复述不算点名），且消息正文先经
       :func:`message_text` 剥掉 ``[回复…]`` / ``[图片(…)]`` 这类中括号块；
    2. 最近一条带角色表内角色的消息若已超出 ``stickiness_hours``，视为无信号
       （``kind="none"`` 且 ``stale=True``），不得回退到那个旧角色；
    3. 最近一条点名消息含 ≥2 个角色 → 多人（这些人，按角色表顺序）；
    4. 含 1 个角色且文本里有追加词（还有/一起/俩/都/加上/和她/and/both）
       → 多人（本条 + 上一条点名的角色，本条在前）；
    5. 含 1 个角色且文本里有改口词（算了/不是/换/改成，或**处于改口语境**的「还是」）
       → 只取本条那一个；「是 A 还是 B？」这类选择疑问句**不算**改口（1.0.12 起）；
    6. 都不含：与上一条点名消息间隔 < ``multi_window_minutes`` → ``uncertain``
       （交给 LLM 仲裁，本函数给出「只补最近那一个」的保守结果）；
       间隔 ≥ 或没有上一条点名消息 → 只取本条那一个；
    7. 角色表为空、窗口里没有可用的点名信号 → ``kind="none"``，由调用方走
       LLM 兜底。

    Args:
        messages: 消息窗口（顺序不限，内部会整理成从新到旧）
        roster: 角色表
        now: 当前时间戳（测试可注入）
        stickiness_hours: 主角粘性时效（小时）；``<=0`` 表示不限时效
        multi_window_minutes: 连点两人的判定窗口（分钟）
        bot_ids: 本机 bot 的 ``sender_id`` 集合（解析不到时给空集合即可）

    Returns:
        :class:`NamingDecision`
    """
    if not messages or not roster:
        return NamingDecision(kind="none", note="无上文或无角色表")
    current_time = time.time() if now is None else float(now)
    ordered = order_messages_newest_first(messages)

    # 用户点名候选：从新到旧，逐条看「不是 bot」的消息里命中的角色表内角色
    named: list[tuple[float | None, list[CharacterEntry], str, bool]] = []
    for message in ordered:
        if is_known_bot_message(message, bot_ids):
            continue
        text = message_text(message)
        hits = find_characters_in_text(text, roster)
        if not hits:
            continue
        named.append(
            (
                message_time(message),
                hits,
                text,
                is_attributed_user_message(message, bot_ids),
            )
        )
    if not named:
        return NamingDecision(
            kind="none",
            note="窗口内没有可用的点名信号（角色表内角色只出现在 bot 消息里，或根本没有）",
        )

    current_stamp, current_hits, current_text, current_attributed = named[0]
    basis = BASIS_USER_NAMED if current_attributed else BASIS_LATEST_MENTION

    # 主角粘性时效：最近一次点名太旧 → 视为无信号（不补，交给 LLM 兜底）
    if (
        stickiness_hours > 0
        and current_stamp is not None
        and (current_time - current_stamp) > stickiness_hours * 3600
    ):
        age = (current_time - current_stamp) / 3600
        return NamingDecision(
            kind="none",
            stale=True,
            stamp=current_stamp,
            note=(
                f"最近一次点名已超出粘性时效（{age:.1f}h > {stickiness_hours:g}h），"
                "视为无信号"
            ),
        )

    # 3.1 最近一条点名消息里就有多个角色 → 多人
    if len(current_hits) >= 2:
        return NamingDecision(
            kind="multi",
            characters=list(current_hits),
            basis=basis,
            stamp=current_stamp,
            note=f"最近一条点名消息含 {len(current_hits)} 个角色",
        )

    # 3.2 追加词 → 本条 + 上一条点名的角色
    if contains_any_word(current_text, ADDITIVE_WORDS):
        previous = named[1][1] if len(named) > 1 else []
        merged = merge_entries(current_hits, previous)
        if len(merged) >= 2:
            return NamingDecision(
                kind="multi",
                characters=merged,
                basis=basis,
                stamp=current_stamp,
                note=f"追加语义（本条 + 上一条点名，共 {len(merged)} 人）",
            )
        return NamingDecision(
            kind="single",
            characters=list(current_hits),
            basis=basis,
            stamp=current_stamp,
            note="追加语义，但上一条没有点名别的角色",
        )

    # 3.3 改口词 → 只取本条那一个（「还是」需判语境，见 contains_corrective_word）
    if contains_corrective_word(current_text):
        return NamingDecision(
            kind="single",
            characters=list(current_hits[:1]),
            basis=basis,
            stamp=current_stamp,
            note="改口语义（放弃上一条点名的角色）",
        )

    # 3.4 都不含 → 看与上一条点名消息的时间差
    if len(named) > 1:
        previous_stamp = named[1][0]
        if (
            current_stamp is not None
            and previous_stamp is not None
            and multi_window_minutes > 0
        ):
            gap_minutes = abs(current_stamp - previous_stamp) / 60.0
            if gap_minutes < multi_window_minutes:
                return NamingDecision(
                    kind="uncertain",
                    characters=list(current_hits[:1]),
                    basis=basis,
                    stamp=current_stamp,
                    note=(
                        f"与上一条点名相差 {gap_minutes:.1f} 分钟"
                        f"（< {multi_window_minutes:g} 分钟）：换人还是追加不确定，"
                        "交 LLM 仲裁；无结论时宁少勿多只补最近那一个"
                    ),
                )
    return NamingDecision(
        kind="single",
        characters=list(current_hits[:1]),
        basis=basis,
        stamp=current_stamp,
    )


def _replace_literal(text: str, needle: str, replacement: str) -> tuple[str, int]:
    """替换文本里的字面量；返回 (新文本, 替换处数)。"""
    needle = (needle or "").strip()
    if not text or not needle:
        return text, 0
    if needle.isascii():
        pattern = re.compile(
            rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", re.IGNORECASE
        )
    else:
        pattern = re.compile(re.escape(needle))
    new_text, hits = pattern.subn(replacement, text)
    return new_text, hits


def replace_character_reference(
    text: str, wrong: CharacterEntry, right: CharacterEntry
) -> tuple[str, int]:
    """把文本里对 ``wrong`` 的引用替换成 ``right``。

    **只替换角色表里已知的字符串**（``wrong`` 的名字 / 别名 / 标准标签），
    不做任何猜测性改写；``right`` 既没有名字也没有标签时原样返回，替换处数为 0，
    由调用方退回 warn 行为。

    Returns:
        (新文本, 替换处数)
    """
    if not text or wrong is None or right is None:
        return text, 0
    if entry_key(wrong) == entry_key(right):
        return text, 0
    replacement = right.name.strip() or right.tag.strip()
    if not replacement:
        return text, 0

    result = text
    count = 0
    wrong_tag = wrong.resolved_tag()
    if wrong_tag:
        result, hits = _replace_literal(
            result, wrong_tag, right.resolved_tag() or replacement
        )
        count += hits
    # 名字与别名按长度倒序替换，避免短别名先下手把长名字切碎
    for keyword in sorted(wrong.keywords(), key=len, reverse=True):
        result, hits = _replace_literal(result, keyword, replacement)
        count += hits
    return result, count


def _format_roster_for_prompt(roster: list[CharacterEntry]) -> str:
    """将角色表格式化为 LLM 提示词中的参考文本。"""
    lines: list[str] = []
    for entry in roster:
        parts = [entry.name.strip()]
        if entry.game.strip():
            parts.append(entry.game.strip())
        parts.append(f"标签={entry.tag.strip() or '未提供'}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


_EXTRACT_SYSTEM_PROMPT = """你是角色识别助手，服务于生图提示词补全。

任务：
1. 阅读「画面描述」，先判断它**已经写了哪些角色**——这些不要再列出来。
2. 结合「对话上文」，找出**画面需要、但描述里还没写**的具体角色（游戏/动漫/二次元角色），
   按"与本次请求的相关度"从高到低排列。
3. 判断 multi：如果本次画面需要**多个角色同时出镜**（例如用户说"画她们俩""我和她一起""双人同框"，
   或上文明显在讨论两个以上角色共同出现的场景），multi 设为 true，并把需要的角色都列出；
   只画一个角色时 multi 设为 false。
4. 没有任何明确角色时输出 {"multi": false, "characters": []}。

输出格式（只输出一个 JSON 对象）：
{"multi": true 或 false, "characters": [{"name": "角色名", "game": "出处作品名", "tag": "角色标签"}]}

字段说明：
- name：角色的中文或常用名。
- game：该角色的出处作品名（游戏名优先），不确定时留空。
- tag：该角色在 Danbooru / NovelAI 中的标准角色标签，格式为 `角色名 (作品名)`，
  例如 `elysia \\(honkai impact 3rd\\)`、`hu tao \\(genshin impact\\)`。
  必须使用英文标准拼写、小写、空格分隔，括号需转义为 `\\(` 与 `\\)`；
  拼写没有把握时留空，不要编造。

硬性规则：
- 只输出 JSON，不要 markdown 代码块，不要任何解释。
- characters 最多 6 项，按相关度降序；每一项都必须是画面里真实需要的角色。
- 名字必须是具体有名字的角色；“我”“你”、bot 自身或泛指（如“一个女孩”）不算。
- 若角色在「角色表」中出现，优先使用角色表中的名字、游戏名与标签。
- 宁缺毋滥：不确定的角色不要列，不确定的字段留空。"""

_EXTRACT_USER_TEMPLATE = """对话上文（按时间顺序）：
{context}

本次画面描述：
{description}

角色表（参考，可忽略）：
{roster}

请按要求输出 JSON。"""


def _strip_json_code_fence(raw_text: str) -> str:
    """剥离模型可能返回的 JSON 代码块包装。"""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _is_deepseek_model_entry(model_entry: Any) -> bool:
    """判断模型条目是否指向 DeepSeek 提供商。"""
    if not isinstance(model_entry, dict):
        return False

    provider = str(model_entry.get("api_provider") or "").lower()
    base_url = str(model_entry.get("base_url") or "").lower()
    model_identifier = str(model_entry.get("model_identifier") or "").lower()
    return (
        "deepseek" in provider
        or "deepseek" in base_url
        or model_identifier.startswith("deepseek-")
    )


def _prepare_model_set(model_name: str) -> ModelSet:
    """为角色提取任务准备模型集，并对 DeepSeek 做请求级兼容。

    Args:
        model_name: 配置指定的模型名；空字符串时回退 UTILS_SMALL 任务模型

    Returns:
        ModelSet 实例
    """
    try:
        if model_name.strip():
            model_set = llm_api.get_model_set_by_name(model_name.strip())
        else:
            model_set = llm_api.get_model_set_by_task(TaskType.UTILS_SMALL.value)
    except Exception:  # noqa: BLE001 —— 模型集取不到就回退，不能把异常抛给生图链路
        logger.debug(
            f"模型集获取失败（{model_name or 'UTILS_SMALL'}），回退 UTILS_SMALL"
        )
        model_set = llm_api.get_model_set_by_task(TaskType.UTILS_SMALL.value)

    if not isinstance(model_set, list):
        return model_set

    prepared_model_set = deepcopy(model_set)
    for model_entry in prepared_model_set:
        if not _is_deepseek_model_entry(model_entry):
            continue

        extra_params = model_entry.get("extra_params")
        if not isinstance(extra_params, dict):
            extra_params = {}
        else:
            extra_params = dict(extra_params)

        # DeepSeek V4 思考能力切到 thinking 参数；提取任务只需直接文本，
        # 显式关闭 thinking 避免只返回 reasoning 通道导致拿不到正文。
        extra_params.pop("reasoning_effort", None)
        extra_params["enable_thinking"] = False
        extra_params["thinking"] = {"type": "disabled"}
        model_entry["extra_params"] = extra_params

    return prepared_model_set


async def _resolve_response_text(response: Any) -> str:
    """统一提取响应正文；正文为空时回退到 reasoning_content。"""
    raw_text = await response
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()

    reasoning_text = getattr(response, "reasoning_content", None)
    if isinstance(reasoning_text, str) and reasoning_text.strip():
        logger.debug("角色提取响应 content 为空，回退使用 reasoning_content")
        return reasoning_text.strip()

    return raw_text.strip() if isinstance(raw_text, str) else ""


@dataclass
class CharacterExtraction:
    """LLM 提取结果。

    Attributes:
        characters: 按相关度降序排列的角色列表
        multi: 本次画面是否需要多个角色同时出镜
    """

    characters: list[CharacterEntry] = field(default_factory=list)
    multi: bool = False


def _parse_extraction_payload(payload: Any) -> CharacterExtraction | None:
    """解析 LLM 返回的 JSON，兼容新旧两种结构。"""
    if not isinstance(payload, dict):
        return None

    raw_characters = payload.get("characters")
    multi_raw = payload.get("multi")

    entries: list[CharacterEntry] = []
    if isinstance(raw_characters, list):
        for item in raw_characters:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            tag = str(item.get("tag") or "").strip()
            if not name and not tag:
                continue
            entries.append(
                CharacterEntry(
                    name=name or tag,
                    game=str(item.get("game") or "").strip(),
                    tag=tag,
                )
            )
    elif raw_characters is None:
        # 兼容旧版单角色结构 {"name": ..., "game": ..., "tag": ...}
        name = str(payload.get("name") or "").strip()
        tag = str(payload.get("tag") or "").strip()
        if name or tag:
            entries.append(
                CharacterEntry(
                    name=name or tag,
                    game=str(payload.get("game") or "").strip(),
                    tag=tag,
                )
            )

    multi = bool(multi_raw) if isinstance(multi_raw, bool) else False
    if len(entries) > 1:
        # 列出了多个角色，说明画面需要多人，模型漏填 multi 时不让它漏判
        multi = True

    if not entries and not multi:
        return CharacterExtraction()
    return CharacterExtraction(characters=entries, multi=multi)


async def extract_characters_via_llm(
    description: str,
    context_text: str,
    roster: list[CharacterEntry],
    translate_model: str = "",
    timeout: float = 0.0,
) -> CharacterExtraction | None:
    """用 LLM 从对话上文中提取画面需要补充的角色（可多个）。

    Args:
        description: 本次生图的画面描述
        context_text: 最近对话上文（可读文本）
        roster: 角色表（作为规范化参考）
        translate_model: 提取用模型名；空字符串回退 UTILS_SMALL
        timeout: 本次提取的超时秒数；``<=0`` 表示不限时。生图 Action 会同步
            等待本函数，模型端挂起时必须有兜底，否则表现为 bot 卡死。

    Returns:
        提取结果；失败或超时返回 None
    """

    async def _run() -> str:
        request = llm_api.create_llm_request(
            model_set=_prepare_model_set(translate_model),
            request_name="char_prompt_guard_extract",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_EXTRACT_SYSTEM_PROMPT)))
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(
                    _EXTRACT_USER_TEMPLATE.format(
                        context=context_text.strip() or "<无>",
                        description=description.strip() or "<无>",
                        roster=_format_roster_for_prompt(roster) or "<空>",
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
        cleaned_text = _strip_json_code_fence(raw_text)
        return _parse_extraction_payload(json.loads(cleaned_text))
    except TimeoutError:
        logger.warning(f"LLM 角色提取超时（{timeout:g}s），本次不补角色")
        return None
    except Exception as e:  # noqa: BLE001 —— 提取失败只记日志并返回 None（放行生图）
        logger.warning(f"LLM 角色提取失败: {e}")
        return None
