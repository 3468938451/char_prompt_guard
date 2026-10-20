"""框架运行时兼容 shim（1.0.34）：没打静态补丁的机器也能收到「执行前事件」。

## 为什么需要它

插件靠「工具 / 动作**执行前**事件」才能在生图前改写提示词。三条执行路径里只有
``ActionManager.execute_action`` 与 ``ToolManager.execute_tool`` 会发事件；agent 聊天器
（``default_chatter`` / ``neo_default_chatter`` / KFC 等）走
``src/core/utils/llm_tool_call.py`` 的 ``run_tool_call`` →
``create_llm_usable_execution`` → ``_wrap_execute``，**一个事件都不发**。此前唯一的
解法是手工把 ``framework-patch/llm_tool_call-pre-events.patch`` 打进框架源码 —— 于是最
常见的现象是「插件加载成功，但永远不生效」。

1.0.34 起改为**插件自己在运行时探测并补一层**（**只改内存，不写任何文件**，也**不
import 补丁的内部符号**）：

- 框架已经在发执行前事件（打过静态补丁，或上游将来原生支持）→ **什么都不做**、一行
  代码都不执行，返回 ``already_patched``；
- 框架不发、结构又认得出 → 把 ``run_tool_call`` **原地换成**一层包装，返回 ``applied``；
- 认不出（模块不在 / 函数不存在 / 源码读不到）→ **不装**，返回 ``unsupported``
  （最坏等于"没装 shim"，行为与 1.0.33 逐字一致）。

## 检测口径（fail-closed：认不出就不装）

静态补丁的特征是「执行前事件 + 发布动作 + 写回 args」三件事同时出现在**同一份源码**
里，所以 :func:`looks_statically_patched` 要求三者都能找到：
``BEFORE_ACTION_CALL`` / ``BEFORE_TOOL_CALL``（事件名）、``.publish(``（发布）、
``"args"``（回写）。扫描范围是**整个宿主模块源码** + 三个相关函数
（``run_tool_call`` / ``create_llm_usable_execution`` / ``exec_llm_usable``）——
**不是只看要包装的那个函数**：静态补丁打在 ``create_llm_usable_execution`` 里
（``run_tool_call`` 只是调用它），只扫 ``run_tool_call`` 会漏判、进而重复发布两条事件。
源码一处都读不到时返回 ``unsupported`` —— **宁可不装，也不重复发事件**。

## 包装形状

::

    async def wrapper(*args, **kwargs):          # functools.wraps 保留原函数信息
        patched = await _prepare_patched_kwargs(kwargs, logger)   # 任何失败 → None
        if patched is None:
            return await func(*args, **kwargs)   # 零开销：原样调用原函数
        return await func(*args, **patched)

- **没有订阅者时一个事件都不发**（先 ``get_subscribers`` 判断，与框架自己的发布点、
  静态补丁同一口径），也不复制参数、不 await；
- 有订阅者时按 ``calls`` 逐条发布（``action-`` / ``tool-`` 前缀与签名里的
  ``plugin:action:name`` 都能认），把订阅者写回的 ``args`` 换成新的调用对象再交给原
  函数 —— 与静态补丁「回写 args 后再 ``_wrap_execute``」的效果一致；
- ``stream_id`` 的解析**沿用插件现有口径**（:func:`handler.resolve_stream_id`），事件里
  同时带 ``message``，两条线索都给到订阅者；
- 任何异常（事件总线 / 结构不符 / 属性只读）都只记 DEBUG、**绝不拦住组件执行**：
  最坏就是"这次没改写提示词"，不会让生图失败。

## 已 import 的别名也一起换

``src/app/plugin_system/api/llm_api.py`` 在导入期就
``from src.core.utils.llm_tool_call import run_tool_call``，之后一直用这个模块性别名；
只换宿主模块属性对它无效。所以安装时顺手把 ``sys.modules`` 里**值仍然是同一个函数
对象**的 ``run_tool_call`` 别名一起指到 wrapper 上（纯内存，只动本来就指向它的模块）。

## 覆盖范围（有意收窄）

包装点是 ``run_tool_call``（agent / KFC 聊天器的工具调用入口，也正是静态补丁要救的
那条路径）。直接走 ``create_llm_usable_execution`` / ``exec_llm_usable`` 的调用方
（例如 ``BaseAgent`` 的本地组件执行）**不在**这层 shim 里 —— 需要全覆盖时仍然建议打
静态补丁；两者可以共存，shim 检测到补丁会自动让位（返回 ``already_patched``）。
"""

