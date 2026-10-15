"""char_prompt_guard 事件处理器。

订阅 ``before_action_call`` 事件，在生图 Action 真正执行之前检查提示词：

- 提示词里已经带有角色 → 放行；
- 没有角色 → 按「角色表快速匹配 → LLM 兜底提取」的顺序从最近对话上文中
  找出当前指代的角色，补入提示词；多角色同框时按配置补齐多个。
- 「这次该画谁」由**主题跟踪器**（:func:`monitor.track_subject`，1.0.14 起）给出：
  按时间正序扫最近 ``monitor.window`` 条消息，逐条演进一个「当前主题」
  （改口 / 多人 / 点名 / 本尊身份 / 代词回指 / 拆单），置信度 = 信号强度 × 时间衰减，
  低于 ``monitor.min_confidence`` 就不补。``monitor.enabled = false`` 时**完全退回
  1.0.13** 的「自我指代扫描（②.5）+ 用户点名/主角粘性（③）」链路（见
  :meth:`CharacterGuardHandler._collect_candidates` 里的 else 分支）。
- **拆单**（1.0.16 起）：同一条用户消息里既有自我指代词又有角色表内角色时（调用方
  把一单拆成两张图：先看本尊、再看壳），主题「不可单独确定」—— 这是唯一允许 LLM
  压过主题的情形：强制问一次 LLM，按**当前画面描述**判定；判定不出就不补。

不同生图插件的提示词语义不同，因此按「目标 Action」区分追加格式：

- ``natural``：提示词是自然语言，由生图插件自己再翻译成标签。
  适用于 nai_artist（``share_photo`` / ``share_drawing`` / 旧版 ``share_visual``
  的 ``content`` 参数）。
- ``tags``：提示词本身就是 NAI 标签串，没有翻译步骤，必须追加标准角色标签
  （如 ``elysia \\(honkai impact 3rd\\)``）。适用于 image_generator_plugin-neo
  （``draw_image`` 的 ``content_description`` 参数）；2 人及以上优先写入其
  ``characters`` 独立角色槽位参数。

主模型可用 ``#multi#`` 等标记临时改变行为，标记会被自动移除，不会进入画面。

对 args 的修改会被 ActionManager 回写并替换实际传给 Action 的参数，
因此补全发生在生图与提示词翻译之前。
"""

from __future__ import annotations

import importlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.message_api import (
    build_readable_messages_to_str,
    get_recent_messages,
)
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from .character import (
    BASIS_USER_NAMED,
    CharacterEntry,
    NamingDecision,
    character_in_texts,
    content_contains_tag,
    entry_key,
    extract_characters_via_llm,
    find_characters_in_history,
    find_roster_identity,
    find_self_reference_word,
    is_known_bot_message,
    iter_config_entries,
    merge_entries,
    message_text,
    normalize_prompt_text,
    order_messages_newest_first,
    parse_roster,
    replace_character_reference,
    resolve_naming_decision,
    strip_quoted_blocks,
)
from .config import CharPromptGuardConfig
from .monitor import SubjectTrack, track_subject

logger = get_logger("char_prompt_guard")

APPEND_MODE_NATURAL = "natural"
APPEND_MODE_TAGS = "tags"

STRATEGY_CHARACTERS = "characters"
STRATEGY_APPEND_TAGS = "append_tags"

SCOPE_MULTI_ONLY = "multi_only"

# 主模型可用标记（会自动从提示词中移除，不进入画面）
MARKER_MULTI = "#multi#"
MARKER_SOLO = "#solo#"
MARKER_CHARS = "#chars#"
MARKER_TAGS = "#tags#"
MARKER_NOCHAR = "#nochar#"

_MARKER_PATTERN = re.compile(r"#(?:multi|solo|chars|tags|nochar)#", re.IGNORECASE)

# 标记优先级：同一类标记同时出现时按此取一个，避免"谁写在后面谁生效"的
# 不可预期行为。#nochar# 是一票否决；人数标记里 #solo# 更保守、压过 #multi#；
# 注入方式标记里 #chars#（独立角色槽位）比 #tags# 更具体。
_MARKER_PRIORITY: dict[str, int] = {
    MARKER_NOCHAR: 50,
    MARKER_SOLO: 40,
    MARKER_MULTI: 30,
    MARKER_CHARS: 20,
    MARKER_TAGS: 10,
}
_MARKER_STRATEGY: dict[str, str] = {
    MARKER_CHARS: STRATEGY_CHARACTERS,
    MARKER_TAGS: STRATEGY_APPEND_TAGS,
}
_MARKER_MULTI_GROUP: tuple[str, ...] = (MARKER_SOLO, MARKER_MULTI)
_MARKER_STRATEGY_GROUP: tuple[str, ...] = (MARKER_CHARS, MARKER_TAGS)

# agent 模式的聊天器（default_chatter / neo_default_chatter）把一个 Action 当
# **工具**调用（工具名形如 ``action-share_photo``），那条路径发的是
# ``before_tool_call`` / ``tool_name``，而不是 ``before_action_call`` /
# ``action_name``。只订阅前者的话，守卫在 agent 聊天器下永远收不到通知。
# 两个都订阅，两条路径都能覆盖（重复触发是幂等的：第二次会因"已含角色"放行）。
_TOOL_CALL_EVENT = getattr(EventType, "BEFORE_TOOL_CALL", None)
_SUBSCRIBE_EVENTS: list[EventType | str] = [EventType.BEFORE_ACTION_CALL]
if _TOOL_CALL_EVENT is not None:
    _SUBSCRIBE_EVENTS.append(_TOOL_CALL_EVENT)

#: 工具名相对 Action 名多出来的前缀（``action-share_photo`` → ``share_photo``）
_TOOL_NAME_PREFIX = "action-"
#: 工具组件自己的前缀（``tool-xxx`` → ``xxx``）
_COMPONENT_NAME_PREFIX = "tool-"

#: 自我指代最多回看「触发之前最近几条**用户**消息」（1.0.12）。
#:
#: 实际上限取 ``min(multi.recent_messages, 这个值)``。为什么不能只看触发消息：
#: 实测线上事故（12:14~12:15 同一会话）里，用户 12:14:39 说「那你再来图，要你自己～」，
#: 20 秒后（12:14:59）用一句「嗯哼？」触发了生图 —— 触发消息本身不含自我指代词，
#: 只看它就会按「主角粘性」沿用更早（12:12 引用块里）提到的「爱莉希雅」，把画面补错。
#: 上限取 4 而不是无限往回翻：再往前容易把别的话题里的「本尊」翻出来，
#: 反而压制掉正常的角色点名（``multi.recent_messages`` 默认 6，故默认回看 4 条）。
_SELF_REFERENCE_SCAN_LIMIT = 4


@dataclass(frozen=True)
class TargetSpec:
    """一个被接管的生图 Action。

    Attributes:
        plugin: 所属插件名；为空表示不限制插件
        action: 组件名（Action 的 name）
        prompt_arg: 承载提示词的参数名
        append_mode: 追加模式，``natural`` 或 ``tags``
        mode_arg: 需要按 ``general.apply_modes`` 过滤的参数名（如 nai_artist 的 mode）
        detect_args: 额外参与"是否已含角色"判断的参数名
        characters_arg: 独立角色槽位参数名（tags 模式多人时使用）
    """

    plugin: str
    action: str
    prompt_arg: str
    append_mode: str = APPEND_MODE_NATURAL
    mode_arg: str = ""
    detect_args: tuple[str, ...] = ()
    characters_arg: str = ""


@dataclass
class BotControl:
    """主模型通过标记给出的运行时指令。"""

    force_multi: bool | None = None
    force_strategy: str = ""
    nochar: bool = False
    markers_found: list[str] = field(default_factory=list)


@dataclass
class CandidateResult:
    """一次判定（「这次该补谁」）的完整结果。

    Attributes:
        candidates: 待补入的候选角色（已过滤掉提示词里已有的）
        decided: **判定集合** —— 本次判定认为画面里应该有的角色（不过滤、不截断），
            供二次一致性检测对账
        llm_multi: LLM 判定的「是否需要多人同框」
        mentioned: 上文中被提及的角色表内角色数（过滤前，供 multi_only 判断）
        naming: 确定性的「主题跟踪 / 用户点名·粘性」判定结果（1.0.14 起由主题跟踪给出，
            见 :meth:`monitor.SubjectTrack.naming`）
        basis: 判定依据的可读文案（进日志与 WARNING）
    """

    candidates: list[CharacterEntry] = field(default_factory=list)
    decided: list[CharacterEntry] = field(default_factory=list)
    llm_multi: bool = False
    mentioned: int = 0
    naming: NamingDecision = field(default_factory=NamingDecision)
    basis: str = ""


_BUILTIN_TARGETS: tuple[TargetSpec, ...] = (
    # nai_artist 1.3.6+：旧的单个 share_visual 已拆成两个动作，都没有 mode 参数，
    # 动作名本身已区分「自拍 / 画作」，因此这里的模式过滤交给动作名，不再用 mode_arg。
    TargetSpec(
        plugin="nai_artist",
        action="share_photo",
        prompt_arg="content",
        append_mode=APPEND_MODE_NATURAL,
    ),
    TargetSpec(
        plugin="nai_artist",
        action="share_drawing",
        prompt_arg="content",
        append_mode=APPEND_MODE_NATURAL,
    ),
    # nai_artist 1.2.x 及更早：单动作 + mode 参数（drawing/photo），保留兼容
    TargetSpec(
        plugin="nai_artist",
        action="share_visual",
        prompt_arg="content",
        append_mode=APPEND_MODE_NATURAL,
        mode_arg="mode",
    ),
    # image_generator_plugin-neo：content_description 本身就是最终 NAI 标签串
    TargetSpec(
        plugin="image_generator_plugin-neo",
        action="draw_image",
        prompt_arg="content_description",
        append_mode=APPEND_MODE_TAGS,
        detect_args=("characters",),
        characters_arg="characters",
    ),
)


