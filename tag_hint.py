"""把「标签写在最前面」直接追加进生图动作的参数说明（1.0.17）。

背景（一次真实事故）
--------------------
调用方（KFC）明明知道「标签 ``Hanabi`` 要挂最前头」，实际写给 ``share_photo`` 的
``content`` 却只有中文外观描述（``清晨…白发少女刚起床…粉色眼睛…``）—— 因为
nai_artist 自己给 ``content`` 写的参数说明是「写画面描述，写给下游翻译模型」，
主模型照着说明写，标签就没了。插件只能靠推断（主题跟踪 / 拆单 / LLM）补，
偶发补错。

1.0.12 起插件用 system reminder（``general.notify_tag_rule``）提醒主模型把标准
标签写在最前面；reminder 是**旁白**，压不过工具定义里逐字的参数说明。
1.0.17 再加一条更强的通道：**直接改动作参数的说明文本**（纯内存改动，不写文件），
让主模型在工具定义里就看到「最前面必须先写标准标签」。

框架里的 schema 生成路径（实测 Neo-MoFox CORE_VERSION 1.2.0）
-------------------------------------------------------------
``BaseAction.to_schema()``（``src/core/components/base/action.py:141``）
→ ``parse_function_signature``（``src/core/components/utils/schema_utils.py:257``）
→ ``_extract_annotated_description``（同文件 :244，取 ``Annotated[...]`` 里第一个
字符串元数据）→ ``param_info["description"]``。

也就是说：**参数说明的源头就是 ``execute`` 的 ``__annotations__``**，而
``to_schema()`` 每次调用都重新 ``inspect``（自身没有二级缓存），LLM 请求侧
（``openai_client._to_openai_tool``，``src/kernel/llm/model_client/openai_client.py:108``）
也是**每次请求现取**。唯一按签名缓存 schema 的地方是
``ActionManager._schema_cache``（``src/core/managers/action_manager.py:51``，
命中在 :172、写入在 :180），公开的清缓存入口是
``action_api.clear_schema_cache``（``src/app/plugin_system/api/action_api.py:204``）。

所以本模块的做法是：**换掉 ``execute.__annotations__[参数]`` 里的说明文本**
（换成 ``Annotated[原类型, 原说明 + 追加句]``，原说明逐字保留在最前面），
再清掉 ActionManager 的 schema 缓存，让下一次生成的工具定义带上追加句。

几条硬性约束
------------
- **只追加、不覆盖**：原说明一字不改，追加句接在换行之后。
- **类型不动**：只替换说明文本；注入后会用框架自己的 ``to_schema()`` 自检
  「说明是不是新的」+「说明以外的字段（type / enum / default…）是不是一字未变」，
  任一条不满足就**还原**并记 WARNING，绝不把第三方动作的 schema 改坏。
- **幂等**：说明里已经含 :data:`HINT_MARKER` 时直接跳过，重复调用只出现一次追加句。
- **静默降级**：找不到动作（还没注册）、结构不认识、注解解析不了……全部只打
  DEBUG/WARNING 并继续。本模块的函数**不向调用方抛异常**，绝不拦住插件加载或生图。
- **纯内存**：不写任何文件，进程重启即恢复原样；卸载时由
  :func:`restore_tag_first_hint` 还原。
"""

from __future__ import annotations

import importlib
import inspect
import types
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, get_type_hints

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("char_prompt_guard")

# ── 追加句 ────────────────────────────────────────────────────────────
#: 追加句里的识别标记：幂等判断、注入后自检、用例断言都用它。
HINT_MARKER = "本参数最前面必须先写角色的标准标签"

