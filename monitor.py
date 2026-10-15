"""主题跟踪器（``subject_monitor``，1.0.14 起）。

按**时间正序**扫最近 ``monitor.window`` 条消息，逐条演进一个「当前主题」（这次该画谁），
并把「主题 + 置信度 + 演进轨迹」交给判定链：

    主题跟踪(15条): 爱莉希雅(3h前) → [本尊:人格配置] → 花火 ← 当前主题（置信度 0.76）

1.0.13 及以前，「这次该补谁」是几处零散规则拼出来的：自我指代扫描
（``handler._detect_self_reference`` / ``_resolve_self_reference``）、
用户点名 + 主角粘性（``character.resolve_naming_decision``）、「最近提及」兜底。
1.0.14 把它们收敛成一个显式状态机 + 一条置信度公式，规则、优先级、可读日志都集中在
本模块一处（详见 README「主题跟踪（``[monitor]``，1.0.14 起）」）。

设计约束（都是踩过坑换来的）：

- **只有用户消息能改变状态**：bot 的复述（「好的，画爱莉希雅～」）既不算点名、
  也不清空主题 —— 否则用户刚改口，bot 一句复述就把旧角色顶回来；
- **普通闲聊既不换主题、也不刷新置信度**：一句「嗯」不该把 3 小时前的旧主题复活
  （``monitor.min_confidence`` 这条闸门就是为它设的，见 tests C52）；
- **拿不准就不补**：本尊身份不在角色表、代词没有先行词、置信度不足 —— 一律不补，
  绝不用「粘性」把更早的旧角色捡回来；
- **同一条消息里的「本尊 + 角色名」＝拆单（1.0.16）**：调用方常把一单拆成两张图
  （「本尊看看，然后穿着爱莉来看看～」＝先画本尊、再画壳），这一轮既不能按角色名
  定主题、也不能按本尊身份定主题，只标记「不可单独确定」并交下游按**当前画面
  描述**由 LLM 判定；
- **不做外观识别**：不从「白发 / 粉眼 / 和服」这类画面关键词反推角色。

状态机（窗口内按时间正序逐条演进，初态 ``subject = 无``）：

===========================================  ==========================================
本条消息（已剥掉 ``[回复…]`` / ``[图片…]``）  对主题的作用
===========================================  ==========================================
含改口词（``算了 / 不是 / 换 / 改成``，或处于  先清空旧主题，再按本条的名字设定
改口语境的「还是」）                          （信号强度 1.0）
含 ≥2 个角色表内角色，或含追加词              ``subject = 这些人``（多人；追加词会与
（``ADDITIVE_WORDS``：还有/一起/俩/都/加上…）  现有主题合并）
**同一条**消息里既有自我指代词、又有角色      **拆单（一单多件，1.0.16 起）**：
表内角色                                     主题保持「不可单独确定」—— 既不是那个
                                            角色名、也不是本尊身份 → ``status = "split"``，
                                            交下游按**当前画面描述**由 LLM 判定；
                                            判定不出（LLM 空/失败/超时）就不补
含 1 个角色表内角色                          ``subject = 该角色``（信号强度 1.0）
含自我指代词（``SELF_REFERENCE_WORDS``）      ``subject = 本尊身份``（强度 0.95）；
                                            解析不到 / 不在角色表 → **不可用**
含人称代词（见 :data:`SUBJECT_PRONOUNS`）     ``subject`` **保持不变**（回指上一个主题），
                                            按「粘性」重新计时（强度 0.7）；
                                            主题为空 → **不可用（代词无先行词）**
以上都不含（普通闲聊）                        **什么都不变**（主题与置信度都不动）
===========================================  ==========================================

置信度（只记在「设定 / 确认主题的那条消息」上）::

    confidence = 信号强度 × exp(-Δt / half_life)
    Δt = 该消息距触发消息的分钟数（half_life = monitor.half_life_minutes，默认 45）

``confidence < monitor.min_confidence``（默认 0.5）→ 主题视同「没有信号」，不补。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from .character import (
    ADDITIVE_WORDS,
    BASIS_LATEST_MENTION,
    BASIS_USER_NAMED,
    CharacterEntry,
    NamingDecision,
    contains_any_word,
    contains_corrective_word,
    find_characters_in_text,
    find_roster_identity,
    find_self_reference_word,
    is_attributed_user_message,
    is_known_bot_message,
    merge_entries,
    message_text,
    message_time,
)

logger = get_logger("char_prompt_guard")

#: 窗口上限（``monitor.window`` 超过它就按它处理）。
#: 建议区间 5~40：更小的窗口会漏掉稍早的主题信号，更大的窗口容易把别的话题里的
#: 角色翻出来 —— 40 条已经覆盖「一次连续对话」的常见长度。
MAX_WINDOW = 40

#: 信号强度（对应 README 的置信度公式）
STRENGTH_NAMED = 1.0  # 明确角色名
STRENGTH_CORRECTIVE = 1.0  # 改口后重新点名的那个角色
STRENGTH_MULTI = 1.0  # 一次点多个 / 追加词（本条点名是明确的，强度不减）
STRENGTH_SELF = 0.95  # 本尊身份解析成功（多一跳间接）
STRENGTH_STICKY = 0.7  # 沿用旧主题（代词回指这类「确认」信号）

#: 「代词回指」小词表（1.0.14 自定，命中即「主题保持不变」，见模块 docstring）。
#:
#: 收录口径：
#:
#: - **第三人称 / 指示代词**：她 / 他 / 它 / 她们 / 他们 / 它们 / 那位 / 那一位 / 那个 /
#:   这个人 / 那个人 / 这人 / 那人 / 台上那个 / 台上那位 / 刚才那个 / 之前那个；
#:   单字的 ``他`` / ``它`` 另见 :data:`_PRONOUN_GUARDED`（排掉「其他 / 其它 / 无他」）；
#: - **第二人称 你 / 您 / 你们**：**故意收录**。「想看看你～」「你今天穿什么」既可能指
#:   角色表里的角色、也可能指 bot 本尊，而「本尊是谁」只有调用方知道（插件看不到）；
#:   这里按「回指上一个主题」处理，真正的本尊请求交给**优先级更高**的自我指代规则
#:   （本尊 / 你自己 / 你本人 …）。实测用例 C40 依赖这条：3 小时前点名过流萤、用户
#:   只说「想看看你」，必须仍然补流萤（1.0.11 起的「主角粘性」语义）。
#: - **不含 我 / 我们**：那是指用户自己，与画面主题无关。
SUBJECT_PRONOUNS: tuple[str, ...] = (
    "台上那个",
    "台上那位",
    "刚才那个",
    "之前那个",
    "那一位",
    "那个人",
    "这个人",
    "那位",
    "那个",
    "她们",
    "他们",
    "它们",
    "你们",
    "这人",
    "那人",
    "他",
    "它",
    "她",
    "你",
    "您",
)

#: 需要排掉复合词的单字代词（``其他 / 其它 / 无他 / 任他`` 里的「他」「它」不是人称代词）
_PRONOUN_GUARDED = frozenset({"他", "它"})
#: 单字代词的负向后顾字符集
_PRONOUN_STOP_PREFIX = "其无任"

#: 代词匹配用正则（长词在前，保证 ``那个人`` 不会被 ``那个`` 截胡）
_SUBJECT_PRONOUN_PATTERN = re.compile(
    "|".join(
        (
            rf"(?<![{_PRONOUN_STOP_PREFIX}]){re.escape(word)}"
            if word in _PRONOUN_GUARDED
            else re.escape(word)
        )
        for word in SUBJECT_PRONOUNS
    )
)


def find_subject_pronoun(text: str) -> str:
    """返回文本里命中的第一个人称代词；没有命中返回空串。"""
    if not text:
        return ""
    match = _SUBJECT_PRONOUN_PATTERN.search(text)
    return match.group(0) if match else ""


def _names(entries: list[CharacterEntry]) -> str:
    """角色名的可读列表（``爱莉希雅、流萤``）。"""
    return "、".join(entry.name or entry.tag or "?" for entry in entries)


def age_label(minutes: float) -> str:
    """把「距今多少分钟」渲染成 trace 里的短标签（``刚刚`` / ``10分钟前`` / ``3h前``）。"""
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{int(minutes)}分钟前"
    if minutes < 60 * 24:
        return f"{minutes / 60:.0f}h前"
    return f"{minutes / (60 * 24):.0f}天前"


def _names_label(
    entries: list[CharacterEntry],
    stamp: float | None,
    trigger_stamp: float,
) -> str:
    """trace 里的一段「角色名(多久前)」。"""
    label = _names(entries)
    if stamp is None:
        return label
    minutes = max(0.0, (trigger_stamp - stamp) / 60.0)
    return f"{label}({age_label(minutes)})"


@dataclass
class SubjectTrack:
    """一次主题跟踪的完整结果。

    Attributes:
        entries: 当前主题（不可用 / 无主题时为空）
        confidence: 当前主题的置信度 = 信号强度 × 时间衰减
        trace: 演进轨迹（进 DEBUG 日志），形如
            ``爱莉希雅(3h前) → [本尊:人格配置] → 花火``
        status: ``ok`` / ``low_confidence`` / ``unavailable`` / ``empty`` /
            ``split``（1.0.16 起：同一条消息里既有自我指代又有角色表内角色 →
            一单多件，「当前主题」不可单独确定，交下游按当前画面描述判定）
        reason: 不可用或不补的**具体**原因（进判定依据，用户要能一眼看出该改什么）
        basis: 完整的判定依据文案（形如 ``判定依据=主题跟踪:...（置信度 0.76）``）
        ambiguous: 是否「换人还是追加不确定」（相邻两条点名消息间隔 <
            ``multi.multi_window_minutes``，且本条没有追加/改口标记）——
            为真时交 LLM 仲裁（沿用 1.0.10 起的既有语义）
        signal_kind: 最近一次信号类型（``named`` / ``multi`` / ``additive`` /
            ``corrective`` / ``self`` / ``pronoun`` / ``split``）
        signal_label: 最近一次信号的可读描述（进判定依据）
        signal_stamp: 最近一次信号那条消息的时间戳
        self_source: 本尊身份来源（``配置`` / ``人格配置``）
        attributed: 最近一次信号是否**能确定**是用户发的（决定「用户点名」还是
            「最近提及」，与 1.0.13 同一口径）
        scanned: 实际扫进窗口的消息条数
        window: 生效的窗口大小（``monitor.window``，已按 :data:`MAX_WINDOW` 收敛）
        min_confidence: 生效的置信度阈值
    """

    entries: list[CharacterEntry] = field(default_factory=list)
    confidence: float = 0.0
    trace: str = ""
    status: str = "empty"
    reason: str = ""
    basis: str = ""
    ambiguous: bool = False
    signal_kind: str = ""
    signal_label: str = ""
    signal_stamp: float | None = None
    self_source: str = ""
    attributed: bool = True
    scanned: int = 0
    window: int = 0
    min_confidence: float = 0.5

    @property
    def usable(self) -> bool:
        """主题是否**可用**（置信度达标且不是「不可用」状态）。"""
        return self.status == "ok" and bool(self.entries)

    @property
    def unavailable(self) -> bool:
        """是否属于「不可用」：本尊身份不在角色表 / 取不到身份名 / 代词无先行词。"""
        return self.status == "unavailable"

    @property
    def split(self) -> bool:
        """是否属于「拆单」（1.0.16）：同一条消息里既有自我指代又有角色表内角色。

        调用方把一单拆成多张图（先本尊、再壳）时，这一轮的主题**不可单独确定** ——
        既不能按那个角色名补（1.0.15 的事故），也不能按本尊身份补。下游应改问
        LLM 按**当前画面描述**判定（见 ``handler._collect_candidates`` 的拆单分支）。
        """
        return self.status == "split"

    def subject_names(self) -> str:
        """当前主题的角色名列表。"""
        return _names(self.entries)

    def naming(self) -> NamingDecision:
        """转成既有判定链认识的 :class:`~.character.NamingDecision`。

        可用的主题按 ``single`` / ``multi`` 返回（沿用 ``multi.decide="bot"`` 的
        现有语义）；「换人还是追加不确定」返回 ``uncertain``（与 1.0.13 完全一致，
        确保 ``_resolve_multi`` 不会被误判成多人）；其余一律 ``none`` —— 由主题跟踪
        自己的 :attr:`basis` 说明为什么没补。**拆单**（1.0.16）也走 ``none``：
        主题不可单独确定，是否补入由下游按当前画面描述问 LLM 决定，而不是由这里
        给出候选人（见 ``handler._collect_candidates`` 的拆单分支）。
        """
        if self.usable:
            if self.ambiguous:
                kind = "uncertain"
            elif len(self.entries) > 1:
                kind = "multi"
            else:
                kind = "single"
            return NamingDecision(
                kind=kind,
                characters=list(self.entries),
                basis=BASIS_USER_NAMED if self.attributed else BASIS_LATEST_MENTION,
                stamp=self.signal_stamp,
                note=f"主题跟踪（置信度 {self.confidence:.2f}）",
            )
        return NamingDecision(kind="none", note=self.reason or "主题跟踪：无可用主题")

    def log_line(self) -> str:
        """DEBUG 日志行：``主题跟踪(15条): … ← 当前主题（置信度 0.76）``。

        括号里是**生效的窗口大小**（``monitor.window``，已按 :data:`MAX_WINDOW`
        收敛）；本次实际扫进窗口的条数见 :attr:`scanned`。
        """
        head = f"主题跟踪({self.window}条)"
        body = self.trace or "（无）"
        if self.usable:
            tail = f"← 当前主题（置信度 {self.confidence:.2f}）"
            if self.ambiguous:
                tail += "；换人还是追加不确定，交 LLM 仲裁"
        elif self.unavailable:
            tail = f"← 不可用（{self.reason}）"
        elif self.split:
            tail = (
                "← 不可单独确定（拆单：一单多件，按当前画面描述交 LLM 判定；"
                "判定不出就不补）"
            )
        elif self.entries:
            tail = (
                f"← 置信度 {self.confidence:.2f} 低于阈值 {self.min_confidence:g}，不补"
                f"（信号={self.signal_label or '无'}）"
            )
        else:
            tail = f"← 无当前主题（{self.reason}）"
        return f"{head}: {body} {tail}"


def track_subject(
    messages: list[dict[str, Any]],
    roster: list[CharacterEntry],
    *,
    window: int = 15,
    min_confidence: float = 0.5,
    half_life_minutes: float = 45.0,
    multi_window_minutes: float = 5.0,
    self_reference_enabled: bool = True,
    bot_ids: frozenset[str] | set[str] = frozenset(),
    resolve_identity: Callable[[], Any] | None = None,
    now: float | None = None,
) -> SubjectTrack:
    """按时间正序扫消息窗口，维护「当前主题」并给出置信度。

    Args:
        messages: 上文消息（含触发消息，已按「旧 → 新」排列），内部只取末尾
            ``window`` 条
        roster: 角色表（为空时调用方不该启用跟踪器）
        window: 窗口大小（消息条数，收敛到 1~:data:`MAX_WINDOW`）
        min_confidence: 置信度阈值，低于它主题视同「没有信号」
        half_life_minutes: 时间衰减的半衰期（分钟）；``<=0`` 表示不衰减
        multi_window_minutes: 「换人还是追加不确定」的判定窗口（分钟），
            与 ``multi.multi_window_minutes`` 同一语义
        self_reference_enabled: 是否启用自我指代规则（对应
            ``general.skip_on_self_reference``，且描述里没有角色表内角色）
        bot_ids: 本机 bot 的 ``sender_id`` 集合（解析不到时给空集合）
        resolve_identity: 取「本尊身份」的回调（返回带 ``candidates`` / ``source``
            的对象，见 ``handler.resolve_true_self_identity``）。用回调注入是为了
            让本模块不反向依赖 handler（避免循环导入），测试也好替换
        now: 触发消息的时间戳（取不到时用当前时间）

    Returns:
        :class:`SubjectTrack`
    """
    size = int(window) if isinstance(window, (int, float)) else 15
    size = max(size, 1)
    if size > MAX_WINDOW:
        logger.debug(
            f"monitor.window={size} 超过上限 {MAX_WINDOW}，按 {MAX_WINDOW} 处理"
        )
        size = MAX_WINDOW
    scoped = list(messages[-size:]) if messages else []

    trigger_stamp = message_time(scoped[-1]) if scoped else None
    if trigger_stamp is None:
        trigger_stamp = float(now) if now is not None else time.time()

    current: list[CharacterEntry] = []
    strength = 0.0
    signal_stamp: float | None = None
    signal_kind = ""
    signal_label = ""
    self_source = ""
    self_matched = ""
    attributed = True
    unavailable_reason = ""
    ambiguous = False
    #: 拆单（1.0.16）：最近一次「同一条消息里既有自我指代又有角色名」的那条
    #: （存自我指代词与角色名，供 trace / 判定依据文案用）。它是**可以被打断**的
    #: 状态：之后任何一条能定主题的消息（改口 / 点名 / 多人 / 本尊身份）都会把它
    #: 清掉 —— 与「最后一条点名生效」的既有口径一致。
    split_word = ""
    split_names = ""
    last_named_stamp: float | None = None
    trace_parts: list[str] = []
    user_count = 0

    for message in scoped:
        # bot 自己的消息只跳过：既不算点名，也不清空主题（否则用户刚改口就被
        # bot 的复述顶回去）。身份判定的口径与 1.0.13 的 resolve_naming_decision
        # 一致 —— 能确定是 bot 的跳过，其余按用户消息处理（is_attributed_user_message
        # 只决定判定依据里写「用户点名」还是「最近提及」，拿不到 adapter 信息时不至于
        # 整条链路静默失效）。
        if is_known_bot_message(message, bot_ids):
            continue
        text = message_text(message)
        if not text:
            continue
        user_count += 1
        stamp = message_time(message)
        # 「能不能确定是用户发的」只影响判定依据的文案（用户点名 / 最近提及），
        # 与 1.0.13 的 resolve_naming_decision 同一口径；拿不到 adapter 信息时
        # 退一步写「最近提及」，不会因此不处理这条消息
        attributed = is_attributed_user_message(message, bot_ids)
        chars = find_characters_in_text(text, roster)
        corrective = contains_corrective_word(text)
        additive = contains_any_word(text, ADDITIVE_WORDS)
        self_word = find_self_reference_word(text) if self_reference_enabled else ""
        pronoun = find_subject_pronoun(text)

        # ① 改口：**先清空**旧主题，再按本条的名字设定
        if corrective:
            current = []
            unavailable_reason = ""
            ambiguous = False
            # 改口是「重新指定」，旧的一单多件标记一并作废（最后一条消息生效）
            split_word = ""
            split_names = ""
            trace_parts.append("[改口]")
            if chars:
                current = list(chars)
                strength = STRENGTH_CORRECTIVE
                signal_stamp = stamp
                signal_kind = "corrective"
                signal_label = f"改口→{_names(current)}"
                trace_parts.append(_names_label(current, stamp, trigger_stamp))
                last_named_stamp = stamp
            else:
                # 「算了，不画了」：主题被清空且没有新名字 → 没有信号
                strength = 0.0
                signal_stamp = stamp
                signal_kind = "corrective"
                signal_label = "改口（本条没有新名字）"
            continue

        # ①.5 拆单（1.0.16）：**同一条**消息里既有自我指代词、又有角色表内角色。
        # 典型是调用方把一单拆成两张图 —— 用户「本尊看看，然后穿着爱莉来看看～」
        # 是一单两件（先看本尊、再看壳），当前这张要画的是哪一件只有**画面描述**
        # 说得清，对话上文里两者权重相同。
        # 所以这里既不能按那个角色名定主题（1.0.15 的事故：整条消息被
        # 「用户点名→爱莉希雅」吃掉，把 LLM 按画面认出的「花火」压掉了），也不能
        # 按本尊身份定主题 —— 只标记「拆单/不可单独确定」，交下游按当前画面判定。
        # **只在同一条消息内成立**：两者分别出现在不同消息里时按原规则走
        # （「先看本尊」→「穿爱莉」＝最后一条点名生效）。
        if chars and self_word:
            current = []
            unavailable_reason = ""
            ambiguous = False
            split_word = self_word
            split_names = _names(chars)
            # 拆单没有自己的置信度：它不给结论，只表示「主题不可单独确定」
            strength = 0.0
            signal_stamp = stamp
            signal_kind = "split"
            signal_label = f"拆单→{self_word}+{split_names}"
            trace_parts.append(f"[拆单:{self_word}+{split_names}]")
            trace_parts.append("需按当前画面判定")
            logger.debug(
                f"主题跟踪：同一条消息里既有自我指代'{self_word}'又有角色名"
                f"（{split_names}）→ 拆单（一单多件），主题不可单独确定，"
                "交 LLM 按当前画面描述判定；判定不出就不补"
            )
            continue

        # ② 本条含 ≥2 个角色表内角色，或含追加词 → 多人主题
        if len(chars) >= 2 or (chars and additive):
            # 能量化主题的消息一定晚于拆单 —— 旧的拆单标记作废（最后一条消息生效）
            split_word = ""
            split_names = ""
            if len(chars) >= 2:
                # 一条消息里点了多个名字：主题就是这些人，**不与旧主题合并** ——
                # 与 1.0.10 起的 resolve_naming_decision 一致，免得把上一位留下
                current = list(chars)
            elif current:
                # 追加词：本条 + 现有主题（「画爱莉希雅」→「还有伊甸，一起画」）
                current = merge_entries(chars, current)
            else:
                current = list(chars)
            unavailable_reason = ""
            strength = STRENGTH_MULTI
            signal_stamp = stamp
            signal_kind = "additive" if additive else "multi"
            signal_label = f"{'追加' if additive else '多人'}→{_names(current)}"
            # 显式点名（尤其一次点多人、或带追加词）本身就消解了「换人还是追加」的歧义
            ambiguous = False
            if additive:
                trace_parts.append("[追加]")
            trace_parts.append(_names_label(current, stamp, trigger_stamp))
            last_named_stamp = stamp
            continue

        # ③ 本条含 1 个角色表内角色 → 主题 = 该角色
        if chars:
            split_word = ""
            split_names = ""
            current = list(chars)
            unavailable_reason = ""
            strength = STRENGTH_NAMED
            signal_stamp = stamp
            signal_kind = "named"
            signal_label = (
                f"{'用户点名' if attributed else '最近提及'}→{_names(current)}"
            )
            # 与上一条点名消息靠得太近又没有追加/改口标记 → 「换人还是追加」不确定，
            # 交 LLM 仲裁（沿用 multi.multi_window_minutes 的既有语义，见 1.0.10 起
            # 的 resolve_naming_decision；测试 C42 守的就是这条）。
            if (
                last_named_stamp is not None
                and stamp is not None
                and multi_window_minutes > 0
                and abs(stamp - last_named_stamp) < multi_window_minutes * 60.0
            ):
                ambiguous = True
                trace_parts.append("[不确定:换人还是追加]")
            else:
                ambiguous = False
            trace_parts.append(_names_label(current, stamp, trigger_stamp))
            last_named_stamp = stamp
            continue

        # ④ 自我指代 → 主题 = 本尊身份（解析不到 / 不在角色表 → 不可用）
        if self_word:
            # 本条没有角色名（有的话①.5 就拆单了）：本尊身份是能定主题的信号，
            # 旧的拆单标记作废（最后一条消息生效）
            split_word = ""
            split_names = ""
            names: list[str] = []
            source = ""
            if resolve_identity is not None:
                try:
                    identity = resolve_identity()
                except Exception as e:  # noqa: BLE001 —— 取身份失败只当「没取到」
                    logger.debug(f"读取本尊身份失败，按「未取到本尊身份」处理: {e}")
                    identity = None
                source = str(getattr(identity, "source", "") or "")
                for raw_name in getattr(identity, "candidates", None) or []:
                    name = str(raw_name or "").strip()
                    if name and name not in names:
                        names.append(name)
            hit: CharacterEntry | None = None
            matched = ""
            for name in names:
                hit = find_roster_identity(name, roster)
                if hit is not None:
                    matched = name
                    break
            ambiguous = False
            if hit is not None:
                current = [hit]
                unavailable_reason = ""
                strength = STRENGTH_SELF
                signal_stamp = stamp
                signal_kind = "self"
                self_source = source
                self_matched = matched
                signal_label = f"本尊切换→{hit.name or hit.tag or '?'}"
                trace_parts.append(f"[本尊:{source or '未知来源'}]")
                logger.debug(
                    f"主题跟踪：自我指代'{self_word}'命中「本尊身份」"
                    f"（候选={names}，来源={source or '未知'}）→ 角色表命中"
                    f"{hit.name or hit.tag or '?'}"
                )
            else:
                current = []
                strength = 0.0
                signal_stamp = stamp
                signal_kind = "self"
                if names:
                    unavailable_reason = (
                        f"自我指代'{self_word}'→本尊身份='{names[0]}' 不在角色表中，"
                        "无法补标准标签；请把该角色加入角色表"
                    )
                    trace_parts.append(f"[本尊:{source or '未知来源'}]")
                    trace_parts.append("[不可用:本尊身份不在角色表]")
                else:
                    unavailable_reason = (
                        f"自我指代'{self_word}'未取到本尊身份："
                        "请填写 general.true_self 或在人格配置里设置 nickname"
                    )
                    trace_parts.append(f"[本尊:{self_word}]")
                    trace_parts.append("[不可用:未取到本尊身份]")
                detail = f"（候选={names}，来源={source or '未知'}）" if names else ""
                logger.debug(
                    f"主题跟踪：自我指代'{self_word}'命中，但"
                    f"{'本尊身份不在角色表' if names else '未取到本尊身份'}，不补{detail}"
                )
            continue

        # ⑤ 代词回指 → 主题**保持不变**（按「粘性」重新计时）；无先行词 → 不可用
        if pronoun:
            if current:
                strength = STRENGTH_STICKY
                signal_stamp = stamp
                signal_kind = "pronoun"
                signal_label = f"代词回指'{pronoun}'→{_names(current)}"
                trace_parts.append(f"[代词:{pronoun}]")
            elif split_word:
                # 拆单还没被下游裁定（1.0.16）：代词只是回指「刚才那件事」，
                # 不该把拆单改写成「代词无先行词」——主题照旧是「不可单独确定」，
                # 仍交 LLM 按当前画面描述判定（别让触发消息里的一句「你」抹掉拆单）。
                trace_parts.append(f"[代词:{pronoun}]")
            else:
                current = []
                unavailable_reason = f"代词'{pronoun}'无先行词（窗口内没有更早的主题）"
                strength = 0.0
                signal_stamp = stamp
                signal_kind = "pronoun"
                trace_parts.append(f"[代词:{pronoun}]")
                trace_parts.append("[不可用:代词无先行词]")
            continue

        # ⑥ 普通闲聊：主题与置信度**都不变**。
        #    这里刻意不按「粘性」刷新时间戳：否则一句「嗯」就能把几小时前的旧主题
        #    复活，min_confidence 这道闸门会形同虚设（测试 C52 守的就是这条）。

    # trace 收尾：主题名不在最后一段里时补上（与 README 的日志样例一致：
    # 爱莉希雅(3h前) → [本尊:人格配置] → 花火）
    if current:
        last = trace_parts[-1] if trace_parts else ""
        missing = [
            entry
            for entry in current
            if (entry.name or entry.tag) and (entry.name or entry.tag) not in last
        ]
        if missing:
            trace_parts.append(_names(current))
    trace = " → ".join(trace_parts)

    delta_minutes = 0.0
    if signal_stamp is not None:
        delta_minutes = max(0.0, (trigger_stamp - signal_stamp) / 60.0)
    decay = (
        1.0 if half_life_minutes <= 0 else math.exp(-delta_minutes / half_life_minutes)
    )
    confidence = strength * decay

    entries: list[CharacterEntry]
    if unavailable_reason:
        status = "unavailable"
        entries = []
        confidence = 0.0
        reason = unavailable_reason
    elif split_word:
        # 拆单（1.0.16）：主题「不可单独确定」—— 不设主题、不给置信度，
        # 交下游按**当前画面描述**由 LLM 判定（判定不出就不补）。
        # 放在 unavailable 之后：拆单自己会清掉 unavailable，而拆单之后的
        # 「本尊身份取不到」等消息会清掉拆单 —— 两条路都是「最后一条消息生效」。
        status = "split"
        entries = []
        confidence = 0.0
        reason = (
            f"拆单（一单多件）：同一条消息里既有自我指代'{split_word}'"
            f"又有角色名'{split_names}'，需按当前画面描述交 LLM 判定，判定不出就不补"
        )
    elif not current:
        status = "empty"
        entries = []
        confidence = 0.0
        reason = (
            f"窗口内 {user_count} 条用户消息都没有主题信号"
            "（点名 / 本尊身份 / 代词都没有）"
        )
    elif confidence < min_confidence:
        status = "low_confidence"
        entries = list(current)
        reason = (
            f"置信度 {confidence:.2f} < {min_confidence:g}"
            f"（信号={signal_label or '无'}，{age_label(delta_minutes)}）"
        )
    else:
        status = "ok"
        entries = list(current)
        reason = ""

    track = SubjectTrack(
        entries=entries,
        confidence=confidence,
        trace=trace,
        status=status,
        reason=reason,
        ambiguous=ambiguous and bool(current),
        signal_kind=signal_kind,
        signal_label=signal_label,
        signal_stamp=signal_stamp,
        self_source=self_source,
        attributed=attributed,
        scanned=len(scoped),
        window=size,
        min_confidence=min_confidence,
    )
    track.basis = format_basis(track, self_matched)
    if track.split:
        logger.debug(
            "主题跟踪命中「拆单（一单多件）」：同一条消息里既有自我指代又有角色名，"
            "本轮主题不可单独确定 —— 交 LLM 按当前画面描述判定，判定不出就不补"
        )
    if track.ambiguous:
        logger.debug(
            "主题跟踪命中「换人还是追加不确定」："
            f"与上一条点名消息间隔 < {multi_window_minutes:g} 分钟且本条没有追加/改口标记，"
            "交 LLM 仲裁（LLM 无结论时用主题跟踪结果）"
        )
    return track


def format_basis(track: SubjectTrack, self_matched: str = "") -> str:
    """拼出完整的判定依据文案（会原样进 DEBUG 日志与一致性检测的 WARNING）。

    - 可用：``判定依据=主题跟踪:本尊身份'花火'(来源=配置)→角色表命中:花火（置信度 0.95）``
      / ``判定依据=主题跟踪:用户点名→伊甸（置信度 0.98）``；
    - 不可用 / 置信度不足 / 无信号 / **拆单**：``判定依据=无(不补)（主题跟踪:<具体原因>）``
      —— 拆单（1.0.16）时原因里会写明「拆单（一单多件）」，交下游按当前画面由 LLM 判定。

    「本尊身份」「(来源=…)」「角色表命中」这几个字段是 1.0.13 起就有的诊断文案，
    用户排查时按它们找，因此 1.0.14 原样保留。
    """
    if track.status != "ok":
        return f"判定依据=无(不补)（主题跟踪:{track.reason}）"
    if track.signal_kind == "self":
        basis = (
            f"判定依据=主题跟踪:本尊身份'{self_matched or track.subject_names()}'"
            f"(来源={track.self_source or '未知来源'})"
            f"→角色表命中:{track.subject_names()}（置信度 {track.confidence:.2f}）"
        )
    else:
        basis = (
            f"判定依据=主题跟踪:{track.signal_label}（置信度 {track.confidence:.2f}）"
        )
    if track.ambiguous:
        basis += "；换人还是追加不确定，交 LLM 仲裁"
    return basis