def parse_targets(raw_text: str) -> list[TargetSpec]:
    """解析目标 Action 表。

    每段格式：``插件名:action:动作名 | 提示词参数名 | 追加模式 | 模式参数 | 额外检测参数 | 角色槽位参数``。
    第 2 段必填，其余可省略。未配置时返回内置默认目标。
    """
    entries = iter_config_entries(raw_text)
    if not entries:
        return list(_BUILTIN_TARGETS)

    targets: list[TargetSpec] = []
    for entry in entries:
        parts = [part.strip() for part in entry.replace("｜", "|").split("|")]
        if not parts or not parts[0]:
            continue
        plugin, action = _split_target_signature(parts[0])
        if not action:
            logger.warning(f"目标 Action 配置缺少动作名，已忽略: {entry}")
            continue
        prompt_arg = parts[1] if len(parts) > 1 else ""
        if not prompt_arg:
            logger.warning(f"目标 Action 配置缺少提示词参数名，已忽略: {entry}")
            continue
        append_mode = (parts[2] if len(parts) > 2 else "").strip().lower()
        if append_mode not in {APPEND_MODE_NATURAL, APPEND_MODE_TAGS}:
            if append_mode:
                logger.warning(
                    f"未知追加模式 {append_mode!r}，按 {APPEND_MODE_NATURAL} 处理"
                )
            append_mode = APPEND_MODE_NATURAL
        mode_arg = parts[3] if len(parts) > 3 else ""
        detect_args: tuple[str, ...] = ()
        if len(parts) > 4:
            detect_args = tuple(
                name.strip()
                for name in parts[4].replace("，", ",").split(",")
                if name.strip()
            )
        characters_arg = parts[5] if len(parts) > 5 else ""
        targets.append(
            TargetSpec(
                plugin=plugin,
                action=action,
                prompt_arg=prompt_arg,
                append_mode=append_mode,
                mode_arg=mode_arg,
                detect_args=detect_args,
                characters_arg=characters_arg,
            )
        )
    return targets or list(_BUILTIN_TARGETS)


def _split_target_signature(signature: str) -> tuple[str, str]:
    """从 ``插件名:action:动作名`` 中拆出插件名与动作名。

    兼容三种形态：

    - ``插件名:action:动作名``：框架注册表给出的标准签名；
    - ``动作名``：只写了动作名的配置行；
    - ``插件名:action:``：发布方漏填动作名（例如只读 ``action_name`` 属性、
      而组件是用新版 ``name`` 声明的），此时动作名为空串，由调用方走参数名兜底。
    """
    raw = (signature or "").strip()
    if not raw:
        return "", ""
    parts = [part.strip() for part in raw.split(":")]
    if len(parts) >= 3:
        return parts[0], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


def _normalize_component_name(raw_name: str) -> str:
    """去掉工具名可能带的 ``action-`` / ``tool-`` 前缀。"""
    for prefix in (_TOOL_NAME_PREFIX, _COMPONENT_NAME_PREFIX):
        if raw_name.startswith(prefix):
            return raw_name[len(prefix) :]
    return raw_name


def resolve_stream_id(params: dict[str, Any]) -> str:
    """从事件参数里解析会话 ID（读上文必须靠它）。

    优先级：

    1. 发布方直接给的 ``params["stream_id"]``（本地补丁二版会带）；
    2. ``message.stream_id``（官方发布点只带 message）；
    3. ``message.chat_stream.stream_id`` / ``message.stream.stream_id``（个别构造方式）。

    agent 编排器自己发起工具调用时，``message`` 可能是 None，若发布方又没带
    ``stream_id``，这里只能返回空串 —— 此时读不到上文，插件会打 WARNING 说明，
    而不是静默地"提取不出角色"。
    """
    raw = str(params.get("stream_id") or "").strip()
    if raw:
        return raw
    message = params.get("message")
    if message is None:
        return ""
    for attr in ("stream_id", "chat_stream_id"):
        value = str(getattr(message, attr, "") or "").strip()
        if value:
            return value
    for holder in ("chat_stream", "stream"):
        value = str(
            getattr(getattr(message, holder, None), "stream_id", "") or ""
        ).strip()
        if value:
            return value
    return ""


def _enrich_from_roster(
    entry: CharacterEntry, roster: list[CharacterEntry]
) -> CharacterEntry:
    """用角色表补全 LLM 提取结果里缺失的字段（主要是游戏名与标准标签）。

    LLM 有时只给角色名，导致追加文案退化成"（画面角色：“爱莉希雅”）"——没有游戏名。
    角色表里若已有同一角色的条目，就把游戏名与标签借过来。

    注意不能直接用 ``entry_key`` 比较：它优先取标签，而 LLM 往往只给名字
    （键不同就匹配不上），所以这里按「标签 / 名字 / 别名」三个维度分别比对。
    """
    if not roster:
        return entry
    name = normalize_prompt_text(entry.name)
    tag = normalize_prompt_text(entry.tag)
    for known in roster:
        known_name = normalize_prompt_text(known.name)
        known_tag = normalize_prompt_text(known.tag)
        same = bool(tag and known_tag and tag == known_tag)
        if not same and name and known_name and name == known_name:
            same = True
        if not same and name:
            # 角色表里的别名与 LLM 给的名字对上（如 elysia ↔ 爱莉希雅）
            same = any(
                normalize_prompt_text(alias) == name for alias in known.aliases if alias
            )
        if not same and tag and known.name:
            same = tag == known_name
        if not same:
            continue
        return CharacterEntry(
            name=entry.name or known.name,
            game=known.game or entry.game,
            aliases=entry.aliases or known.aliases,
            tag=known.tag or entry.tag,
        )
    return entry


def _match_target_by_args(
    params: dict[str, Any], targets: list[TargetSpec], plugin: str
) -> TargetSpec | None:
    """动作名缺失时的兜底匹配：按「提示词参数名」唯一命中。

    典型场景是框架补丁只把旧的 ``action_name`` 属性写进事件，而组件是用新版
    ``name`` 声明的（如 image_generator_plugin-neo 的 ``draw_image``），于是
    ``action_name`` 与签名里的动作名都是空的。此时退一步：谁的提示词参数真的
    出现在 args 里，就认谁。

    多个目标共用同一个参数名是常态（nai_artist 1.3.6 的 ``share_photo`` /
    ``share_drawing`` 与旧版 ``share_visual`` 都用 ``content``），所以不能一见
    多个候选就放弃，按下面三步收窄，仍然无法区分时才宁可不处理：

    1. 带 ``mode_arg`` 的目标只有在 args 里真有这个参数时才可能是它（旧版
       nai_artist 的 ``share_visual`` 一定有 ``mode``）；
    2. 若仍剩多个，优先取"更具体"的那个（声明了 ``mode_arg`` 且 args 里存在）；
    3. 最后按「注入语义」去重：``prompt_arg`` / ``append_mode`` / ``mode_arg`` /
       ``characters_arg`` 完全一致的目标，补入行为没有区别，取配置里靠前的那个。
    """
    args = params.get("args")
    if not isinstance(args, dict):
        return None
    candidates = [
        spec
        for spec in targets
        if (not spec.plugin or not plugin or spec.plugin == plugin)
        and spec.prompt_arg in args
    ]
    # 第 1 步：声明的模式参数不存在 → 这个目标不可能是当前动作
    applicable = [
        spec for spec in candidates if not spec.mode_arg or spec.mode_arg in args
    ]
    if applicable:
        candidates = applicable
    # 第 2 步：优先"更具体"的目标（带 mode_arg 且 args 里确实有）
    specific = [spec for spec in candidates if spec.mode_arg and spec.mode_arg in args]
    if len(specific) == 1:
        candidates = specific
    if len(candidates) == 1:
        spec = candidates[0]
        logger.debug(
            f"事件未提供动作名，按提示词参数名 {spec.prompt_arg!r} 兜底匹配到 {spec.action}"
        )
        return spec
    # 第 3 步：注入语义完全等价的候选视为同一个，取靠前的
    if candidates:
        shapes = {
            (spec.prompt_arg, spec.append_mode, spec.mode_arg, spec.characters_arg)
            for spec in candidates
        }
        if len(shapes) == 1:
            spec = candidates[0]
            logger.debug(
                f"事件未提供动作名，参数名 {spec.prompt_arg!r} 命中多个语义等价目标 "
                f"{[item.action for item in candidates]}，取 {spec.action}"
            )
            return spec
    if len(candidates) > 1:
        logger.debug(
            f"事件未提供动作名，参数名匹配到多个目标 "
            f"{[spec.action for spec in candidates]}，无法唯一确定，跳过"
        )
    return None


def _match_target(
    params: dict[str, Any], targets: list[TargetSpec]
) -> TargetSpec | None:
    """判断当前事件命中了哪个目标 Action。

    组件若使用 ``name`` 声明动作名，事件的 ``action_name`` 可能为空，
    此时只能依赖 ``signature``，因此两者都要参与匹配。

    工具调用路径（agent 聊天器）给的是 ``tool_name``，且名字带 ``action-``
    前缀（如 ``action-share_visual``），这里一并归一化后再比对。
    """
    signature_plugin, signature_action = _split_target_signature(
        str(params.get("signature") or "")
    )
    signature_action = _normalize_component_name(signature_action)
    raw_name = _normalize_component_name(
        str(params.get("action_name") or params.get("tool_name") or "").strip()
    )
    action = signature_action or raw_name
    plugin = signature_plugin
    if not action:
        # 发布方漏填动作名：按参数名兜底，避免插件在 agent 路径下静默失效
        return _match_target_by_args(params, targets, plugin)
    for spec in targets:
        if spec.action != action:
            continue
        if spec.plugin and plugin and spec.plugin != plugin:
            continue
        if spec.plugin and not plugin:
            # 事件里没带插件名时只能按动作名匹配，可能撞上别插件的同名动作
            logger.debug(
                f"事件未提供插件名，按动作名匹配到 {spec.action}"
                f"（目标声明来自 {spec.plugin}）"
            )
        return spec
    return None


# 解析结果缓存：配置文本 → 解析结果。
# 角色表与目标 Action 表都是静态配置文本，而 before_action_call 每次 Action
# 调用都会触发，重复解析纯属浪费。以文本内容为键，配置一改文本就变、自动失效。
_PARSE_CACHE_LIMIT = 8
_targets_cache: dict[str, list[TargetSpec]] = {}
_roster_cache: dict[str, list[CharacterEntry]] = {}