#: 追加到提示词参数说明**末尾**的那句话（原说明保持不变，接在它后面）。
#:
#: 1.0.19 起在原句**后面再加一句**「标签必须与本次画面描述的角色一致」：
#: 实测事故里主模型虽然照做把标签写在了最前面，但照搬的是**备忘里过期的固定规则**
#: （「标签必须是 Hanabi 挂最前头」），而这次画的是爱莉希雅 —— 标签与画面不符，
#: 比"没写标签"更糟（下游会按标签锁死成另一个角色）。所以追加句里明确写清：
#: 这次画的是谁就写谁的标准标签，不要照搬备忘 / 历史记录 / 上一张图。
#: 原句（含「标准标签」与 ``Hanabi \\(Honkai: Star Rail\\)`` 示例）**逐字保留**。
HINT_SUFFIX = (
    "附加要求：" + HINT_MARKER + "（例如 Hanabi \\(Honkai: Star Rail\\)），"
    "然后才是画面描述；若角色表未提供标签，则写「角色名（游戏名）」。"
    "标签必须与本次画面描述的角色一致 —— 这次画的是谁就写谁的标准标签，"
    "不要照搬备忘、历史记录或上一张图的标签；标签后面用中文补上角色名，"
    "例如 elysia \\(honkai impact 3rd\\)（爱莉希雅）。"
)

#: 追加句与原说明之间的分隔符（多行说明在工具定义里很常见，换行最清楚）。
HINT_SEPARATOR = "\n"

#: 配置里被当成「关」的字符串取值（TOML 正常给布尔，人工改配置时可能是字符串）。
_FALSY_STRINGS = frozenset({"false", "0", "no", "off", ""})

_STATUS_INJECTED = "injected"
_STATUS_ALREADY = "already"


@dataclass
class TagHintResult:
    """一次「参数说明注入」尝试的结果（供日志与用例断言）。

    Attributes:
        enabled: 开关是否打开（``general.tag_first_hint`` / ``general.enabled`` /
            ``targets.enabled`` 三个都为真）
        injected: 真的改成功的 ``签名.参数名`` 列表（空列表 = 什么都没改）
        already: 说明里本来就带着追加句、本次跳过的 ``签名.参数名`` 列表
        unavailable: 没做成的 ``签名``（未注册）或 ``签名.参数名``（结构不认识 /
            自检未通过）列表 —— 静默降级的具体去向，日志里能看到
    """

    enabled: bool = True
    injected: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否至少真的改成了一个参数说明。"""
        return bool(self.injected)

    def log_line(self) -> str:
        """一行式结果，便于日志排查。"""
        return (
            f"enabled={self.enabled} injected={self.injected} "
            f"already={self.already} unavailable={self.unavailable}"
        )


@dataclass(frozen=True)
class _Patch:
    """单个参数说明改动的结果。

    Attributes:
        status: :data:`_STATUS_INJECTED` 或 :data:`_STATUS_ALREADY`
        original: 改动前的说明文本（还原时要用；``already`` 时为空串）
    """

    status: str
    original: str = ""


@dataclass
class _InjectionState:
    """进程内一次性状态。

    本插件的 ``dependencies`` 是空的，框架用 Kahn + 字典序拓扑排序
    （``loader.PluginLoader.resolve_load_order``），``char_prompt_guard`` 通常排在
    ``image_generator_plugin-neo`` / ``nai_artist`` **前面** —— 也就是说插件加载时
    目标动作多半还没注册。所以除了加载时试一次，还允许在
    ``on_all_plugin_loaded``（全部插件加载完成）或**首个事件**时**再试一次**；
    两个标志位保证「再试」最多发生一次，之后每次事件都是常数级空操作。
    """

    #: 所有目标都已处理完（注入成功 / 已带追加句 / 目标压根不存在）→ 事件钩子直接返回
    finished: bool = False
    #: 「再试一次」的机会已经用掉
    retried: bool = False
    #: 已注入的 ``签名.参数名`` → 原说明文本（卸载时还原用）
    applied: dict[str, str] = field(default_factory=dict)


_state = _InjectionState()


def reset_state() -> None:
    """清空模块级一次性状态（只给用例用，不还原已经改过的注解）。"""
    global _state
    _state = _InjectionState()


# ── 开关 ──────────────────────────────────────────────────────────────


def _as_bool(value: Any, default: bool = True) -> bool:
    """把配置值当布尔读（兼容被写成字符串的 ``"false"`` / ``"0"`` / ``"no"`` / ``"off"``）。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_STRINGS
    return bool(value)