from __future__ import annotations

import dataclasses
import functools
import importlib
import inspect
import logging
import sys
from typing import Any

# ── 宿主结构（只读：模块名 / 函数名，不写文件、不碰源码）─────────────────

#: agent 聊天器走的 LLM 工具调用模块
HOST_MODULE = "src.core.utils.llm_tool_call"
#: 要补事件的入口（原函数自己不发任何事件）
TARGET_FUNC = "run_tool_call"
#: 静态补丁会改到、上游将来也可能原生支持的两个函数（**只用于检测**）
PATCH_CARRIER_FUNCS: tuple[str, ...] = (
    "create_llm_usable_execution",
    "exec_llm_usable",
)

#: 幂等标记（函数属性，纯内存；打过之后第二次调用直接 `already_installed`）
WRAP_FLAG = "__cpg_runtime_wrapped__"

# ── 静态补丁的检测标记（见模块顶部「检测口径」）────────────────────────
#: 「执行前事件」标记：这一类串出现在源码里说明这条路径在发事件
PRE_EVENT_MARKERS: tuple[str, ...] = ("BEFORE_ACTION_CALL", "BEFORE_TOOL_CALL")
#: 「发布动作」标记
PUBLISH_MARKER = ".publish("
#: 「写回 args」标记（补丁二版用 ``_modified.get("args")`` 把订阅者的修改取回来）
WRITEBACK_MARKERS: tuple[str, ...] = ('"args"', "'args'")

#: 两个执行前事件的名字（取 ``EventType`` 成员时用）
EVENT_ACTION = "BEFORE_ACTION_CALL"
EVENT_TOOL = "BEFORE_TOOL_CALL"

# ── `install()` 的返回值（插件入口据它决定日志文案与级别）──────────────
#: 配置开关关着，什么都没做
STATUS_DISABLED = "disabled"
#: 框架结构不认识 / 源码读不到 —— 已跳过（需要静态补丁）
STATUS_UNSUPPORTED = "unsupported"
#: 框架已经在发执行前事件（静态补丁或上游原生支持）—— 未重复包装
STATUS_ALREADY_PATCHED = "already_patched"
#: 本进程里已经装过 —— 跳过
STATUS_ALREADY_INSTALLED = "already_installed"
#: 本次装上了运行时兼容
STATUS_APPLIED = "applied"

#: 组件类型（事件字段前缀同时是 ``action_name`` / ``tool_name``）
_KINDS: tuple[str, ...] = ("action", "tool")


# ── 宿主接缝（前三个可被离线用例替换）+ 插件现有口径 ────────────────────


def host_module() -> Any:
    """导入宿主模块（**可替换接缝**：离线用例换掉它即可完全不碰真框架）。

    Returns:
        Any: 宿主模块对象。
    """
    return importlib.import_module(HOST_MODULE)


def event_bus() -> Any:
    """取框架事件总线（**可替换接缝**）。"""
    from src.kernel.event import get_event_bus

    return get_event_bus()


def event_type(name: str) -> Any:
    """按名字取 ``EventType`` 成员（**可替换接缝**）；核心偏旧时返回 ``None``。"""
    from src.core.components.types import EventType

    return getattr(EventType, name, None)


