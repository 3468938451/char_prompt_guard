"""char_prompt_guard 事件处理器。

订阅 ``before_action_call`` 事件，在生图 Action 真正执行之前检查提示词：

- 提示词里已经带有角色 → 放行；
- 没有角色 → 按「角色表快速匹配 → LLM 兜底提取」的顺序从最近对话上文中
  找出当前指代的角色，补入提示词；多角色同框时按配置补齐多个。

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
    CharacterEntry,
    character_in_texts,
    content_contains_tag,
    entry_key,
    extract_characters_via_llm,
    find_characters_in_history,
    iter_config_entries,
    normalize_prompt_text,
    parse_roster,
)
from .config import CharPromptGuardConfig

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
                normalize_prompt_text(alias) == name
                for alias in known.aliases
                if alias
            )
        if not same and tag and known.name:
            same = tag == known_name
        if not same:
            continue
        return CharacterEntry(
            name=entry.name or known.name,
            game=entry.game or known.game,
            aliases=entry.aliases or known.aliases,
            tag=entry.tag or known.tag,
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

        # 候选角色：先角色表扫描，必要时 LLM 兜底/补全
        candidates, llm_multi, mentioned = await self._collect_candidates(
            params, config, roster, content, texts
        )

        # 标签补全：描述里已经点名了角色表内的角色、但缺少该角色的标准标签时补标签。
        # 与"要不要新补一个角色"无关，所以放在最前面独立处理。
        # 这是实测逼出来的：描述里写着"花火"，下游 nai_artist 的翻译模型却把它翻成
        # ``hanabi (firefly) (honkai: star rail)``（括号未转义 + 串了另一个角色），
        # 补上转义正确的标准标签后，NAI 才能稳定锁定角色。
        completion_tags = self._completion_tags(content, roster, config)

        multi = self._resolve_multi(config, control, candidates, llm_multi, mentioned)
        if not multi:
            # 单角色路径：沿用"命中任一角色即整体放行"
            if present_keys:
                if completion_tags:
                    return self._apply_completion(params, args, spec, content, completion_tags)
                logger.debug("提示词已包含角色，放行")
                return EventDecision.SUCCESS, params
            target_characters = candidates[:1]
        else:
            target_characters = [
                entry for entry in candidates if entry_key(entry) not in present_keys
            ][: max(1, int(config.multi.max_characters))]

        if not target_characters:
            if completion_tags:
                return self._apply_completion(params, args, spec, content, completion_tags)
            logger.debug("未识别到需要补入的角色，放行")
            return EventDecision.SUCCESS, params

        new_content = self._apply(
            args=args,
            spec=spec,
            content=content,
            characters=target_characters,
            config=config,
            control=control,
        )
        if new_content is None:
            return EventDecision.SUCCESS, params

        args[spec.prompt_arg] = new_content
        params["args"] = args
        names = ", ".join(entry.name or entry.tag or "?" for entry in target_characters)
        # 把"追加了什么"直接写进日志：用户最容易困惑的就是"到底补了谁、有没有游戏名"
        appended = new_content[len(content) :].strip()
        logger.info(
            f"已补入角色（{spec.action} / {spec.append_mode}"
            f"{' / 多人' if multi else ''}）: {names}"
            f"{f' → 追加 {appended!r}' if appended else ''}"
        )
        return EventDecision.SUCCESS, params

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

    async def _collect_candidates(
        self,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        content: str,
        texts: list[str],
    ) -> tuple[list[CharacterEntry], bool, int]:
        """收集候选角色（角色表扫描 → LLM 兜底/补全）。

        Returns:
            (待补入的角色, LLM 判定的 multi, 上文提到的角色总数)
            第三项是**过滤前**的候选数量，供 multi_only 判断"上文是不是多人场景"。
        """
        messages = await self._collect_context(params, config)
        recent_window = (
            int(config.multi.recent_messages) if config.multi.recent_only else 0
        )
        window = messages[-recent_window:] if recent_window > 0 else messages

        # ① 角色表在上文里的命中（确定性、零成本）：作为兜底候选
        history_candidates: list[CharacterEntry] = []
        seen: set[str] = set()
        if roster:
            for entry in find_characters_in_history(window, roster):
                key = entry_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                history_candidates.append(entry)

        # 「上文中被提及的角色数」在过滤前先记下来：multi_only 判断的是
        # "上文是不是多人场景"，而不是"这次还缺几个角色"。
        mentioned = len(history_candidates)

        # ② 画面描述里已经点名的「角色表内」角色
        present_keys = {entry_key(entry) for entry in roster if character_in_texts(entry, texts)}

        # ③ 决定要不要问 LLM。
        # 关键：光看"上文里有没有角色表内的角色"是不够的 —— 上文里提到的可能是
        # 几句之前的**另一个**角色（实测：描述里要的是花火，上文最旧那条却在说
        # "不加爱莉的提示词"，于是补错了人）。所以只要**画面描述里没有角色表内
        # 角色**，就让 LLM 结合描述判定一次（它能看到这次到底要画谁）。
        #
        # 另有一种必须问 LLM 的情况：上文里出现了 ≥2 个角色表内角色（multi_only
        # 会据此进多人路径）。此时若描述其实已经点名了某个角色，贸然把上文那两个
        # 都补进去就会出错（实测：用户要爱莉，却被补成花火 + 流萤）。多人场景交给
        # LLM 仲裁，它读过描述，知道这次到底要几个人。
        multi_candidates = (
            config.multi.enabled
            and str(config.multi.scope).lower() == SCOPE_MULTI_ONLY
            and mentioned >= 2
        )
        need_llm = config.general.use_llm and (
            str(config.multi.llm_mode).lower() == "always"
            or not history_candidates
            or (config.general.description_first and not present_keys)
            or multi_candidates
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
                logger.debug("LLM 提取无结果（超时或失败），回退角色表命中")
            else:
                logger.debug(
                    "LLM 提取结果: "
                    f"characters={[entry.name or entry.tag for entry in extraction.characters]} "
                    f"multi={extraction.multi}"
                )
                llm_multi = extraction.multi
                llm_answered = bool(extraction.characters)
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

        # ④ LLM 读过画面描述，它的结论优先；角色表的历史命中只作兜底。
        # 注意三种情况要分清（实测踩过坑：描述里写着"花火"、LLM 也答"花火"，
        # 但那条"已在提示词里"被过滤掉后错误地回退，把上文里的爱莉补了进去）：
        if llm_candidates:
            # LLM 给出了需要补的角色
            candidates = llm_candidates
        elif llm_answered:
            # LLM 答了，只是它给的角色都已经在提示词里 → 画面没问题，本次不补，
            # 绝不回退到上文里的旧角色
            logger.debug("LLM 给出的角色都已在提示词中，无需补入（不回退上文命中）")
            candidates = []
        else:
            # LLM 没给出结论（超时/失败/空结果）→ 才用角色表在上文里的命中兜底
            candidates = [
                entry for entry in history_candidates if not character_in_texts(entry, texts)
            ]

        max_candidates = max(1, int(config.multi.max_characters))
        return candidates[:max_candidates], llm_multi, mentioned

    @staticmethod
    def _resolve_multi(
        config: CharPromptGuardConfig,
        control: BotControl,
        candidates: list[CharacterEntry],
        llm_multi: bool,
        mentioned: int = 0,
    ) -> bool:
        """决定本次是否按"多人同框"处理。"""
        if not config.multi.enabled:
            return False
        if control.force_multi is not None:
            return control.force_multi

        decision = str(config.multi.decision).lower()
        if decision == "off":
            return False
        if decision == "always":
            return True

        # decision == "bot"：由 LLM 判定 + 候选数量兜底
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
        message = params.get("message")
        stream_id = resolve_stream_id(params)
        trigger_text = ""
        if message is not None:
            trigger_text = str(
                getattr(message, "processed_plain_text", "")
                or getattr(message, "content", "")
                or ""
            ).strip()

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
                    messages = await get_recent_messages(
                        stream_id,
                        hours=float(config.general.history_hours),
                        limit=int(config.general.history_limit),
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
    def _apply_completion(
        params: dict[str, Any],
        args: dict[str, Any],
        spec: TargetSpec,
        content: str,
        tags: list[str],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """把标准标签补到提示词末尾（保留原有描述）。"""
        appended = ", ".join(tags)
        stripped = content.rstrip().rstrip(",").rstrip()
        args[spec.prompt_arg] = f"{stripped}, {appended}" if stripped else appended
        params["args"] = args
        logger.info(f"已补标准标签（{spec.action}）: {appended}")
        return EventDecision.SUCCESS, params

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
                if entry.resolved_tag() and not content_contains_tag(merged, entry.resolved_tag())
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