def tag_hint_enabled(config: Any) -> bool:
    """参数说明注入是否开启。

    三个开关都为真才注入：

    - ``general.tag_first_hint``（1.0.17 新增，默认开）—— 本功能自己的开关；
    - ``general.enabled``（默认开）—— 总开关。关掉表示「这个插件整个别管生图」，
      此时连 reminder 都不注册，自然也不该去改第三方动作的参数说明；
    - ``targets.enabled``（默认开）—— 「是否接管目标 Action」。关掉表示不要碰
      生图动作，参数说明也不该碰（否则「关了开关却仍在改动作定义」很费解）。

    配置对象缺失、字段缺失或取值不是布尔时按**默认（开）**处理 —— 与
    ``plugin._tag_rule_enabled`` 同一套容错，字段本身在配置类里一定有。
    """
    general = getattr(config, "general", None)
    targets = getattr(config, "targets", None)
    return (
        _as_bool(getattr(general, "tag_first_hint", None))
        and _as_bool(getattr(general, "enabled", None))
        and _as_bool(getattr(targets, "enabled", None))
    )


# ── 目标动作定位 ──────────────────────────────────────────────────────


def target_signature(spec: Any) -> str:
    """目标动作在框架组件注册表里的签名：``插件名:action:动作名``。

    ``handler.TargetSpec`` 的 ``plugin`` 为空表示「不限制插件」，那种情况下猜签名
    没有意义（可能撞上别插件的同名动作），直接返回空串，调用方会跳过它。

    签名格式来自 ``core.components.types.build_signature``：
    ``f"{plugin_name}:{component_type.value}:{component_name}"``，其中 Action 的
    ``component_type.value`` 就是 ``"action"``（types.py:29）。
    """
    plugin = str(getattr(spec, "plugin", "") or "").strip()
    action = str(getattr(spec, "action", "") or "").strip()
    if not plugin or not action:
        return ""
    return f"{plugin}:action:{action}"


def _default_action_getter(signature: str) -> Any:
    """按签名取 Action 类：先用框架公开 API，拿不到再退回组件注册表。

    两条路都失败（API 不存在 / 管理器没起来 / 注册表里没有）时返回 ``None`` ——
    这不算异常，只说明「这个目标动作此刻还没注册」，调用方记 DEBUG 后继续。
    """
    module: Any = None
    try:
        module = importlib.import_module("src.app.plugin_system.api.action_api")
    except Exception as e:  # noqa: BLE001 —— API 不可用就换注册表
        logger.debug(f"action_api 不可用，改查组件注册表: {type(e).__name__}: {e}")

    getter = getattr(module, "get_action_class", None)
    if callable(getter):
        try:
            found = getter(signature)
        except Exception as e:  # noqa: BLE001 —— 参数校验失败等一律当作没找到
            logger.debug(f"action_api.get_action_class({signature!r}) 失败: {e}")
            found = None
        if found is not None:
            return found

    try:
        from src.core.components.registry import get_global_registry

        return get_global_registry().get(signature)
    except Exception as e:  # noqa: BLE001 —— 注册表也没有就真的没有
        logger.debug(f"组件注册表不可用: {type(e).__name__}: {e}")
        return None


def _default_cache_clearer(signature: str) -> None:
    """清掉框架按签名缓存的 Action schema（**注入后必须清**）。

    为什么必须清：``ActionManager._schema_cache`` 把 ``to_schema()`` 的结果缓存起来
    （``action_manager.py:51`` 建、:172 命中、:180 写入），命中就直接返回**旧 dict**。
    我们改的是动作类的注解，只有把缓存丢掉、让下次 ``get_action_schema`` 重新生成，
    追加的那句话才会出现在给模型的工具定义里。（LLM 请求路径本身是每次
    ``to_schema()`` 现取，不吃这个缓存；清它是为了 API / 其它消费者也一致。）

    公开入口 ``action_api.clear_schema_cache``；拿不到 API 时退回直接操作
    ``ActionManager._schema_cache`` —— 纯内存 dict，pop 掉没有任何副作用。
    """
    module: Any = None
    try:
        module = importlib.import_module("src.app.plugin_system.api.action_api")
    except Exception as e:  # noqa: BLE001 —— 换下一条路径
        logger.debug(f"action_api 不可用，改用 ActionManager 清缓存: {e}")

    clearer = getattr(module, "clear_schema_cache", None)
    if callable(clearer):
        try:
            clearer(signature)
            return
        except Exception as e:  # noqa: BLE001 —— 换下一条路径
            logger.debug(f"action_api.clear_schema_cache({signature!r}) 失败: {e}")

    try:
        from src.core.managers.action_manager import get_action_manager

        cache = getattr(get_action_manager(), "_schema_cache", None)
        if isinstance(cache, dict):
            cache.pop(signature, None)
    except Exception as e:  # noqa: BLE001 —— 清不掉也不影响功能（下次仍用旧 schema）
        logger.debug(f"清 schema 缓存失败（不影响生图）: {type(e).__name__}: {e}")