def resolve_stream_id(raw_stream_id: Any, message: Any) -> str:
    """解析会话 ID —— **沿用插件现有口径**（``handler.resolve_stream_id``）。

    发布点（``ActionManager`` / 静态补丁）给的字段并不统一：有的只给 ``message``，
    有的额外带 ``stream_id``。这里直接把两条线索交给插件自己那个函数，
    保证 shim 发出去的事件与框架原生发布点解析结果一致。
    """
    raw = str(raw_stream_id or "").strip()
    try:
        from .handler import resolve_stream_id as _plugin_resolve_stream_id
    except Exception:  # noqa: BLE001 —— 拿不到插件自己的解析函数时退回原始值
        return raw
    try:
        return _plugin_resolve_stream_id({"stream_id": raw, "message": message})
    except Exception:  # noqa: BLE001
        return raw


# ── 安装入口 ──────────────────────────────────────────────────────────


def install(logger: Any = None, config: Any = None) -> str:
    """探测框架是否需要运行时兼容，需要就在**内存里**补一层。

    Args:
        logger: 框架 logger（只用来记 DEBUG / WARNING）；``None`` 时用同名的 stdlib logger。
        config: 插件配置对象（读 ``general.runtime_framework_compat``）；``None`` 时按
            插件配置类的默认值处理（**开**）。

    Returns:
        str: :data:`STATUS_DISABLED` / :data:`STATUS_UNSUPPORTED` /
        :data:`STATUS_ALREADY_PATCHED` / :data:`STATUS_ALREADY_INSTALLED` /
        :data:`STATUS_APPLIED` 之一。

    Raises:
        不抛异常：任何一步失败都只记日志并返回对应状态（最坏等价"没装 shim"）。
    """
    log = logger if logger is not None else logging.getLogger("char_prompt_guard")
    try:
        return _install(log, config)
    except Exception as e:  # noqa: BLE001 —— 兜底：安装失败绝不影响插件加载 / 生图
        _log(
            log,
            "warning",
            f"运行时兼容：安装过程异常，已跳过（不影响生图）: {type(e).__name__}: {e}",
        )
        return STATUS_UNSUPPORTED


def _install(logger: Any, config: Any) -> str:
    """按「配置 → 模块 → 函数 → 检测 → 幂等 → 包装」的固定顺序安装。"""
    # a. 配置开关
    if not _compat_enabled(config):
        _log(
            logger,
            "debug",
            "运行时兼容：general.runtime_framework_compat=false，跳过",
        )
        return STATUS_DISABLED

    # b. 导入宿主模块
    try:
        module = host_module()
    except Exception as e:  # noqa: BLE001 —— 老核心 / 模块被挪走都走这里
        _log(
            logger,
            "debug",
            f"运行时兼容：导入 {HOST_MODULE} 失败，跳过（{type(e).__name__}: {e}）",
        )
        return STATUS_UNSUPPORTED
    if module is None:
        _log(logger, "debug", f"运行时兼容：拿不到 {HOST_MODULE}，跳过")
        return STATUS_UNSUPPORTED

    # c. 取 run_tool_call（不存在 / 不可调用 → 认不出）
    func = getattr(module, TARGET_FUNC, None)
    if not callable(func):
        _log(
            logger,
            "debug",
            f"运行时兼容：{HOST_MODULE} 里没有可调用的 {TARGET_FUNC}，跳过",
        )
        return STATUS_UNSUPPORTED
    if not inspect.iscoroutinefunction(func):
        # 同步函数包成 async 会把调用方搞坏 —— 宁可不装
        _log(
            logger,
            "debug",
            f"运行时兼容：{TARGET_FUNC} 不是协程函数，跳过（不猜它的调用约定）",
        )
        return STATUS_UNSUPPORTED

    # d. 静态补丁检测（**先于**幂等判断：已打补丁的机器一行代码都不执行）
    detection = _detect_static_patch(module, func)
    if detection == "unknown":
        _log(
            logger,
            "debug",
            f"运行时兼容：读不到 {HOST_MODULE} 的源码，无法确认是否已发事件，跳过"
            "（宁可不装，也不重复发事件）",
        )
        return STATUS_UNSUPPORTED
    if detection == "patched":
        _log(
            logger,
            "debug",
            "运行时兼容：检测到框架已含执行前事件"
            "（静态补丁 / 上游原生支持），未重复包装",
        )
        return STATUS_ALREADY_PATCHED

    # e. 幂等：本进程里已经装过
    if getattr(func, WRAP_FLAG, False):
        _log(logger, "debug", "运行时兼容：已安装，跳过")
        return STATUS_ALREADY_INSTALLED

    # f. 原地替换（monkey patch，纯内存）
    wrapper = _make_wrapper(func, logger)
    setattr(module, TARGET_FUNC, wrapper)
    aliases = _install_aliases(func, wrapper)
    _log(
        logger,
        "debug",
        f"运行时兼容：已包装 {HOST_MODULE}.{TARGET_FUNC}"
        f"（顺带更新 {aliases} 处已 import 的别名）",
    )
    return STATUS_APPLIED