# 历史消息缓存：stream_id → (写入时间, 消息列表)
_HISTORY_CACHE_LIMIT = 32
_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

# bot 自己的 sender_id 缓存：platform → (写入时间, bot_id 或 None)。
# 用来把「bot 的复述」和「用户的点名」分开：bot 说「好的，画爱莉希雅」不该
# 覆盖用户上一句刚改口的角色。解析不到时返回空集合，判定会退化成
# 「最近提及优先」（见 character.is_known_bot_message 的说明）。
_BOT_ID_CACHE_TTL = 60.0
_bot_id_cache: dict[str, tuple[float, str | None]] = {}


def _cached_parse(key_text: str, cache: dict[str, Any], factory: Any) -> Any:
    """按配置文本缓存解析结果。"""
    key = key_text or ""
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = factory(key)
    if len(cache) >= _PARSE_CACHE_LIMIT:
        cache.clear()
    cache[key] = result
    return result


# ── 「本尊身份」解析（1.0.13）─────────────────────────────────────
#
# 自我指代（本尊 / 你本人 / 你自己 …）要的是「bot 本尊」，而插件看不到聊天器的
# 人设。1.0.12 的做法是一律不补；1.0.13 起给出两个**显式**来源：
#
#   ① ``general.true_self``（填角色表里的名字或别名）—— 填了就以它为准；
#   ② 框架人格配置的 ``personality.nickname``，其后 ``alias_names`` 逐个尝试。
#
# 命中角色表（名字 → 别名 → 标签）→ 正常注入该角色；没命中、或两个来源都取不到
# → 仍然**不补**（宁少勿多，绝不猜别的角色）。
#
# 明确不做的事：**不做外观识别**。不从「白发 / 粉眼 / 和服」这类画面关键词反推
# 角色 —— 误差不可控，角色表也保持现有的 4 列（名字 | 游戏名 | 别名 | 标签）。

#: 本尊身份来源：插件配置 ``general.true_self``
_TRUE_SELF_SOURCE_CONFIG = "配置"
#: 本尊身份来源：框架人格配置 ``personality.nickname`` / ``personality.alias_names``
_TRUE_SELF_SOURCE_PERSONALITY = "人格配置"

#: 读取 core 配置的模块路径（按顺序尝试）。
#:
#: ``src.core.config`` 是框架文档里的公开路径（``from src.core.config import
#: get_core_config``）；后者是它背后的实现模块，包 ``__init__`` 因为环境问题
#: 导入失败时仍有机会读到配置。
_CORE_CONFIG_MODULES: tuple[str, ...] = (
    "src.core.config",
    "src.core.config.core_config",
)


@dataclass(frozen=True)
class TrueSelfIdentity:
    """「本尊身份」解析结果（1.0.13）。

    Attributes:
        candidates: 候选名，按优先级排列（``general.true_self`` 只有 1 个；
            来自人格配置时是 ``nickname`` + 逐个 ``alias_names``），已去空白、去重
        source: 来源说明（:data:`_TRUE_SELF_SOURCE_CONFIG` /
            :data:`_TRUE_SELF_SOURCE_PERSONALITY`）；空串表示没取到任何身份名
    """

    candidates: list[str] = field(default_factory=list)
    source: str = ""


def _personality_section_from_plugin_api() -> Any | None:
    """尝试用框架的插件 API（``config_api``）读 core 的 ``personality`` 节。

    实测 ``src/app/plugin_system/api/config_api.py``（API_VERSION 1.0.0）**只提供
    插件配置**的加载与查询 —— ``load_config`` / ``reload_config`` / ``get_config`` /
    ``remove_config`` / ``get_loaded_plugins`` / ``initialize_all_configs``，
    **没有**读 core 配置（``config/core.toml``）的入口。所以这里只能试一下
    ``get_config("core")``：core 配置不经过 ConfigManager（它按插件名缓存），
    正常会返回 ``None``，此时由调用方退回 :func:`_personality_section_from_core_config`。

    整个函数用 ``importlib`` 动态导入 + try/except 包住：**任何失败都返回 None**，
    不抛异常，也不把 config_api 变成插件的硬依赖。
    """
    try:
        module = importlib.import_module("src.app.plugin_system.api.config_api")
    except Exception as e:  # noqa: BLE001 —— 拿不到就换下一条路径
        logger.debug(f"config_api 不可用，改读 core 配置对象: {e}")
        return None
    get_config = getattr(module, "get_config", None)
    if get_config is None:
        return None
    try:
        config = get_config("core")
    except Exception as e:  # noqa: BLE001 —— 同上，失败即换路径
        logger.debug(f"config_api.get_config('core') 调用失败: {e}")
        return None
    return getattr(config, "personality", None)


def _personality_section_from_core_config() -> Any | None:
    """直接读框架的 core 配置对象，取出 ``personality`` 节。

    走 ``src.core.config.get_core_config()``（core 配置未初始化时它抛
    ``RuntimeError``，被测/未启动运行时的环境就是这样）。同样用动态导入 +
    try/except 包住：**任何失败都返回 None**，只留一条 DEBUG 说明原因。
    """
    for module_name in _CORE_CONFIG_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception as e:  # noqa: BLE001 —— 该路径不可用就试下一个
            logger.debug(f"core 配置模块 {module_name} 不可用: {e}")
            continue
        get_core_config = getattr(module, "get_core_config", None)
        if get_core_config is None:
            continue
        try:
            config = get_core_config()
        except Exception as e:  # noqa: BLE001 —— 未初始化等情况：本次没有本尊身份
            logger.debug(f"读取 core 配置失败，本次按「未取到本尊身份」处理: {e}")
            return None
        section = getattr(config, "personality", None)
        if section is not None:
            return section
    return None