# ── 真正的注入 ────────────────────────────────────────────────────────


def _schema_param(action_cls: Any, param: str) -> dict[str, Any] | None:
    """取 ``to_schema()`` 里该参数的 schema 片段（拿不到返回 ``None``）。

    走框架自己的 schema 生成器，而不是自己去解析注解：说明可能来自
    ``Annotated`` 元数据、也可能来自 docstring 的 ``Args:`` 段，两条路都要照顾到。
    """
    to_schema = getattr(action_cls, "to_schema", None)
    if not callable(to_schema):
        logger.debug(f"{action_cls!r} 没有 to_schema()，跳过参数说明注入")
        return None
    try:
        schema = to_schema()
        param_schema = schema["function"]["parameters"]["properties"][param]
    except Exception as e:  # noqa: BLE001 —— 结构不认识就静默降级
        logger.debug(
            f"读取 {action_cls!r} 的参数 {param!r} 说明失败，跳过注入: "
            f"{type(e).__name__}: {e}"
        )
        return None
    return param_schema if isinstance(param_schema, dict) else None


def _verify_rewrite(
    action_cls: Any, param: str, before: dict[str, Any], expected: str
) -> bool:
    """注入后自检：说明正好是 ``expected``，且**说明以外的字段一字未变**。

    这一步是为了不把第三方动作的 schema 改坏：类型（``type`` / ``enum``）、
    默认值、其它键都必须与注入前完全一致；同时说明必须真的是我们写的那句
    （框架若绕开注解、从别处重新生成说明，这里会失败 → 调用方还原）。
    """
    after = _schema_param(action_cls, param)
    if after is None:
        return False
    if after.get("description") != expected:
        return False
    rest_before = {k: v for k, v in before.items() if k != "description"}
    rest_after = {k: v for k, v in after.items() if k != "description"}
    return rest_before == rest_after


def _clone_function(func: types.FunctionType) -> types.FunctionType:
    """复制一个「同代码、同全局、同闭包」的函数对象（只给注解动刀时用）。

    只用于 ``execute`` 是**继承**来的情形：那时直接改 ``func.__annotations__``
    会连父类和所有共享同一个 ``execute`` 的兄弟动作一起改掉，所以给本类装一份
    副本，只改副本的注解。代码对象 / 全局命名空间 / 闭包 / 默认值全部沿用，
    行为与原来完全一致（异步生成器函数的标志位也在 code 对象里，一并保留）。
    """
    clone = types.FunctionType(
        func.__code__,
        func.__globals__,
        func.__name__,
        func.__defaults__,
        func.__closure__,
    )
    clone.__kwdefaults__ = func.__kwdefaults__
    clone.__doc__ = func.__doc__
    clone.__module__ = func.__module__
    clone.__qualname__ = func.__qualname__
    clone.__dict__.update(func.__dict__)
    return clone