# ── 检测 ─────────────────────────────────────────────────────────────


def looks_statically_patched(source: str) -> bool:
    """源码里同时出现「执行前事件」「发布动作」「写回 args」→ 认为已经有人在发事件。"""
    if not source:
        return False
    if not any(marker in source for marker in PRE_EVENT_MARKERS):
        return False
    if PUBLISH_MARKER not in source:
        return False
    return any(marker in source for marker in WRITEBACK_MARKERS)


def _detect_static_patch(module: Any, func: Any) -> str:
    """返回 ``"patched"`` / ``"clean"`` / ``"unknown"``（unknown = 源码读不到）。"""
    targets: list[Any] = [func, module]
    for name in PATCH_CARRIER_FUNCS:
        candidate = getattr(module, name, None)
        if callable(candidate):
            targets.append(candidate)
    sources = [source for source in (_source_of(t) for t in targets) if source]
    if not sources:
        return "unknown"
    return "patched" if any(looks_statically_patched(s) for s in sources) else "clean"


def _source_of(obj: Any) -> str:
    """取源码；读不到（模块不是文件 / 内置 / 已包装）时返回空串。

    带 :data:`WRAP_FLAG` 的对象是**本模块自己**包出来的，永远不当成宿主证据。
    """
    if getattr(obj, WRAP_FLAG, False):
        return ""
    try:
        return inspect.getsource(obj)
    except Exception:  # noqa: BLE001 —— 读不到源码不是错误，按"看不出来"处理
        return ""


# ── 包装 ─────────────────────────────────────────────────────────────


def _make_wrapper(func: Any, logger: Any) -> Any:
    """造包装函数：事件成功才用新 calls，任何异常都退回「原样调用」。"""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        patched: dict[str, Any] | None = None
        try:
            patched = await _prepare_patched_kwargs(kwargs, logger)
        except Exception as e:  # noqa: BLE001 —— 事件失败绝不拦住组件执行
            _log(
                logger,
                "debug",
                "运行时兼容：补发执行前事件失败，按原逻辑执行"
                f"（{type(e).__name__}: {e}）",
            )
            patched = None
        if patched is None:
            return await func(*args, **kwargs)
        return await func(*args, **patched)

    wrapper.__cpg_runtime_wrapped__ = True  # type: ignore[attr-defined]
    return wrapper


def _install_aliases(original: Any, wrapper: Any) -> int:
    """把**已经 import 过**同一函数对象的模块也指到 wrapper 上，返回改动处数。"""
    patched = 0
    for name, module in list(sys.modules.items()):
        if module is None or name == __name__:
            continue
        try:
            value = getattr(module, TARGET_FUNC, None)
        except Exception:  # noqa: BLE001, S112 —— 个别模块的 __getattr__ 会抛异常；读不到就当没这个别名
            continue
        if value is not original:
            continue
        try:
            setattr(module, TARGET_FUNC, wrapper)
        except Exception:  # noqa: BLE001, S112 —— 只读模块跳过；写不进就不写
            continue
        patched += 1
    return patched


