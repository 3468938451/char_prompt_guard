"""char_prompt_guard 插件入口。

加载后订阅 before_action_call 事件，在生图 Action 执行前检测角色提示词：
已含角色则放行，未含则根据上文对话中的角色自动补入（支持多角色同框）。

同时把「角色补全已开启 + 可用的运行时标记 + 生图参数的标签书写规则」以
system reminder 的形式告知主模型，让它可以按需决定本次要单人还是多人、
用哪种注入方式，并把角色标准标签写在提示词最前面。

1.0.17 起再加一条**更强的**通道：插件加载时（以及「全部插件加载完成」/首次生图
事件时各重试一次）把同一句「最前面必须先写角色的标准标签」**直接追加进生图动作
的参数说明**（``general.tag_first_hint``，纯内存改动）—— 主模型在工具定义里就能
看到这条要求，比 system reminder 这种"旁白"有效得多。逻辑都在 :mod:`tag_hint`。

1.0.18 起关联一个**外部记忆库**（Booku Memory，``[memory]`` 节，见 :mod:`memory`）：
判定**不确定**时（主题跟踪的 ``split`` 拆单 / ``unavailable`` 本尊身份取不到 /
``low_confidence`` 置信度不足 / ``empty`` 无信号）去检索相关信息做一次「再确认」，
命中「触发词 → 角色表内角色」的明确对应就直接用作判定依据，并把这次确认写回
记忆库（``memory.write_back``）。关闭（``memory.enabled = false``）时**完全不发
任何请求**；不可用时静默降级 —— 判定链与 1.0.17 逐字一致。

1.0.19 起针对一次真实事故补上两处对账（都不新增配置项）：

- 参数说明的追加句（:data:`tag_hint.HINT_SUFFIX`）再补一句：标签必须与**本次画面
  描述的角色一致**，不要照搬备忘 / 历史记录 / 上一张图的标签；
- 二次一致性检测（``general.consistency_check``）的对账基准从「判定集合」扩成
  **期望集合**（判定集合 + 主题跟踪主题 + 记忆库确认结果），并且**描述里已经点名
  角色表内角色时**也会额外做一次「对账用」记忆库查询（只用于对账，不改判定、
  不写回）。细节见 :mod:`handler` 模块顶部与 :class:`handler.ExpectationSet`。

1.0.20 起再补上一处**独立**的检查（同一次事故的另一半，同样不新增配置项）：
用户这次点名的是**角色表之外**的角色（「刻律德拉」），画面里却挂着角色表内的
花火标签 —— 1.0.19 的三份证据（判定 / 主题跟踪 / 记忆库）**全是空的**，期望集合
为空 → 一致性检查直接跳过，该报警却没报。所以：

- 参数说明的追加句再补第三句：**要画的角色不在角色表里时，不要写任何标准标签**，
  只写画面描述（不要拿备忘 / 历史记录 / 上一张图的标签顶上）；
- 新增 :meth:`handler.CharacterGuardHandler._apply_outside_naming_check`：扫描
  「触发消息 + 触发前最近几条**用户**消息」提取**表外**点名（引号模式 + 句式模式，
  见 :func:`character.extract_outside_names`），若最终提示词里出现**用户这次没点过**
  的角色表内角色即为冲突 —— ``warn`` 只报不改、``fix`` **删除**画面里那个角色的
  名字 / 别名 / 标准标签（**保留外观描述原文**），``off`` 完全不检查。
  它与期望集合对账**互相独立**、可各自触发（跑在 :meth:`_finalize` 里、对账之后）。

1.0.21 起补上「删掉之后没有补上正确角色的标签」这半边（用户反馈「这个角色的提示词
没有进去」）：新增 ``general.auto_resolve_characters``（默认开）—— 用**已配置的模型**
（同一套 ``llm_api``，模型沿用 ``general.translate_model``、超时沿用 ``llm_timeout``，
不引入新依赖）把角色表之外的角色解析成「正式名 + 作品名 + NAI 标签」
（:func:`character.resolve_character_via_llm`），再走两个入口：

- **替换**：表外点名对账判成冲突 + ``consistency_check="fix"`` + 解析成功 →
  用解析出的标签**替换**画面里的错误标签（而不是只删除）；
- **补入**：画面里**没有任何角色标签**（例如描述只写了外观）+ 解析成功 →
  按 ``natural`` / ``tags`` 模式把解析结果补进提示词最前面（复用既有注入格式）。

解析结果严格校验（``unknown`` / JSON 坏 / 标签不符合规范 / ``confidence`` 低于 0.5
一律判失败）并缓存（命中缓存不重复问模型）；失败时逐字退回 1.0.20 的行为
（``warn`` 不变、``fix`` 只删除），**任何异常都不拦住生图**。

1.0.22 起再补上**「皮肤 / 时装」这一维**（``[outfits]`` 节，见 :mod:`outfit`）：
用户说「我想看你的某某某（皮肤）」时，把它解析成英文关键词并**追加**进提示词
（例如「我想看粉色妖精小姐」→ 爱莉穿着这一套的衣服）。**角色那一侧一个字都没改**
（描述点名 > 主题跟踪 > 记忆库 > LLM 仲裁 > 不补），新增的只是「这次要穿哪一套」：

- 皮肤名只从**用户消息**里找（触发消息 + 触发前最近 4 条用户消息，bot 消息跳过；
  **不扫画面描述**），句式 + 引号两种模式提取，再过长度 / 虚词 / 黑名单 / 通用词 /
  「已经在角色表里」五道过滤；命中 ``[outfits] entries`` 的候选优先；
- **表优先 → 模型兜底**：表命中零模型调用；表未命中且 ``auto_resolve`` 时用已配置的
  模型（同一套 ``llm_api`` / ``translate_model`` / ``llm_timeout``）解析，
  ``unknown`` / JSON 坏 / ``confidence < 0.5`` / 没有可用关键词一律判失败 ——
  **什么都不注入**（绝不瞎编），日志 ``皮肤解析(LLM)失败：<原因>``；
- 缓存按「皮肤名 + 角色名」，成功 24 小时、失败 10 分钟，**读写在调用方**；
- 注入放在 ``_finalize`` 的最后一步：``natural`` 追加
  ``（皮肤/时装：“粉色妖精小姐”：pink elf dress, fairy wings）``，``tags`` 把关键词
  追加进同一个参数串；``[outfits] enabled = false`` 时整条链路零开销。

1.0.23 起修三件事（判定链仍然一行未改，见 :mod:`handler` / :mod:`outfit`）：

- **皮肤名不再被当成表外角色名**：皮肤识别先跑，它认出来的皮肤 / 时装名通过
  ``exclude`` 传给 :func:`character.extract_outside_names` **豁免** —— 用户说
  「画粉色妖精小姐这套」时「粉色妖精小姐」是**一套皮肤**，不会为它多问一次模型、
  ``fix`` 也不会再去动画面里的角色标签（注入位置与语义一个字没变）；
- **作品名去重**（:func:`character._normalize_game_field`）：``game_cn`` 已含
  ``game_en`` 时只保留一个，不再拼出 ``… (Honkai: Star Rail) (Honkai: Star Rail)``；
- **``outfits.infer_unnamed``**（新增，**默认关**）：用户只说「流萤的那一套皮肤」
  而没点名具体皮肤时，开了才由模型推断该角色最知名的一套并注入
  （:func:`outfit.find_unnamed_outfit_owner` + :func:`outfit.infer_outfit_via_llm`，
  校验口径与 1.0.22 完全相同，日志标注「来源=模型推断」，推断结果不写回记忆库）；
  默认关时 0 次模型调用、无相关日志，行为与 1.0.22 逐字一致。
"""