def _normalize_identity_names(raw: Any) -> list[str]:
    """把「本尊身份」候选名归一化成「去空白、去重、保序」的字符串列表。

    约定 :func:`_read_personality_identity` 返回 ``list[str]``；这里额外兼容裸字符串
    （以及元组/集合/None）：人格配置里 ``nickname`` 是字符串、``alias_names`` 是列表，
    不同核心版本或人工改写的配置文件都可能给成别的形状，**任何形状都不能让判定崩掉**。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        return []
    names: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in names:
            continue
        names.append(text)
    return names


def _read_personality_identity() -> list[str]:
    """读取框架人格配置里的「本尊身份」候选名（nickname → alias_names）。

    优先级与来源：

    1. 插件 API ``config_api``（见 :func:`_personality_section_from_plugin_api`，
       实测它没有 core 配置入口，正常取不到）；
    2. core 配置对象 ``personality.nickname``（框架默认值是 ``小狐狸``）；
    3. 同一个 ``personality`` 节的 ``alias_names``（逐个尝试）。

    ``nickname`` 为空、或只有 ``alias_names`` 时，仍然返回其中非空的那部分。

    **任何失败（模块不存在、配置未初始化、字段缺失、类型不对）都返回空列表**，
    绝不抛异常 —— 它只是「有没有本尊身份」的一个来源，失败时判定退回
    「不补 + 日志提示」，不影响生图。
    """
    for get_section in (
        _personality_section_from_plugin_api,
        _personality_section_from_core_config,
    ):
        try:
            section = get_section()
        except Exception as e:  # noqa: BLE001 —— 双保险：读取函数本身也不许抛
            logger.debug(f"读取人格配置失败: {e}")
            continue
        if section is None:
            continue
        names = _normalize_identity_names(getattr(section, "nickname", ""))
        for alias in _normalize_identity_names(getattr(section, "alias_names", None)):
            if alias not in names:
                names.append(alias)
        if names:
            logger.debug(f"人格配置提供本尊身份候选：{names}")
            return names
    return []


def resolve_true_self_identity(config: CharPromptGuardConfig) -> TrueSelfIdentity:
    """按优先级解析「本尊身份」候选名。

    优先级：``general.true_self``（非空）→ 框架人格配置 ``personality.nickname``
    → ``personality.alias_names``（逐个尝试）。

    本函数只负责「取名字」，**不判断名字在不在角色表里** —— 命中与否由
    :func:`character.find_roster_identity` 在调用处判断，日志里要能区分
    「命中并补入」与「不在角色表中，不补」两种情况。
    """
    configured = str(
        getattr(getattr(config, "general", None), "true_self", "") or ""
    ).strip()
    if configured:
        return TrueSelfIdentity(
            candidates=[configured], source=_TRUE_SELF_SOURCE_CONFIG
        )
    candidates = _normalize_identity_names(_read_personality_identity())
    if candidates:
        return TrueSelfIdentity(
            candidates=candidates, source=_TRUE_SELF_SOURCE_PERSONALITY
        )
    return TrueSelfIdentity()


class CharacterGuardHandler(BaseEventHandler):
    """生图前角色提示词检测与补全处理器。"""

    name: str = "char_prompt_guard"
    description: str = (
        "在生图 Action 执行前检测角色提示词：已含角色则放行，"
        "否则根据上文补入角色名+游戏名（或 NAI 角色标签），支持多角色同框"
    )
    weight: int = 100
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = _SUBSCRIBE_EVENTS
    # LLM 提取可能较慢，禁用订阅者级超时保护
    timeout: float = 0.0

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 before_action_call 事件。"""
        config = self._config()
        if not config.general.enabled or not config.targets.enabled:
            return EventDecision.SUCCESS, params

        targets = _cached_parse(config.targets.list, _targets_cache, parse_targets)
        spec = _match_target(params, targets)
        if spec is None:
            # 未命中目标不是异常，但静默会让「插件装了却没反应」无从排查，
            # 所以这里留一条 DEBUG：把它和「命中了但放行」区分开。
            logger.debug(
                "未命中任何目标动作，放行"
                f"（签名={params.get('signature')!r}, "
                f"动作名={(params.get('action_name') or params.get('tool_name'))!r}, "
                f"已配置目标={[item.action for item in targets]}）"
            )
            return EventDecision.SUCCESS, params

        args = params.get("args")
        if not isinstance(args, dict):
            return EventDecision.SUCCESS, params

        raw_content = args.get(spec.prompt_arg)
        if not isinstance(raw_content, str) or not raw_content.strip():
            if spec.prompt_arg not in args:
                # 目标匹配上了，但配置里声明的参数名不在本次调用里 —— 多半是
                # [targets] 配置写错了（例如把 neo 的 content_description 填给了
                # nai_artist）。这里只告警不写回，避免给动作塞进未知参数导致报错。
                logger.warning(
                    f"目标 {spec.action} 声明的提示词参数 {spec.prompt_arg!r} 不在本次调用参数里"
                    f"（实际参数: {sorted(args)}），本次放行不补角色；请检查 [targets] 配置"
                )
            return EventDecision.SUCCESS, params

        if spec.mode_arg and not self._mode_allowed(args.get(spec.mode_arg), config):
            logger.debug(f"模式 {args.get(spec.mode_arg)} 不在生效范围，放行")
            return EventDecision.SUCCESS, params

        # 主模型运行时指令（标记会从提示词里移除）
        control, content = self._extract_control(raw_content, config)
        if control.nochar:
            if content != raw_content:
                args[spec.prompt_arg] = content
                params["args"] = args
            logger.info("主模型标记 #nochar#，本次不补角色")
            return EventDecision.SUCCESS, params

        roster = (
            _cached_parse(config.roster.characters, _roster_cache, parse_roster)
            if config.roster.enabled
            else []
        )
        texts = self._detection_texts(content, spec, args)
        present_keys = {
            entry_key(entry) for entry in roster if character_in_texts(entry, texts)
        }

        # 候选角色：用户点名/粘性 → LLM 仲裁（必要时）
        result = await self._collect_candidates(params, config, roster, content, texts)
        candidates = result.candidates

        # 标签补全：描述里已经点名了角色表内的角色、但缺少该角色的标准标签时补标签。
        # 与"要不要新补一个角色"无关，所以放在最前面独立处理。
        # 这是实测逼出来的：描述里写着"花火"，下游 nai_artist 的翻译模型却把它翻成
        # ``hanabi (firefly) (honkai: star rail)``（括号未转义 + 串了另一个角色），
        # 补上转义正确的标准标签后，NAI 才能稳定锁定角色。
        completion_tags = self._completion_tags(content, roster, config)

        multi = self._resolve_multi(
            config,
            control,
            candidates,
            result.llm_multi,
            result.mentioned,
            result.naming.multi,
        )
        if not multi:
            # 单角色路径：沿用"命中任一角色即整体放行"
            if present_keys:
                final_content = (
                    self._complete(spec, content, completion_tags)
                    if completion_tags
                    else content
                )
                if not completion_tags:
                    logger.debug("提示词已包含角色，放行")
                return self._finalize(
                    params=params,
                    args=args,
                    spec=spec,
                    original=raw_content,
                    final_content=final_content,
                    decided=result.decided,
                    roster=roster,
                    config=config,
                    basis=result.basis,
                )
            target_characters = candidates[:1]
        else:
            target_characters = [
                entry for entry in candidates if entry_key(entry) not in present_keys
            ][: max(1, int(config.multi.max_characters))]

        if not target_characters:
            final_content = (
                self._complete(spec, content, completion_tags)
                if completion_tags
                else content
            )
            if not completion_tags:
                logger.debug("未识别到需要补入的角色，放行")
            return self._finalize(
                params=params,
                args=args,
                spec=spec,
                original=raw_content,
                final_content=final_content,
                decided=result.decided,
                roster=roster,
                config=config,
                basis=result.basis,
            )

        new_content = self._apply(
            args=args,
            spec=spec,
            content=content,
            characters=target_characters,
            config=config,
            control=control,
        )
        if new_content is None:
            return self._finalize(
                params=params,
                args=args,
                spec=spec,
                original=raw_content,
                final_content=content,
                decided=result.decided,
                roster=roster,
                config=config,
                basis=result.basis,
            )

        names = ", ".join(entry.name or entry.tag or "?" for entry in target_characters)
        # 把"追加了什么"直接写进日志：用户最容易困惑的就是"到底补了谁、有没有游戏名"
        appended = new_content[len(content) :].strip()
        logger.info(
            f"已补入角色（{spec.action} / {spec.append_mode}"
            f"{' / 多人' if multi else ''}）: {names}"
            f"{f' → 追加 {appended!r}' if appended else ''}"
        )
        return self._finalize(
            params=params,
            args=args,
            spec=spec,
            original=raw_content,
            final_content=new_content,
            decided=result.decided,
            roster=roster,
            config=config,
            basis=result.basis,
        )

    # ── 配置与检测辅助 ────────────────────────────────────────────

    def _config(self) -> CharPromptGuardConfig:
        """获取插件配置，缺失时回退默认配置。"""
        config = self.plugin.config
        if isinstance(config, CharPromptGuardConfig):
            return config
        return CharPromptGuardConfig()

    @staticmethod
    def _extract_control(
        content: str, config: CharPromptGuardConfig
    ) -> tuple[BotControl, str]:
        """解析并移除主模型标记，返回指令与清理后的提示词。

        同一类标记同时出现时按 :data:`_MARKER_PRIORITY` 取一个并打 WARN，
        而不是"谁写在后面谁生效"。
        """
        control = BotControl()
        cleaned = content
        if config.multi.bot_control:
            found = _MARKER_PATTERN.findall(content)
            if found:
                markers = [marker.lower() for marker in found]
                control.markers_found = markers

                multi_markers = [m for m in markers if m in _MARKER_MULTI_GROUP]
                strategy_markers = [m for m in markers if m in _MARKER_STRATEGY_GROUP]
                if len(multi_markers) > 1 or len(strategy_markers) > 1:
                    logger.warning(f"检测到互相冲突的标记 {markers}，按固定优先级处理")

                if multi_markers:
                    winner = max(multi_markers, key=lambda m: _MARKER_PRIORITY[m])
                    control.force_multi = winner == MARKER_MULTI
                if strategy_markers:
                    winner = max(strategy_markers, key=lambda m: _MARKER_PRIORITY[m])
                    control.force_strategy = _MARKER_STRATEGY[winner]
                if MARKER_NOCHAR in markers:
                    control.nochar = True

                cleaned = _MARKER_PATTERN.sub(" ", content)
                cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
                if not cleaned:
                    # 标记误写导致内容为空时保留原文，避免把画面清空
                    cleaned = content
                    control.nochar = False
        return control, cleaned

    @staticmethod
    def _detection_texts(
        content: str, spec: TargetSpec, args: dict[str, Any]
    ) -> list[str]:
        """汇总用于"是否已含角色"判断的文本。"""
        texts = [content]
        for arg_name in spec.detect_args:
            extra = args.get(arg_name)
            if isinstance(extra, str) and extra.strip():
                texts.append(extra)
        return texts

    @staticmethod
    def _trigger_text(params: dict[str, Any]) -> str:
        """取出触发本次生图的那条消息正文（没有 message 时为空串）。

        ``_collect_context`` 与「自我指代」扫描都要用同一份文本，故抽成一处：
        两处若各取一次，很容易出现「收集上文时用了 A、判定时看了 B」的偏差。
        """
        message = params.get("message")
        if message is None:
            return ""
        return str(
            getattr(message, "processed_plain_text", "")
            or getattr(message, "content", "")
            or ""
        ).strip()

    async def _collect_candidates(
        self,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        content: str,
        texts: list[str],
    ) -> CandidateResult:
        """收集候选角色。

        判定优先级（1.0.10 起，1.0.14 起第二级由主题跟踪承担）：**描述点名 >
        主题跟踪（用户点名/粘性） > LLM 仲裁 > 不补**。

        - 描述点名：画面描述里已经写了角色表内角色 —— 由调用方直接放行/补标签，
          本函数只负责算出「判定集合」，供二次一致性检测对账；
        - **主题跟踪**（1.0.14 起，``monitor.enabled = true``）：把「这次该画谁」交给
          :func:`monitor.track_subject` —— 按时间正序扫最近 ``monitor.window`` 条消息
          维护一个「当前主题」（改口 / 多人 / 点名 / 本尊身份 / 代词回指 / 拆单），置信度 =
          信号强度 × 时间衰减，低于 ``monitor.min_confidence`` 就不补。它压过 LLM 的
          猜测；主题**不可用**（本尊身份不在角色表、取不到身份名、代词无先行词）时
          直接不补且不问 LLM。只有「换人还是追加不确定」（相邻两条点名消息靠得太近）
          才交 LLM 仲裁，与 1.0.12 起的既有语义一致；
        - **拆单**（1.0.16 起，主题跟踪的 ``split``）：同一条用户消息里既有自我指代词
          又有角色表内角色（「本尊看看，然后穿着爱莉来看看～」＝一单两件）时，主题
          「不可单独确定」。这是**唯一**允许 LLM 压过主题的情形：强制问一次 LLM，
          它给出的**角色表内**结论直接采用（日志
          ``判定依据=拆单→LLM 按画面判定:<角色名>``）；LLM 空 / 失败 / 超时 /
          ``use_llm = false`` → **不补**（日志 ``判定依据=无(不补)（拆单且无法按画面判定）``）。
          其它 status（可用 / 不可用 / 置信度不足 / 空）的一切照旧；
        - 自我指代（1.0.12 起；1.0.13 起能定位到角色）：``monitor.enabled = false`` 时
          走这条老链路 —— 按「``general.true_self`` → 人格配置 ``nickname`` →
          ``alias_names``」取本尊身份并在角色表里定位；**命中就补那个角色**，
          没命中或完全取不到身份名才**直接不补**（见 ②.5），不走粘性判定；
        - 用户点名(粘性)：``character.resolve_naming_decision`` 的确定性结论
          （带 ``multi.stickiness_hours`` 时效、换人/追加规则）—— 同样只在
          ``monitor.enabled = false`` 时使用。**它压过 LLM 的猜测** ——
          「用户 21:52 点名流萤、01:03 只说想看看你」时必须是流萤；
        - LLM 仲裁：主题为空 / 置信度不足 / 换人还是追加不确定 / **拆单**时才问（条件见 ④）；
        - 都不成立 → 不补（宁少勿多）。

        Returns:
            :class:`CandidateResult`
        """
        messages = await self._collect_context(params, config)
        recent_window = (
            int(config.multi.recent_messages) if config.multi.recent_only else 0
        )
        window = messages[-recent_window:] if recent_window > 0 else messages

        # ① 上文中被提及的角色表内角色：只用于 multi_only 的「是不是多人场景」判断
        history_candidates: list[CharacterEntry] = []
        seen: set[str] = set()
        if roster:
            for entry in find_characters_in_history(window, roster):
                key = entry_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                history_candidates.append(entry)
        mentioned = len(history_candidates)

        # ② 画面描述里已经点名的「角色表内」角色
        present_keys = {
            entry_key(entry) for entry in roster if character_in_texts(entry, texts)
        }
        # ②.5 要用：描述里到底有没有角色表内角色（判定自我指代的前提之一）
        description_has_character = any(
            character_in_texts(entry, [content]) for entry in roster
        )

        # bot 自己的复述既不算点名、也不算「用户说过本尊」，所以先尽力解析本机 bot 的
        # sender_id（解析不到就退化成「最近提及优先」，见 character.is_known_bot_message）。
        # ②.5 与 ③ 共用这一份结果，只解析一次。
        bot_ids = await self._resolve_bot_sender_ids(window)

        # ② 主题跟踪（1.0.14 起，``monitor.enabled = true`` 时的主判定）：按时间正序
        # 扫最近 ``monitor.window`` 条消息，逐条演进一个「当前主题」，置信度 =
        # 信号强度 × 时间衰减。它取代了 1.0.13 里分散在两处的规则 ——「自我指代扫描
        # （②.5）」与「用户点名/主角粘性（③）」；规则、优先级与可读日志统一在
        # ``monitor.track_subject`` 一处（README「主题跟踪」一节有完整表格）。
        #
        # ``monitor.enabled = false`` 时走下面的 else 分支：1.0.13 的两条链路
        # 一字未改，可逐字节回归（用例 C53）。
        monitor = getattr(config, "monitor", None)
        monitor_on = bool(getattr(monitor, "enabled", False)) and bool(roster)
        track: SubjectTrack | None = None
        naming = NamingDecision()

        if monitor_on:
            try:
                track = track_subject(
                    messages,
                    roster,
                    window=int(getattr(monitor, "window", 15)),
                    min_confidence=float(getattr(monitor, "min_confidence", 0.5)),
                    half_life_minutes=float(
                        getattr(monitor, "half_life_minutes", 45.0)
                    ),
                    multi_window_minutes=float(config.multi.multi_window_minutes),
                    # 自我指代规则与 1.0.13 同一前提：画面描述里已经写明了角色表内角色时，
                    # 「本尊/你自己」不再改变主题（描述点名优先级最高）
                    self_reference_enabled=(
                        bool(config.general.skip_on_self_reference)
                        and not description_has_character
                    ),
                    bot_ids=bot_ids,
                    resolve_identity=lambda: resolve_true_self_identity(config),
                )
            except Exception as e:  # noqa: BLE001 —— 跟踪器自身出问题绝不能拦住生图
                logger.warning(
                    f"主题跟踪异常，本次退回 1.0.13 判定链路: {type(e).__name__}: {e}"
                )
                track = None
                monitor_on = False
            else:
                logger.debug(track.log_line())
                # 判定依据与主题轨迹一样重要：用户核对「为什么补了/没补」看的就是它
                # （「本尊身份」/「不在角色表」/「置信度不足」等原因都在里面）
                logger.debug(track.basis)
                naming = track.naming()
                if track.unavailable:
                    # 本尊身份不在角色表 / 取不到身份名 / 代词没有先行词：宁可什么都不补，
                    # 也绝不用粘性把更早的旧角色捡回来（1.0.12 起一直守的结论）。
                    # 这里**不问 LLM**：问它同样只能从上文里猜出那个旧角色。
                    return CandidateResult(
                        candidates=[],
                        decided=[],
                        llm_multi=False,
                        mentioned=mentioned,
                        naming=naming,
                        basis=track.basis,
                    )

        if not monitor_on:
            # ②.5 自我指代（1.0.12 起；1.0.13 起能定位到具体角色）：用户要的是
            # 「bot 本尊」，而「本尊是谁」只存在于调用方（聊天器）的内部设定里，
            # 插件看不到。此时按粘性沿用旧点名会补错人（实测：更早用户点名过
            # 「爱莉希雅」，插件就把它补进了「要你自己」的画面）。
            # 1.0.13 起先按「general.true_self → 人格配置 nickname → alias_names」
            # 取本尊身份：命中角色表就**正常注入那个角色**（走既有注入路径），
            # 没命中或完全取不到身份名才不补（也不问 LLM —— 问它同样只能从上文里
            # 猜出那个旧角色）。前提是**描述里没有角色表内角色**：描述里写明了角色
            # 就是另一回事。
            if (
                config.general.skip_on_self_reference
                and roster
                and not description_has_character
            ):
                source, hit, scanned = self._detect_self_reference(
                    window=window,
                    trigger_text=self._trigger_text(params),
                    content=content,
                    bot_ids=bot_ids,
                    config=config,
                )
                if hit:
                    logger.debug(
                        f"自我指代命中：来源={source} 词={hit!r}"
                        f"（焦点范围=画面描述+触发消息+触发前最近 {scanned} 条用户消息）"
                    )
                    return self._resolve_self_reference(
                        config=config,
                        roster=roster,
                        mentioned=mentioned,
                    )

            # ③ 用户点名 / 主角粘性（确定性、零成本）：1.0.13 的主依据。
            naming = resolve_naming_decision(
                window,
                roster,
                stickiness_hours=float(config.multi.stickiness_hours),
                multi_window_minutes=float(config.multi.multi_window_minutes),
                bot_ids=bot_ids,
            )
            logger.debug(naming.label())

        # ④ 决定要不要问 LLM（沿用 1.0.6 起的既有条件，另加「不确定」与「拆单」）：
        #   - llm_mode="always"：显式要求每次都问，可识别角色表**外**的角色；
        #   - 没有任何上文命中：保持原有 LLM 兜底行为；
        #   - description_first 且描述里没有角色表内角色：让读过描述的 LLM 参与；
        #   - 上文出现 ≥2 个角色表内角色：多人场景需要 LLM 仲裁一次；
        #   - 换人还是追加不确定（uncertain / 主题跟踪的 ambiguous）：按规则交 LLM 仲裁；
        #   - **拆单**（1.0.16，主题跟踪的 split）：**强制**问一次，不受上面几条限制。
        # 注意：**问不问 LLM** 与 **听不听 LLM** 是两件事 —— 主题跟踪（或 1.0.13 的用户
        # 点名）有确定结论时，LLM 的表内结论一律让位（见 ⑤），这里问一次只是为了
        # llm_mode="always" 下还能认出角色表外的角色、以及保留 description_first 的
        # 既有排查能力。
        llm_mode = str(config.multi.llm_mode).lower()
        multi_candidates = (
            config.multi.enabled
            and str(config.multi.scope).lower() == SCOPE_MULTI_ONLY
            and mentioned >= 2
        )
        # 拆单（1.0.16）：monitor 开启时由主题跟踪给出 —— 同一条用户消息里既有自我
        # 指代词又有角色表内角色（「本尊看看，然后穿着爱莉来看看～」＝一单两件）。
        # 此时主题**不可单独确定**，且**只有这一种情形**允许 LLM 压过主题：
        # 它读得到本次的**画面描述**，能判断当前这张图是「本尊」还是「壳」。
        split = bool(track is not None and track.split)
        # 「换人还是追加不确定」的来源：monitor 开启时由主题跟踪给出（相邻两条点名
        # 消息间隔 < multi.multi_window_minutes），关闭时沿用 resolve_naming_decision。
        ambiguous = track.ambiguous if track is not None else naming.kind == "uncertain"
        need_llm = config.general.use_llm and (
            split
            or llm_mode == "always"
            or not history_candidates
            or (config.general.description_first and not present_keys)
            or multi_candidates
            or ambiguous
        )

        llm_candidates: list[CharacterEntry] = []
        llm_multi = False
        llm_answered = False
        if need_llm:
            context_text = ""
            try:
                context_text = await build_readable_messages_to_str(
                    window,
                    timestamp_mode="relative",
                )
            except Exception as e:  # noqa: BLE001 —— 上文格式化失败就退回空上下文继续
                logger.debug(f"格式化上文失败: {e}")

            # 这次日志很关键：上文为空时模型必然给不出角色，
            # 之前正是"事件缺 stream_id → 上文只有触发消息"导致误判为插件没生效。
            logger.debug(
                f"调用 LLM 提取角色: 上文 {len(context_text)} 字符 / "
                f"{len(window)} 条消息，描述={content[:40]!r}"
            )
            extraction = await extract_characters_via_llm(
                description=content,
                context_text=context_text,
                roster=roster,
                translate_model=config.general.translate_model,
                timeout=float(config.general.llm_timeout),
            )
            if extraction is None:
                logger.debug("LLM 提取无结果（超时或失败），按用户点名/粘性判定兜底")
            else:
                llm_multi = extraction.multi
                llm_answered = bool(extraction.characters)
                logger.debug(
                    "LLM 提取结果: "
                    f"characters={[entry.name or entry.tag for entry in extraction.characters]} "
                    f"multi={extraction.multi}"
                )
                # 注意：这里**不能**复用上文史命中的 seen 集合。实测踩过：
                # 角色在上文里被提到过 → 已在 seen 里 → LLM 的结果被去重掉 →
                # 列表变空 → 被误判成"角色已在提示词里"，于是什么都不补。
                seen_llm: set[str] = set()
                for entry in extraction.characters:
                    if character_in_texts(entry, texts):
                        # 提示词里已经写了的，LLM 偶尔仍会列出来
                        continue
                    entry = _enrich_from_roster(entry, roster)
                    key = entry_key(entry)
                    if key in seen_llm:
                        continue
                    seen_llm.add(key)
                    llm_candidates.append(entry)

        # ④.5 拆单（1.0.16）：主题跟踪给出的结论是「不可单独确定」（同一条用户消息里
        # 既有自我指代词、又有角色表内角色），**只在这一种情形下**允许 LLM 压过主题 ——
        # 它读得到本次的**画面描述**，而主题跟踪只看对话上文，两者在拆单场景里权重相同
        # （这正是 1.0.15 的事故：「本尊看看，然后穿着爱莉来看看～」被整条判成
        # 「用户点名→爱莉希雅」，把 LLM 按画面正确认出的本尊=花火压掉了）。
        # 只采纳**角色表内**的结论：拆单时主题跟踪给不出任何可核对的候选人，角色表外的
        # 名字无从对账，宁可不补（与 `llm_mode="always"` 的「额外认人」是两回事）。
        # LLM 没结论（空 / 失败 / 超时 / `use_llm = false`）→ 什么都不补。
        if split:
            in_roster = [
                entry
                for entry in llm_candidates
                if any(entry_key(entry) == entry_key(known) for known in roster)
            ]
            if in_roster:
                candidates = list(in_roster)
                decided = list(in_roster)
                basis = (
                    "判定依据=拆单→LLM 按画面判定:"
                    f"{'、'.join(entry.name or entry.tag or '?' for entry in in_roster)}"
                )
            else:
                candidates = []
                decided = []
                basis = "判定依据=无(不补)（拆单且无法按画面判定）"
            logger.debug(basis)
            return CandidateResult(
                candidates=candidates[: max(1, int(config.multi.max_characters))],
                decided=decided,
                llm_multi=llm_multi,
                mentioned=mentioned,
                naming=naming,
                basis=basis,
            )

        # ⑤ 汇总。优先级：主题跟踪（1.0.14）/ 用户点名·粘性（1.0.13）有确定结论时以它
        # 为准 —— LLM 的表内猜测一律让位（这就是 1.0.10 起要修的核心问题：用户点名
        # 流萤，LLM 却猜成爱莉希雅）；只有 llm_mode="always" 这种显式开启的场景，才
        # 额外保留 LLM 认出来的**角色表外**角色（宁少勿多，不让模型的猜测混进画面）。
        # 唯一的例外是「换人还是追加不确定」：相邻两条点名消息靠得太近又没有任何
        # 追加/改口标记时，交 LLM 仲裁 —— 与 1.0.12 起「还是」判语境后的既有行为一致。
        if track is not None and track.usable:
            if track.ambiguous and llm_candidates:
                candidates = list(llm_candidates)
                decided = list(llm_candidates)
                basis = (
                    f"判定依据=LLM 仲裁:"
                    f"{[entry.name or entry.tag or '?' for entry in llm_candidates]}"
                    f" multi={str(llm_multi).lower()}"
                    f"（主题跟踪={track.subject_names()}，但换人/追加不确定）"
                )
                logger.debug(basis)
            else:
                candidates = [
                    entry
                    for entry in track.entries
                    if not character_in_texts(entry, texts)
                ]
                decided = list(track.entries)
                basis = track.basis
                if track.ambiguous:
                    basis = f"{basis}；LLM 无结论，按主题跟踪结果补入"
                outside = (
                    [
                        entry
                        for entry in llm_candidates
                        if not any(
                            entry_key(entry) == entry_key(known) for known in roster
                        )
                    ]
                    if llm_mode == "always"
                    else []
                )
                if outside:
                    logger.debug(
                        "LLM 额外识别出角色表外的角色 "
                        f"{[entry.name or entry.tag for entry in outside]}，一并补入"
                    )
                    candidates = merge_entries(candidates, outside)
        elif naming.kind in {"single", "multi"}:
            candidates = [
                entry
                for entry in naming.characters
                if not character_in_texts(entry, texts)
            ]
            decided = list(naming.characters)
            basis = naming.label()
            outside = (
                [
                    entry
                    for entry in llm_candidates
                    if not any(entry_key(entry) == entry_key(known) for known in roster)
                ]
                if llm_mode == "always"
                else []
            )
            if outside:
                logger.debug(
                    "LLM 额外识别出角色表外的角色 "
                    f"{[entry.name or entry.tag for entry in outside]}，一并补入"
                )
                candidates = merge_entries(candidates, outside)
        elif llm_candidates:
            candidates = list(llm_candidates)
            decided = list(llm_candidates)
            basis = (
                f"判定依据=LLM 仲裁:"
                f"{[entry.name or entry.tag or '?' for entry in llm_candidates]}"
                f" multi={str(llm_multi).lower()}"
            )
            logger.debug(basis)
        elif llm_answered:
            # LLM 答了，只是它给的角色都已经在提示词里 → 画面没问题，本次不补，
            # 绝不回退到上文里的旧角色
            logger.debug("LLM 给出的角色都已在提示词中，无需补入（不回退上文命中）")
            candidates = []
            decided = []
            basis = "判定依据=LLM 仲裁:[]（LLM 认为无需补入）"
            logger.debug(basis)
        elif naming.kind == "uncertain":
            # 不确定且 LLM 无结论/不可用 → 宁少勿多，只补最近那一个
            candidates = [
                entry
                for entry in naming.characters
                if not character_in_texts(entry, texts)
            ]
            decided = list(naming.characters)
            basis = f"{naming.label()}；LLM 无结论，宁少勿多只补最近那一个"
        else:
            # 无信号（没有点名、或点名已超出粘性时效）；monitor 开启时由主题跟踪给出
            # 具体原因（轨迹与判定依据在上面已经打过日志）：窗口内根本没有信号，
            # 还是信号太旧被 min_confidence 挡掉。
            candidates = []
            decided = []
            basis = track.basis if track is not None else naming.label()

        max_candidates = max(1, int(config.multi.max_characters))
        return CandidateResult(
            candidates=candidates[:max_candidates],
            decided=decided,
            llm_multi=llm_multi,
            mentioned=mentioned,
            naming=naming,
            basis=basis,
        )

    @staticmethod
    def _detect_self_reference(
        *,
        window: list[dict[str, Any]],
        trigger_text: str,
        content: str,
        bot_ids: set[str],
        config: CharPromptGuardConfig,
    ) -> tuple[str, str, int]:
        """在「焦点范围」里查找自我指代词（``本尊`` / ``你本人`` / ``你自己`` …）。

        .. note::
           1.0.14 起这条链路只在 ``monitor.enabled = false`` 时使用 —— 默认开启
           **主题跟踪**（``monitor.track_subject``）时，自我指代是状态机里的一条规则
           （主题 = 本尊身份），不再需要这里「按焦点范围找词」的启发式。保留它是为了
           「关掉 monitor 就完全退回 1.0.13」（回归用例 C53）。

        焦点范围（1.0.12）按顺序是：

        1. **画面描述**（``content``）；
        2. **触发本次生图的那条消息**；
        3. **触发之前最近 N 条用户消息**，N =
           ``min(multi.recent_messages, _SELF_REFERENCE_SCAN_LIMIT)``（默认 6 与 4
           取小 → 4 条），从新到旧数，bot 自己的消息跳过、不占名额。

        为什么要这么宽：实测事故（12:14~12:15 同一会话）里用户真正表达意图的是
        12:14:39 的「那你再来图，要你自己～」，而触发消息是 20 秒后的「嗯哼？」，
        画面描述里也没有角色名 —— 只看后两者根本看不到「要本尊」，插件就会按粘性
        把更早（12:12 引用块里）提到的「爱莉希雅」补进画面。N 不放大到整个窗口，
        是为了避免把别的话题里的「本尊」翻出来压制正常的角色点名。

        注意这里用 :func:`character.message_text` 取正文：引用块/媒体块（``[回复…]``）
        里的文字不算用户自己的表达，与点名扫描保持同一口径。

        Returns:
            ``(来源说明, 命中的自我指代词, 实际回看的用户消息条数)``；
            没有命中时前两项为空串。
        """
        sources: list[tuple[str, str]] = [("画面描述", content)]
        if trigger_text:
            sources.append(("触发消息", strip_quoted_blocks(trigger_text)))

        scan_limit = max(
            1, min(int(config.multi.recent_messages), _SELF_REFERENCE_SCAN_LIMIT)
        )
        # _collect_context 只在 trigger_text 非空时才把触发消息追加在末尾，
        # 所以「末尾那条是不是触发消息」可以直接由 trigger_text 判断。
        history_messages = window[:-1] if trigger_text else window
        scanned = 0
        # order_messages_newest_first：窗口理论上已是「旧 → 新」，但时间戳乱序时
        # 必须显式排一次，否则「最近 N 条用户消息」会数错（同 resolve_naming_decision 的口径）
        for message in order_messages_newest_first(history_messages):
            if scanned >= scan_limit:
                break
            if is_known_bot_message(message, bot_ids):
                continue
            scanned += 1
            sources.append((f"触发前第 {scanned} 条用户消息", message_text(message)))

        for source, text in sources:
            hit = find_self_reference_word(text)
            if hit:
                return source, hit, scanned
        return "", "", scanned

    @staticmethod
    def _resolve_self_reference(
        *,
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        mentioned: int,
    ) -> CandidateResult:
        """自我指代命中后的判定：本尊身份 → 角色表命中就补，否则不补（1.0.13）。

        .. note::
           与 :meth:`_detect_self_reference` 一样，1.0.14 起只在
           ``monitor.enabled = false`` 时使用；默认的主题跟踪路径里，同样的
           「本尊身份」定位逻辑在 ``monitor.track_subject`` 的自我指代分支，
           日志字段（``本尊身份`` / ``(来源=…)`` / ``角色表命中`` / ``不在角色表中``）
           与这里保持一致。

        流程（对应 README「本尊/自我指代怎么定」）：

        1. 按 ``general.true_self`` → 人格配置 ``personality.nickname`` →
           ``alias_names`` 取「本尊身份」候选名（见 :func:`resolve_true_self_identity`）；
        2. 候选名**逐个**去角色表按「名字 → 别名 → 标签」找同一个角色
           （见 :func:`character.find_roster_identity`）；
        3. **命中** → 返回该角色作为候选，走既有注入路径（natural 追加中文说明 +
           角色表标准标签；``tag_completion`` 逻辑照旧），日志：
           ``判定依据=自我指代→本尊身份'<名字>'(来源=配置/人格配置)→角色表命中:<角色名>``；
        4. **没命中** → 不补，日志写明「本尊身份不在角色表中，请加入角色表」；
        5. **完全取不到身份名** → 保持 1.0.12 的保守行为（不补 + 原日志），
           并补一句提示（填 ``general.true_self`` 或人格配置 nickname）。

        无论哪种结果都**不猜测、不做外观识别**：拿不准就不补。

        Returns:
            :class:`CandidateResult`；命中时 ``candidates`` / ``decided`` 都是那一个角色
        """
        identity = resolve_true_self_identity(config)
        hit_entry: CharacterEntry | None = None
        matched_name = ""
        # 候选名逐个试：nickname 不在角色表、别名在角色表时也要能命中
        for candidate in identity.candidates:
            hit_entry = find_roster_identity(candidate, roster)
            if hit_entry is not None:
                matched_name = candidate
                break

        if hit_entry is not None:
            basis = (
                f"判定依据=自我指代→本尊身份'{matched_name}'"
                f"(来源={identity.source})"
                f"→角色表命中:{hit_entry.name or hit_entry.tag or '?'}"
            )
            logger.debug(basis)
            naming = NamingDecision(
                kind="single",
                characters=[hit_entry],
                basis=BASIS_USER_NAMED,
                note=f"自我指代：本尊身份'{matched_name}'（来源={identity.source}）",
            )
            return CandidateResult(
                candidates=[hit_entry],
                decided=[hit_entry],
                llm_multi=False,
                mentioned=mentioned,
                naming=naming,
                basis=basis,
            )

        if identity.candidates:
            if len(identity.candidates) > 1:
                logger.debug(
                    f"本尊身份候选（来源={identity.source}）：{identity.candidates}，"
                    "都不在角色表中"
                )
            # 人格配置的 nickname 有框架默认值（core.toml 里默认是「小狐狸」），
            # 很容易落在角色表之外 —— 额外提示一条更省事的做法（判定依据那行不动）
            if identity.source == _TRUE_SELF_SOURCE_PERSONALITY:
                logger.debug(
                    f"提示：本尊身份来自人格配置（{'、'.join(identity.candidates)}）；"
                    "若与实际角色不符，可直接在 general.true_self 里填写角色表中的名字或别名覆盖"
                )
            # 「本尊是谁」有来源但角色表里没有 → 不补，并把该做什么写清楚
            naming = NamingDecision(
                kind="none",
                note=(
                    f"本尊身份='{identity.candidates[0]}' 不在角色表中，"
                    "无法补标准标签；请把该角色加入角色表"
                ),
            )
        else:
            # 1.0.12 的原日志一字不改，后面补一句怎么配置
            naming = NamingDecision(
                kind="none",
                note="检测到自我指代，本尊由调用方设定，插件不猜",
            )
        basis = naming.label()
        if not identity.candidates:
            basis = (
                f"{basis}"
                "（未取到本尊身份：请填写 general.true_self"
                " 或在人格配置里设置 nickname）"
            )
        logger.debug(basis)
        return CandidateResult(
            candidates=[],
            decided=[],
            llm_multi=False,
            mentioned=mentioned,
            naming=naming,
            basis=basis,
        )

    async def _resolve_bot_sender_ids(self, messages: list[dict[str, Any]]) -> set[str]:
        """尽力解析「本机 bot 自己的 sender_id」集合。

        框架自带的 history 字典（``message_api._rows_to_message_dicts``）只有
        ``sender_id`` / ``sender_name``，没有 ``is_bot`` / ``role`` 字段；框架
        自己是靠 ``adapter_api.get_bot_info_by_platform(platform)["bot_id"]``
        比对的，这里沿用同一口径。

        取不到时返回空集合：判定会退化成「最近提及优先」（把 bot 的复述也当
        点名），但绝不会因为认不出字段而让整条链路静默失效。用
        ``importlib`` 动态导入是为了不把 ``adapter_api`` 变成插件的硬依赖
        （manifest 的 ``api_version`` 里没有它，静态导入会被校验器挑出来）。
        """
        platforms = {str(message.get("platform") or "") for message in messages} - {""}
        if not platforms:
            return set()
        try:
            module = importlib.import_module("src.app.plugin_system.api.adapter_api")
        except Exception as e:  # noqa: BLE001 —— 拿不到 adapter 信息就退化成最近提及
            logger.debug(f"adapter_api 不可用，无法区分 bot 消息与用户消息: {e}")
            return set()
        get_bot_info = getattr(module, "get_bot_info_by_platform", None)
        if get_bot_info is None:
            return set()

        bot_ids: set[str] = set()
        now = time.time()
        for platform in platforms:
            cached = _bot_id_cache.get(platform)
            if cached is not None and (now - cached[0]) < _BOT_ID_CACHE_TTL:
                bot_id = cached[1]
            else:
                try:
                    info = await get_bot_info(platform)
                except Exception as e:  # noqa: BLE001 —— 单个平台失败不影响其它平台
                    logger.debug(f"解析平台 {platform} 的 bot 信息失败: {e}")
                    info = None
                bot_id = str((info or {}).get("bot_id") or "").strip() or None
                _bot_id_cache[platform] = (now, bot_id)
            if bot_id:
                bot_ids.add(bot_id)
        return bot_ids

    # ── 收尾：二次一致性检测 → 按需写回 ────────────────────────────

    def _finalize(
        self,
        *,
        params: dict[str, Any],
        args: dict[str, Any],
        spec: TargetSpec,
        original: str,
        final_content: str,
        decided: list[CharacterEntry],
        roster: list[CharacterEntry],
        config: CharPromptGuardConfig,
        basis: str,
    ) -> tuple[EventDecision, dict[str, Any]]:
        """统一的收尾：跑二次一致性检测，内容有变化才回写 args。"""
        checked = self._apply_consistency_check(
            content=final_content,
            spec=spec,
            args=args,
            roster=roster,
            decided=decided,
            config=config,
            basis=basis,
        )
        if checked != original:
            args[spec.prompt_arg] = checked
            params["args"] = args
        return EventDecision.SUCCESS, params

    def _apply_consistency_check(
        self,
        *,
        content: str,
        spec: TargetSpec,
        args: dict[str, Any],
        roster: list[CharacterEntry],
        decided: list[CharacterEntry],
        config: CharPromptGuardConfig,
        basis: str,
    ) -> str:
        """二次一致性检测：对账「最终提示词里的角色表内角色」vs「判定集合」。

        - ``off``：不检查；
        - ``warn``（默认）：提示词里出现判定集合之外的**角色表内**角色时打一条
          WARNING（含两个角色名与判定依据），内容不改；
        - ``fix``：仅当冲突是「用户点名的角色 vs 画面里另一个角色」且双方都能
          唯一定位（判定集合恰好 1 人、冲突角色恰好 1 个）时，把画面里那个错误
          角色的名字/别名/标签替换成正确角色的（**替换范围仅限角色表里已知的
          字符串**），替换后按需补正确角色的标准标签；出现歧义或多角色无法定位
          → 自动退回 warn 行为并记 WARNING。

        本次没有判定出任何角色（判定集合为空）时直接跳过：那种情况下提示词里的
        角色是描述自己写的，没有依据可对账，也不该报警。
        """
        mode = str(config.general.consistency_check or "").strip().lower()
        if mode == "off" or not roster:
            return content
        if mode not in {"warn", "fix"}:
            logger.warning(
                f"general.consistency_check={mode!r} 不是合法取值（off/warn/fix），按 warn 处理"
            )
            mode = "warn"
        if not decided:
            return content

        texts = self._detection_texts(content, spec, args)
        decided_keys = {entry_key(entry) for entry in decided}
        extras = [
            entry
            for entry in roster
            if entry_key(entry) not in decided_keys and character_in_texts(entry, texts)
        ]
        decided_names = "、".join(entry.name or entry.tag or "?" for entry in decided)
        if not extras:
            logger.debug(
                f"一致性检查=正常（提示词里的角色表内角色都在判定集合内：{decided_names}）"
            )
            return content

        extra_names = "、".join(entry.name or entry.tag or "?" for entry in extras)
        if mode != "fix":
            logger.warning(
                "一致性检查=冲突（warn 不改内容）：提示词里出现了判定集合之外的角色表内角色"
                f"「{extra_names}」，判定集合=「{decided_names}」，依据={basis}"
            )
            return content

        if len(decided) != 1 or len(extras) != 1:
            logger.warning(
                "一致性检查=冲突但无法唯一定位"
                f"（判定集合 {len(decided)} 个 / 冲突角色 {len(extras)} 个），"
                f"退回 warn 行为、不改内容：冲突「{extra_names}」vs 判定「{decided_names}」，依据={basis}"
            )
            return content

        wrong, right = extras[0], decided[0]
        replaced, hits = replace_character_reference(content, wrong, right)
        if hits <= 0:
            logger.warning(
                f"一致性检查=冲突但未能定位可替换文本（角色表里没有「{wrong.name}」的字面写法），"
                f"退回 warn 行为、不改内容：判定「{right.name}」，依据={basis}"
            )
            return content

        logger.warning(
            "一致性检查=冲突已修正（fix）：把画面里的"
            f"「{wrong.name or wrong.tag}」替换为「{right.name or right.tag}」"
            f"（替换 {hits} 处），依据={basis}"
        )
        # 替换后按需补正确角色的标准标签（沿用 tag_completion 逻辑）
        tags = self._completion_tags(replaced, [right], config)
        if tags:
            replaced = self._complete(spec, replaced, tags)
        return replaced

    @staticmethod
    def _resolve_multi(
        config: CharPromptGuardConfig,
        control: BotControl,
        candidates: list[CharacterEntry],
        llm_multi: bool,
        mentioned: int = 0,
        naming_multi: bool = False,
    ) -> bool:
        """决定本次是否按"多人同框"处理。

        优先级：主模型标记 > ``multi.decide``（bot/latest/always_multi） >
        ``multi.decision``（旧字段 off/always/bot）> 用户点名的多人语义 /
        LLM 判定 / 上文人数兜底。
        """
        if not config.multi.enabled:
            return False
        if control.force_multi is not None:
            return control.force_multi

        # 新字段 decide：latest=总是只补最近点名的那个；always_multi=总是多人
        decide = str(getattr(config.multi, "decide", "") or "").lower()
        if decide == "latest":
            return False
        if decide == "always_multi":
            return True

        # 旧字段 decision：保留兼容
        decision = str(config.multi.decision).lower()
        if decision == "off":
            return False
        if decision == "always":
            return True

        # "bot"：用户点名的多人语义 / LLM 判定 / 候选数量兜底
        if naming_multi:
            # 用户在同一条消息里点了 ≥2 个角色，或用了「还有/一起」这类追加词
            return True
        if llm_multi:
            return True
        if str(config.multi.scope).lower() == SCOPE_MULTI_ONLY:
            # 用「上文提到的角色数」判断，而不是「还缺几个」：
            # 上文提了 A+B、提示词已经写了 A 时，也要进多人路径把 B 补上。
            return mentioned >= 2
        return True

    @staticmethod
    def _mode_allowed(mode: Any, config: CharPromptGuardConfig) -> bool:
        """判断当前生图模式是否在生效范围内。"""
        allowed_raw = str(config.general.apply_modes or "")
        allowed = {
            part.strip().lower()
            for part in re.split(r"[,，]", allowed_raw)
            if part.strip()
        }
        if not allowed:
            return False
        mode_str = str(mode or "").strip().lower()
        if not mode_str:
            return True
        return mode_str in allowed

    async def _collect_context(
        self, params: dict[str, Any], config: CharPromptGuardConfig
    ) -> list[dict[str, Any]]:
        """收集最近上文消息（含触发本次生图的消息）。

        历史消息按 stream 做短 TTL 缓存（``general.history_cache_ttl``）：
        主模型一次发起多个生图时不必重复查库。**触发消息永远实时追加**，
        不吃缓存。
        """
        stream_id = resolve_stream_id(params)
        # 触发消息正文只从 params 取一处（_trigger_text），避免与自我指代扫描取到不同文本
        trigger_text = self._trigger_text(params)

        messages: list[dict[str, Any]] = []
        if not stream_id:
            # 读不到上文时把话说清楚：否则表现为"插件没反应"，极难排查
            logger.warning(
                "事件里没有会话 ID（既无 stream_id 也无可用的 message），"
                "本次只能依据触发消息本身判断角色；"
                "若框架补丁是旧版请更新（二版起会带 stream_id）"
            )
        if stream_id:
            ttl = float(config.general.history_cache_ttl)
            cached = _history_cache.get(stream_id) if ttl > 0 else None
            if cached is not None and (time.time() - cached[0]) < ttl:
                messages = list(cached[1])
            else:
                try:
                    # 取回窗口放宽到至少 24h / 200 条：history_limit 配得过小时，
                    # "几小时前明确点名的角色"会被整段截掉，导致判定只能靠猜
                    # （实测：history_limit=10 时把 3 小时前的"流萤"截没了）。
                    # 真正参与判定的条数仍由 multi.recent_messages 控制。
                    _wide_hours = max(float(config.general.history_hours), 24.0)
                    _wide_limit = max(int(config.general.history_limit), 200)
                    messages = await get_recent_messages(
                        stream_id,
                        hours=_wide_hours,
                        limit=_wide_limit,
                    )
                    logger.debug(
                        f"上文取回 {len(messages)} 条"
                        f"（窗口 {_wide_hours:g}h / 上限 {_wide_limit} 条）"
                    )
                except Exception as e:  # noqa: BLE001 —— 读不到历史就当没有上文，不影响生图
                    logger.debug(f"读取上文消息失败: {e}")
                if ttl > 0:
                    if len(_history_cache) >= _HISTORY_CACHE_LIMIT:
                        _history_cache.clear()
                    _history_cache[stream_id] = (time.time(), list(messages))

        if trigger_text:
            messages.append(
                {
                    "time": time.time(),
                    "sender_name": "用户",
                    # 触发消息一定是「进来的人话」，显式标成用户消息：判定依据才会
                    # 记成「用户点名」而不是退一步的「最近提及」
                    "is_bot": False,
                    "role": "user",
                    "processed_plain_text": trigger_text,
                }
            )
        logger.debug(
            f"上文收集: stream_id={stream_id[:8] or '<无>'} "
            f"消息数={len(messages)} 触发消息={trigger_text[:30]!r}"
        )
        return messages

    # ── 注入 ──────────────────────────────────────────────────────

    @staticmethod
    def _completion_tags(
        content: str, roster: list[CharacterEntry], config: CharPromptGuardConfig
    ) -> list[str]:
        """返回需要"补全"的标准标签。

        条件：角色表提供了标签、该角色（名字/别名/标签）已出现在画面描述里、
        但描述里**没有**那个转义正确的标准标签。这类情况说明下游翻译不可靠，
        补标签比补中文名更管用。
        """
        if not config.general.tag_completion or not roster:
            return []
        tags: list[str] = []
        for entry in roster:
            tag = entry.resolved_tag()
            if not tag:
                continue
            if not character_in_texts(entry, [content]):
                continue
            if content_contains_tag(content, tag):
                continue
            tags.append(tag)
        return tags

    @staticmethod
    def _complete(spec: TargetSpec, content: str, tags: list[str]) -> str:
        """把标准标签补到提示词末尾（保留原有描述），返回新内容。"""
        appended = ", ".join(tags)
        stripped = content.rstrip().rstrip(",").rstrip()
        logger.info(f"已补标准标签（{spec.action}）: {appended}")
        return f"{stripped}, {appended}" if stripped else appended

    def _apply(
        self,
        *,
        args: dict[str, Any],
        spec: TargetSpec,
        content: str,
        characters: list[CharacterEntry],
        config: CharPromptGuardConfig,
        control: BotControl,
    ) -> str | None:
        """按目标模式注入角色，返回新的提示词；无需修改时返回 None。"""
        if spec.append_mode == APPEND_MODE_TAGS:
            strategy = control.force_strategy or str(config.multi.strategy_neo).lower()
            use_slot = bool(spec.characters_arg) and (
                strategy == STRATEGY_CHARACTERS
                and len(characters) >= 2
                or control.force_strategy == STRATEGY_CHARACTERS
            )
            if use_slot and self._fill_characters_arg(args, spec, characters, config):
                return content
            return self._append_tags(content, characters)

        # natural 模式：追加中文说明，交由生图插件自己的翻译模型处理
        max_natural = max(1, int(config.multi.natural_max))
        entries = characters
        if len(entries) > max_natural:
            logger.warning(
                f"{spec.plugin or spec.action} 不支持多角色槽位，"
                f"本次只补前 {max_natural} 个角色"
            )
            entries = entries[:max_natural]
        clauses = [self._build_append_text(entry, config) for entry in entries]
        clauses = [clause for clause in clauses if clause]
        if not clauses:
            return None
        merged = f"{content.strip()}\n{''.join(clauses)}"
        # 角色表提供了标准标签时，顺带把转义标签也写上：实测下游翻译模型会把中文名
        # 翻坏（把「花火」翻成 hanabi (firefly) (honkai: star rail)），有标签才保险。
        if config.general.tag_completion:
            tags = [
                entry.resolved_tag()
                for entry in entries
                if entry.resolved_tag()
                and not content_contains_tag(merged, entry.resolved_tag())
            ]
            if tags:
                merged = f"{merged.rstrip().rstrip(',')}, {', '.join(tags)}"
        return merged

    @staticmethod
    def _append_tags(content: str, characters: list[CharacterEntry]) -> str | None:
        """把角色标签追加到标签串末尾。"""
        tags: list[str] = []
        for entry in characters:
            tag = entry.resolved_tag()
            if not tag:
                logger.info(
                    f"角色 {entry.name or '?'} 未提供角色标签，跳过注入"
                    "（请在角色表第 4 列填写标准标签，如 elysia \\(honkai impact 3rd\\)）"
                )
                continue
            if content_contains_tag(content, tag):
                continue
            tags.append(tag)
        if not tags:
            return None
        stripped = content.rstrip().rstrip(",").rstrip()
        joined = ", ".join(tags)
        return f"{stripped}, {joined}" if stripped else joined

    def _fill_characters_arg(
        self,
        args: dict[str, Any],
        spec: TargetSpec,
        characters: list[CharacterEntry],
        config: CharPromptGuardConfig,
    ) -> bool:
        """把角色写入独立角色槽位参数；成功返回 True。"""
        existing = self._parse_existing_characters(args.get(spec.characters_arg))
        if existing is None:
            logger.warning(
                f"{spec.characters_arg} 不是合法 JSON，退回追加标签方式，不做覆盖"
            )
            return False

        existing_keys = {
            normalize_prompt_text(str(item.get("prompt") or "")) for item in existing
        }
        added = 0
        for entry in characters:
            tag = entry.resolved_tag()
            if not tag:
                logger.info(
                    f"角色 {entry.name or '?'} 未提供角色标签，跳过角色槽位注入"
                )
                continue
            key = normalize_prompt_text(tag)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            existing.append({"prompt": tag})
            added += 1

        if added == 0:
            return False

        if str(config.multi.coord_plan).lower() == "spread" and len(existing) > 1:
            total = len(existing)
            for index, item in enumerate(existing):
                if item.get("x") is None:
                    item["x"] = round((index + 1) / (total + 1), 3)
                if item.get("y") is None:
                    item["y"] = 0.5

        args[spec.characters_arg] = json.dumps(existing, ensure_ascii=False)
        return True

    @staticmethod
    def _parse_existing_characters(raw: Any) -> list[dict[str, Any]] | None:
        """解析已存在的角色槽位参数；非法时返回 None。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return []
        if isinstance(raw, list):
            data: Any = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return None
        else:
            return None

        if not isinstance(data, list):
            return None
        result: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                result.append(dict(item))
        return result

    @staticmethod
    def _build_append_text(
        character: CharacterEntry, config: CharPromptGuardConfig
    ) -> str:
        """根据配置模板生成追加的角色说明文本。"""
        game = character.game.strip()
        template = (
            config.general.append_format
            if game
            else config.general.append_format_no_game
        )
        try:
            return template.format(name=character.name.strip(), game=game)
        except (KeyError, ValueError):
            return template