async def _prepare_patched_kwargs(
    kwargs: dict[str, Any], logger: Any
) -> dict[str, Any] | None:
    """发布执行前事件并取回订阅者写回的 ``args``。

    Returns:
        dict | None: 需要替换的 kwargs（``calls`` 里带着新 args）；**没有订阅者、
        没有可用调用、或 args 一个都没被改**时返回 ``None``（调用方原样调用原函数）。
    """
    bus = event_bus()
    if bus is None:
        return None
    action_event = event_type(EVENT_ACTION)
    tool_event = event_type(EVENT_TOOL)
    live_action = action_event if _has_subscribers(bus, action_event) else None
    live_tool = tool_event if _has_subscribers(bus, tool_event) else None
    if live_action is None and live_tool is None:
        # 零订阅者：一个事件都不发（与框架发布点 / 静态补丁同一口径）
        return None

    calls = kwargs.get("calls")
    usable_map = kwargs.get("usable_map")
    message = kwargs.get("trigger_msg")
    if not calls or usable_map is None or message is None:
        return None

    stream_id = resolve_stream_id(kwargs.get("stream_id"), message)
    resolve_plugin = kwargs.get("resolve_component_plugin")
    default_plugin = kwargs.get("plugin")

    replaced: list[Any] = []
    changed = False
    for call in calls:
        taken_over: Any = None
        try:
            taken_over = await _publish_for_call(
                bus,
                call,
                usable_map=usable_map,
                message=message,
                stream_id=stream_id,
                resolve_plugin=resolve_plugin,
                default_plugin=default_plugin,
                action_event=live_action,
                tool_event=live_tool,
            )
        except Exception as e:  # noqa: BLE001 —— 单条失败不影响其它调用与本次执行
            _log(
                logger,
                "debug",
                f"运行时兼容：为单次调用补发事件失败，已忽略（{type(e).__name__}: {e}）",
            )
        if taken_over is None:
            replaced.append(call)
        else:
            replaced.append(taken_over)
            changed = True

    if not changed:
        return None
    return {**kwargs, "calls": replaced}


async def _publish_for_call(
    bus: Any,
    call: Any,
    *,
    usable_map: Any,
    message: Any,
    stream_id: str,
    resolve_plugin: Any,
    default_plugin: Any,
    action_event: Any,
    tool_event: Any,
) -> Any:
    """给一次 tool call 发布执行前事件；需要换 args 时返回新的调用对象，否则 ``None``。"""
    args = getattr(call, "args", None)
    if not isinstance(args, dict):
        return None
    call_name = str(getattr(call, "name", "") or "")
    usable_cls = _registry_get(usable_map, call_name)
    kind, component = _component_kind(usable_cls, call_name)
    if kind == "action":
        event = action_event
    elif kind == "tool":
        event = tool_event
    else:
        event = None
    if event is None or not component:
        return None
    if not _has_subscribers(bus, event):
        return None

    signature = _component_signature(usable_cls)
    owner = default_plugin
    if callable(resolve_plugin) and signature:
        try:
            resolved = resolve_plugin(signature)
        except Exception:  # noqa: BLE001 —— 解析不到归属插件时退回默认插件
            resolved = None
        if resolved is not None:
            owner = resolved
    if not signature:
        signature = _fallback_signature(owner, kind, component)

    params: dict[str, Any] = {
        "signature": signature,
        f"{kind}_name": component,
        f"{kind}_description": "",
        "args": dict(args),
        "message": message,
        "stream_id": stream_id,
    }
    _, modified = await bus.publish(event, params)
    new_args = modified.get("args") if isinstance(modified, dict) else None
    if not isinstance(new_args, dict) or new_args == args:
        return None
    return _call_with_args(call, new_args)


def _registry_get(usable_map: Any, call_name: str) -> Any:
    """从工具注册表按调用名取组件类；取不到就是 ``None``（不猜）。"""
    getter = getattr(usable_map, "get", None)
    if not callable(getter):
        return None
    try:
        return getter(call_name)
    except Exception:  # noqa: BLE001
        return None


