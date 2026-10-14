"""char_prompt_guard 插件入口。

加载后订阅 before_action_call 事件，在生图 Action 执行前检测角色提示词：
已含角色则放行，未含则根据上文对话中的角色自动补入（支持多角色同框）。

同时把「角色补全已开启 + 可用的运行时标记 + 生图参数的标签书写规则」以
system reminder 的形式告知主模型，让它可以按需决定本次要单人还是多人、
用哪种注入方式，并把角色标准标签写在提示词最前面。
"""

from __future__ import annotations

from typing import Any, ClassVar

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .config import CharPromptGuardConfig
from .handler import CharacterGuardHandler

logger = get_logger("char_prompt_guard")

_REMINDER_BUCKET = "actor"
_REMINDER_NAME = "关于生图时的角色提示词补全"
_REMINDER_CONTENT = (
    "【生图角色补全已开启】你调用生图工具时，如果提示词里没有写出画面需要的角色，"
    "系统会自动根据最近对话把角色补进去；多人同框的场景也会一并补齐。"
    "你正常写画面描述即可，不需要为了这个机制改变习惯。"
    "需要显式控制时，可以在提示词里加入下列标记（系统会自动移除，不会进入画面）："
    "#solo# 本次只补一个角色；#multi# 强制按多人同框补齐；"
    "#chars# 用角色的独立槽位参数；#tags# 把角色标签直接追加进提示词；"
    "#nochar# 本次完全不要自动补角色。"
)

#: 生图参数的「标签在前」硬规则（1.0.12 起，随 ``general.notify_tag_rule`` 开关）。
#: 实测下游翻译模型会把中文角色名翻坏（「花火」→ ``hanabi (firefly) (honkai: star rail)``，
#: 括号未转义还串了角色），标签写错位置（追加在末尾/混在描述中间）时权重与稳定性都更差，
#: 所以在 reminder 里明确要求主模型**自己就把标准标签写在最前面**。
_REMINDER_TAG_RULE = (
    "【生图参数硬规则】调用生图动作（share_photo / share_drawing / draw_image）时，"
    "提示词参数（content / content_description）**最前面必须先写角色的标准标签**"
    "（角色表第 4 列，例如 Hanabi \\(honkai: star rail\\)），然后再写画面描述；"
    "角色表没有提供标签时，写「角色名（游戏名）」。"
)


def _tag_rule_enabled(config: Any) -> bool:
    """读取 ``general.notify_tag_rule``；读不到时按默认（**包含**这条规则）处理。

    配置对象缺失、字段缺失或取值不是布尔时一律返回 True：reminder 是给主模型的
    提示，多一条硬规则不会让生图失败，少一条却会让标准标签被翻译模型改坏。
    """
    general = getattr(config, "general", None)
    value = getattr(general, "notify_tag_rule", None)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(value)


def build_reminder_content(config: Any = None) -> str:
    """拼出 system reminder 的正文。

    - 标记说明（``#multi#`` / ``#nochar#`` 等）是 1.0.11 及以前就有的内容，
      **任何情况下都不能丢**；
    - 「生图参数最前面先写标准标签」这条硬规则由 ``general.notify_tag_rule``
      控制（默认开启），关掉后只保留标记说明。
    """
    if _tag_rule_enabled(config):
        return f"{_REMINDER_CONTENT}\n{_REMINDER_TAG_RULE}"
    return _REMINDER_CONTENT


@register_plugin
class CharPromptGuardPlugin(BasePlugin):
    """角色提示词守卫插件。"""

    plugin_name: str = "char_prompt_guard"
    plugin_description: str = (
        "生图前检测角色提示词：已含角色则放行，否则根据上文自动补入角色名+游戏名"
        "（或 NAI 角色标签），支持多角色同框"
    )
    plugin_version: str = "1.0.14"

    # 注意：这里刻意**不加** ClassVar 注解 —— MPDT 的 ConfigValidator 用 libcst 只识别
    # 普通赋值（cst.Assign），写成 `configs: list[type] = [...]` 会被判成"插件未定义配置类"。
    configs = [CharPromptGuardConfig]  # noqa: RUF012
    dependencies: ClassVar[list[str]] = []

    def get_components(self) -> list[type]:
        """返回插件提供的组件类列表。"""
        return [CharacterGuardHandler]

    @staticmethod
    def _log_runtime_capability() -> None:
        """启动时打一条能力自检日志，避免"装上了却没反应"无处排查。

        两条执行路径分别依赖不同的钩子：

        - ``before_action_call``：`ActionManager` 路径会发；
        - ``before_tool_call``：agent 聊天器把 Action 当工具调用时才会用到。

        核心没提供 ``before_tool_call`` 时说明核心偏旧；核心提供了事件、
        但 agent 聊天器路径仍不发（上游在 ``llm_tool_call.py`` 直接
        ``_wrap_execute``）时，需要打框架补丁，这里只能提示、无法自检。
        """
        from src.app.plugin_system.types import EventType

        has_tool_event = getattr(EventType, "BEFORE_TOOL_CALL", None) is not None
        if has_tool_event:
            logger.info(
                "char_prompt_guard 运行环境自检：核心提供 before_tool_call，"
                "已同时订阅 before_action_call + before_tool_call"
            )
        else:
            logger.warning(
                "char_prompt_guard 运行环境自检：核心未提供 before_tool_call，"
                "只订阅 before_action_call；agent 聊天器路径需要更新核心或打框架补丁"
            )

    async def on_plugin_loaded(self) -> None:
        """插件加载时告知主模型角色补全机制、可用标记与标签书写规则。"""
        config = self.config
        if not isinstance(config, CharPromptGuardConfig):
            config = CharPromptGuardConfig()

        self._log_runtime_capability()

        if not config.general.enabled or not config.multi.notify_bot:
            logger.debug("char_prompt_guard 未注入标记说明 reminder")
            return

        try:
            from src.app.plugin_system.api import prompt_api

            prompt_api.add_system_reminder(
                bucket=_REMINDER_BUCKET,
                name=_REMINDER_NAME,
                content=build_reminder_content(config),
            )
            logger.debug(
                "char_prompt_guard 标记说明 reminder 已注册"
                f"（标签硬规则：{'含' if _tag_rule_enabled(config) else '不含'}）"
            )
        except Exception as e:  # noqa: BLE001 —— 提醒注入失败不该影响插件加载
            logger.warning(f"注册 system reminder 失败: {e}")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时移除 reminder。

        注意：``prompt_api`` 目前只提供 reminder 的 ``add`` / ``get``，
        **没有 bucket 级删除接口**（只有 stream 级有 ``delete_stream_reminder``），
        所以这里只能直接走底层的 SystemReminderStore；等上游补上 API 再换回
        prompt_api。store 是纯内存单例，进程重启即清空。
        """
        try:
            from src.core.prompt import get_system_reminder_store

            get_system_reminder_store().delete(_REMINDER_BUCKET, _REMINDER_NAME)
            logger.debug("char_prompt_guard 标记说明 reminder 已移除")
        except Exception as e:  # noqa: BLE001 —— 卸载清理失败只记日志，不打断卸载流程
            logger.debug(f"移除 system reminder 失败: {e}")