def _install_description(
    action_cls: Any, param: str, description: str
) -> Callable[[], None] | None:
    """把 ``param`` 的说明文本换成 ``description``（**类型保持不变**）。

    Returns:
        成功时返回「还原函数」（调用它即可回到改动前），失败返回 ``None``。
        本函数不抛异常。
    """
    func = getattr(action_cls, "execute", None)
    if not isinstance(func, types.FunctionType):
        logger.debug(
            f"{action_cls!r}.execute 不是普通函数（{type(func).__name__}），跳过注入"
        )
        return None

    owner = action_cls.__dict__.get("execute")
    if owner is not None and owner is not func:
        # 类属性不是这个函数对象（staticmethod / classmethod / 装饰器对象……）：
        # 不认识的结构一律不动
        logger.debug(f"{action_cls!r}.execute 的类属性不是普通函数，跳过注入")
        return None

    # 解析注解拿到「原类型」：说明换成新的，类型必须一字不改。
    # 用与框架同一句 get_type_hints（include_extras=True），PEP 563 的字符串注解
    # （两个上游生图插件都写了 from __future__ import annotations）也能正确解析；
    # 解析不了时**宁可什么都不做**，绝不猜类型。
    try:
        resolved = get_type_hints(func, include_extras=True)
    except Exception as e:  # noqa: BLE001
        logger.debug(
            f"解析 {action_cls!r}.execute 的注解失败，跳过注入: {type(e).__name__}: {e}"
        )
        return None

    raw = resolved.get(param, inspect.Parameter.empty)
    if getattr(raw, "__metadata__", None) is not None:
        # Annotated[X, ...] → 取 X（框架的 _extract_annotated_description 也这么取）
        base_type = getattr(raw, "__origin__", None)
    else:
        base_type = raw
    if base_type is inspect.Parameter.empty or base_type is None:
        # 参数没有注解时框架本来就把类型退化成 string（map_type_to_json 的兜底），
        # 这里用 str 写出等价的结果；自检会再确认一次类型没变
        base_type = str

    try:
        new_annotation = Annotated[base_type, description]
    except Exception as e:  # noqa: BLE001 —— 类型不认识就放弃
        logger.debug(f"构造新注解失败，跳过注入: {type(e).__name__}: {e}")
        return None

    old_annotations = func.__annotations__
    new_annotations = dict(old_annotations)
    new_annotations[param] = new_annotation

    try:
        if owner is func:
            # 本类自己定义的 execute：只换注解字典，函数对象一个字节都不动
            func.__annotations__ = new_annotations

            def rollback() -> None:
                func.__annotations__ = old_annotations

        else:
            # 继承来的 execute：给本类装一份副本，绝不改父类的函数对象
            clone = _clone_function(func)
            clone.__annotations__ = new_annotations
            action_cls.execute = clone

            def rollback() -> None:
                if action_cls.__dict__.get("execute") is clone:
                    delattr(action_cls, "execute")

    except Exception as e:  # noqa: BLE001 —— 装不上去就当作没做成
        logger.debug(
            f"写入 {action_cls!r} 的注解失败，跳过注入: {type(e).__name__}: {e}"
        )
        return None

    return rollback


def _inject_into_action(action_cls: Any, param: str) -> _Patch | None:
    """把追加句追加到 ``action_cls`` 的 ``param`` 参数说明末尾。

    Returns:
        :class:`_Patch`（改成功 / 本来就带着追加句）；``None`` 表示没做成
        （结构不认识、注解解析不了、自检未通过 —— 自检未通过时**已经还原**）。
        本函数不抛异常。
    """
    before = _schema_param(action_cls, param)
    if before is None:
        return None

    description = before.get("description")
    if not isinstance(description, str) or not description.strip():
        logger.debug(f"{action_cls!r} 的参数 {param!r} 没有可追加的说明文本，跳过")
        return None
    description = description.strip()
    if HINT_MARKER in description:
        # 幂等：已经追加过（加载时试过一次、事件里又试一次也会走到这里）
        return _Patch(status=_STATUS_ALREADY)

    new_description = f"{description}{HINT_SEPARATOR}{HINT_SUFFIX}"
    rollback = _install_description(action_cls, param, new_description)
    if rollback is None:
        return None

    if not _verify_rewrite(action_cls, param, before, new_description):
        logger.warning(
            f"{action_cls!r} 的参数 {param!r} 追加「标签在最前」提示后自检未通过"
            "（类型变了，或参数说明被框架从别处重新生成），已还原、本次不生效"
        )
        try:
            rollback()
        except Exception:
            logger.debug(f"还原 {action_cls!r} 的 {param!r} 说明失败", exc_info=True)
        return None

    return _Patch(status=_STATUS_INJECTED, original=description)