def _component_kind(usable_cls: Any, call_name: str) -> tuple[str, str]:
    """判断这次调用是 action 还是 tool，并取出组件名（**不实例化组件**）。

    优先用组件签名 ``plugin:action:name`` / ``plugin:tool:name``（与框架原生发布点
    写进事件的字段同一形状）；取不到时退回注册表里的工具名前缀
    （``action-share_photo`` → action / ``share_photo``）。
    """
    signature = _component_signature(usable_cls)
    if signature:
        parts = [part.strip() for part in signature.split(":")]
        if len(parts) >= 3 and parts[1].lower() in _KINDS:
            return parts[1].lower(), parts[2]
    for kind in _KINDS:
        prefix = f"{kind}-"
        if call_name.startswith(prefix) and len(call_name) > len(prefix):
            return kind, call_name[len(prefix) :]
    return "", ""


def _component_signature(usable_cls: Any) -> str:
    """组件签名；组件类没有这个方法 / 抛异常时返回空串。"""
    getter = getattr(usable_cls, "get_signature", None)
    if not callable(getter):
        return ""
    try:
        signature = getter()
    except Exception:  # noqa: BLE001 —— 组件还没注入插件信息时可能抛异常
        return ""
    return signature.strip() if isinstance(signature, str) else ""


def _fallback_signature(plugin: Any, kind: str, component: str) -> str:
    """签名取不到时按 ``插件名:类型:组件名`` 拼一个（插件名也没有就留空串）。"""
    plugin_name = str(getattr(plugin, "plugin_name", "") or "")
    if not plugin_name or not component:
        return ""
    return f"{plugin_name}:{kind}:{component}"


def _call_with_args(call: Any, args: dict[str, Any]) -> Any:
    """返回一份「只换了 args」的调用对象；换不了时返回 ``None``（宁可不回写）。"""
    if _try_set_args(call, args):
        return call
    for rebuild in (_dataclass_replace, _rebuild_call):
        replacement = rebuild(call, args)
        if replacement is not None:
            return replacement
    return None


def _try_set_args(call: Any, args: dict[str, Any]) -> bool:
    """原地改 args（可变对象）；frozen dataclass 等只读对象返回 ``False``。"""
    try:
        call.args = args
    except Exception:  # noqa: BLE001 —— 真实 ToolCall 是 frozen dataclass / slots
        return False
    return True


def _dataclass_replace(call: Any, args: dict[str, Any]) -> Any:
    """按 dataclass 的方式换一份（真实 ``ToolCall`` 就走这条路）。"""
    try:
        return dataclasses.replace(call, args=args)
    except Exception:  # noqa: BLE001 —— 不是 dataclass
        return None


def _rebuild_call(call: Any, args: dict[str, Any]) -> Any:
    """最后一招：按 ``(id, name, args)`` 重新构造一个同类型的调用对象。"""
    try:
        return type(call)(
            id=getattr(call, "id", None),
            name=str(getattr(call, "name", "") or ""),
            args=args,
        )
    except Exception:  # noqa: BLE001
        return None


# ── 小工具 ───────────────────────────────────────────────────────────


def _has_subscribers(bus: Any, event: Any) -> bool:
    """该事件有没有订阅者（总线结构不认识时按"没有"处理）。"""
    if event is None:
        return False
    try:
        return bool(bus.get_subscribers(event))
    except Exception:  # noqa: BLE001
        return False


def _compat_enabled(config: Any) -> bool:
    """读 ``general.runtime_framework_compat``；读不到时按默认（**开**）处理。"""
    if config is None:
        config = _default_config()
    general = getattr(config, "general", None)
    value = getattr(general, "runtime_framework_compat", None)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(value)


def _default_config() -> Any:
    """插件配置对象的默认实例；拿不到时返回 ``None``（按默认开处理）。"""
    try:
        from .config import CharPromptGuardConfig
    except Exception:  # noqa: BLE001 —— 配置类导入失败不该拦住安装
        return None
    try:
        return CharPromptGuardConfig()
    except Exception:  # noqa: BLE001
        return None


def _log(logger: Any, level: str, message: str) -> None:
    """记一条日志；logger 不可用 / 记日志本身失败都静默忽略（兼容层不添乱）。"""
    try:
        getattr(logger, level)(message)
    except Exception:  # noqa: BLE001
        return