from __future__ import annotations

from typing import Any, ClassVar

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from . import tag_hint
from .config import CharPromptGuardConfig
from .handler import _BUILTIN_TARGETS, CharacterGuardHandler

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
    plugin_version: str = "1.0.27"

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

        # 1.0.17：把「标签写在最前面」直接追加进生图动作的**参数说明**（纯内存改动，
        # 不写文件）。这是比 reminder 强得多的通道，所以放在 reminder 之前、且不受
        # multi.notify_bot 影响 —— 两条通道互相独立，各有各的开关
        # （tag_first_hint / notify_tag_rule）。
        #
        # 本插件 dependencies 为空，拓扑排序通常把它排在 nai_artist /
        # image_generator_plugin-neo **前面**，所以这一次多半会因为「目标动作还没注册」
        # 而空手而归；那种情况下 handler 会在「全部插件加载完成」或首次生图事件时
        # 再试一次（见 tag_hint.maybe_inject_on_event）。这里失败只记 DEBUG，不报错。
        tag_hint.inject_on_load(_BUILTIN_TARGETS, config=config)

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
        """插件卸载时移除 reminder，并把改过的参数说明还原。

        注意：``prompt_api`` 目前只提供 reminder 的 ``add`` / ``get``，
        **没有 bucket 级删除接口**（只有 stream 级有 ``delete_stream_reminder``），
        所以这里只能直接走底层的 SystemReminderStore；等上游补上 API 再换回
        prompt_api。store 是纯内存单例，进程重启即清空。

        1.0.17 起参数说明也是内存内改动，同样在卸载时还原（``tag_hint`` 只记
        「改过哪几个参数 + 原说明」），尽力而为、失败只记 DEBUG。
        """
        try:
            tag_hint.restore_tag_first_hint()
        except Exception as e:  # noqa: BLE001 —— 还原失败不该打断卸载流程
            logger.debug(f"还原生图动作的参数说明失败: {e}")

        try:
            from src.core.prompt import get_system_reminder_store

            get_system_reminder_store().delete(_REMINDER_BUCKET, _REMINDER_NAME)
            logger.debug("char_prompt_guard 标记说明 reminder 已移除")
        except Exception as e:  # noqa: BLE001 —— 卸载清理失败只记日志，不打断卸载流程
            logger.debug(f"移除 system reminder 失败: {e}")