def inject_tag_first_hint(
    targets: Iterable[Any],
    *,
    config: Any = None,
    action_getter: Callable[[str], Any] | None = None,
    cache_clearer: Callable[[str], None] | None = None,
) -> TagHintResult:
    """给所有**已注册**的目标动作追加「标签写在最前面」这句参数说明。

    Args:
        targets: 目标动作表（``handler.TargetSpec`` 列表；只用到它的 ``plugin`` /
            ``action`` / ``prompt_arg`` 三个字段 —— 这里**故意不 import handler**，
            免得和 handler 形成循环依赖）
        config: 插件配置；``general.tag_first_hint`` / ``general.enabled`` /
            ``targets.enabled`` 任一为假时直接返回 ``enabled=False``，
            **一个字段都不改**
        action_getter: ``签名 -> Action 类`` 的取用函数；默认走框架公开 API
            （``action_api.get_action_class``）再退回组件注册表。用例用它注入
            假的「动作注册表」
        cache_clearer: ``签名 -> None`` 的清缓存函数；默认走
            ``action_api.clear_schema_cache``
    """
    result = TagHintResult()
    if not tag_hint_enabled(config):
        result.enabled = False
        logger.debug(
            "tag_first_hint / general.enabled / targets.enabled 有开关关着，"
            "不改动任何第三方动作的参数说明"
        )
        return result

    getter = action_getter or _default_action_getter
    clearer = cache_clearer or _default_cache_clearer

    for spec in targets:
        signature = target_signature(spec)
        param = str(getattr(spec, "prompt_arg", "") or "").strip()
        if not signature or not param:
            continue
        key = f"{signature}.{param}"

        try:
            action_cls = getter(signature)
        except Exception as e:  # noqa: BLE001 —— 取用函数自己出问题也不许炸
            logger.debug(f"取动作 {signature} 失败，跳过: {type(e).__name__}: {e}")
            action_cls = None
        if action_cls is None:
            # 目标动作还没注册（本插件常比生图插件先加载完）：不是错误，
            # 留给 on_all_plugin_loaded / 首个事件再试一次
            result.unavailable.append(signature)
            logger.debug(f"目标动作 {signature} 尚未注册，本次跳过参数说明注入")
            continue

        try:
            patch = _inject_into_action(action_cls, param)
        except Exception as e:  # noqa: BLE001 —— 双保险：内部已全程 try/except
            logger.debug(f"注入 {key} 的参数说明失败: {type(e).__name__}: {e}")
            patch = None

        if patch is None:
            result.unavailable.append(key)
            continue
        if patch.status == _STATUS_ALREADY:
            result.already.append(key)
            logger.debug(f"{key} 的参数说明已含「标签在最前」提示，跳过（幂等）")
            continue

        result.injected.append(key)
        _state.applied[key] = patch.original
        try:
            clearer(signature)
        except Exception as e:  # noqa: BLE001 —— 清缓存失败只影响下次读取，不影响生图
            logger.debug(f"清 {signature} 的 schema 缓存失败: {type(e).__name__}: {e}")
        logger.info(
            f"已为 {signature} 的参数说明追加「标签在最前」提示（参数 {param}）"
        )

    return result


# ── 时机：加载时 / 全部插件加载完成 / 首次生图事件 ────────────────────


def _finish_if_complete(result: TagHintResult) -> None:
    """所有目标都已处理（没有"未注册"的）时收工，后续事件不再尝试。"""
    if result.enabled and not result.unavailable:
        _state.finished = True


def inject_on_load(
    targets: Iterable[Any], *, config: Any = None
) -> TagHintResult | None:
    """插件加载（``on_plugin_loaded``）时的第一次尝试。**不抛异常。**

    加载顺序不保证：本插件 ``dependencies`` 为空，拓扑排序通常把它排在生图插件
    **前面**，这时目标动作还没注册。那种情况下这里什么都不做（连 WARNING 都不打，
    只记 DEBUG），留给 :func:`maybe_inject_on_event` 再试一次。
    """
    try:
        result = inject_tag_first_hint(targets, config=config)
    except Exception as e:  # noqa: BLE001 —— 对调用方保证不抛异常
        logger.debug(f"加载时注入参数说明失败（已忽略）: {type(e).__name__}: {e}")
        return None

    if result.enabled:
        logger.debug(f"加载时注入参数说明: {result.log_line()}")
    if result.unavailable:
        logger.debug(
            "以下目标动作尚未注册（或参数说明结构不认识），"
            f"将在全部插件加载完成 / 首次生图事件时再试一次：{result.unavailable}"
        )
    _finish_if_complete(result)
    return result


def maybe_inject_on_event(
    targets: Iterable[Any], *, config: Any = None
) -> TagHintResult | None:
    """「全部插件加载完成」或**首个事件**时的一次性重试。**不抛异常。**

    用两个一次性标志位避免"每次事件都试一遍"：``_state.finished``（所有目标都已
    处理完）与 ``_state.retried``（再试的机会已用掉）。之后每次事件只做一次布尔判断。

    开关关着时**不消耗**这次机会（只多一次 getattr）：这样运行期把
    ``tag_first_hint`` 改成 true 之后，下一个事件仍然能补上。

    Returns:
        本次真的尝试了则返回 :class:`TagHintResult`；没做事返回 ``None``。
    """
    if _state.finished or _state.retried:
        return None
    if not tag_hint_enabled(config):
        return None
    _state.retried = True
    logger.debug("再尝试一次把「标签在最前」写进生图动作的参数说明")
    try:
        result = inject_tag_first_hint(targets, config=config)
    except Exception as e:  # noqa: BLE001 —— 对调用方保证不抛异常
        logger.debug(f"注入参数说明失败（已忽略，不影响生图）: {type(e).__name__}: {e}")
        return None
    logger.debug(f"重试注入参数说明: {result.log_line()}")
    _finish_if_complete(result)
    return result


# ── 还原（插件卸载） ──────────────────────────────────────────────────


def restore_tag_first_hint(
    *,
    action_getter: Callable[[str], Any] | None = None,
    cache_clearer: Callable[[str], None] | None = None,
) -> list[str]:
    """把追加过的说明还原成原文（插件卸载时调用）。**不抛异常。**

    参数说明是**内存内**改动，插件卸载后不该继续影响第三方动作。还原只需要把说明
    换回记录下来的原文 —— 类型沿用当前注解的 ``__origin__``（就是注入时用的原类型），
    再清一次 schema 缓存。

    Returns:
        还原成功的 ``签名.参数名`` 列表。
    """
    getter = action_getter or _default_action_getter
    clearer = cache_clearer or _default_cache_clearer
    restored: list[str] = []

    for key, original in list(_state.applied.items()):
        signature, _, param = key.rpartition(".")
        if not signature or not param or not original:
            continue
        try:
            action_cls = getter(signature)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"还原时取动作 {signature} 失败: {type(e).__name__}: {e}")
            continue
        if action_cls is None:
            # 动作已经没了（插件被卸载/重载）：没什么可还原的
            _state.applied.pop(key, None)
            continue

        before = _schema_param(action_cls, param)
        if before is None:
            continue
        rollback = _install_description(action_cls, param, original)
        if rollback is None:
            continue
        if not _verify_rewrite(action_cls, param, before, original):
            logger.debug(f"还原 {key} 的参数说明后自检未通过，已还原本次还原改动")
            try:
                rollback()
            except Exception:
                logger.debug(f"回滚还原操作失败: {key}", exc_info=True)
            continue

        _state.applied.pop(key, None)
        try:
            clearer(signature)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"还原后清 {signature} 的 schema 缓存失败: {e}")
        restored.append(key)
        logger.debug(f"已还原 {key} 的参数说明")

    if restored:
        logger.info(f"已还原生图动作的参数说明（插件卸载）：{restored}")
    return restored


__all__ = [
    "HINT_MARKER",
    "HINT_SEPARATOR",
    "HINT_SUFFIX",
    "TagHintResult",
    "inject_on_load",
    "inject_tag_first_hint",
    "maybe_inject_on_event",
    "reset_state",
    "restore_tag_first_hint",
    "tag_hint_enabled",
    "target_signature",
]
