r"""char_prompt_guard 逻辑自检。

在框架环境里真实加载插件、构造 ``before_action_call`` 事件，验证
「检测 → 候选提取 → 注入 → 回写 args」的完整行为，覆盖自然语言模式
（nai_artist）、标签模式（image_generator_plugin-neo）、多角色同框、
运行时标记、自定义目标表、主角粘性/用户点名（换人 vs 追加、时效）、
二次一致性检测（warn/fix）等场景。

判定优先级（1.0.10 起）：**描述点名 > 用户最近点名(粘性) > LLM 仲裁 > 不补**。
C28/C29/C32 这三条守住的是"上文里**非用户点名**的旧角色不得覆盖描述/用户结论"，
因此它们的旧角色提及由 **bot 复述** 提供（``is_bot=True``）—— 真实环境里这个
身份来自 ``adapter_api.get_bot_info_by_platform`` 的 bot_id 比对。

1.0.12 起另有三条收紧，对应 C42~C45：

1. system reminder 增加「生图参数最前面先写角色标准标签」的硬规则
   （``general.notify_tag_rule``，默认开）；
2. 改口词「还是」改为**判语境**（选择疑问句「是 A 还是你？」不算改口），
   并在点名识别前剥掉 ``[回复…]`` / ``[图片(…)]`` 这类中括号块（C42/C43）；
3. 自我指代（``本尊`` / ``你本人`` / ``你自己`` …）判定为**不补**，检查范围是
   「画面描述 + 触发消息 + 触发之前最近 N 条用户消息」（C44/C45）。

1.0.13 起自我指代不再一律不补（C46~C48）：先按 ``general.true_self`` →
框架人格配置 ``personality.nickname`` → ``alias_names`` 取「本尊身份」，
再在角色表里按「名字 → 别名 → 标签」定位 —— **命中就正常注入该角色**，
没命中或完全取不到身份名才不补（日志会写明原因）。C47/C48 通过 monkeypatch
替换 ``handler._read_personality_identity`` 来模拟人格配置（不必真的起框架运行时）。

1.0.14 起「这次该画谁」交给**主题跟踪器**（``monitor.track_subject``，C49~C53）：
按时间正序扫最近 ``monitor.window`` 条消息维护一个「当前主题」，置信度 =
信号强度 × 时间衰减（``exp(-Δt / half_life)``），低于 ``monitor.min_confidence``
就不补。C49 主题切换（旧主题 →「本尊」→ 花火）、C50 多人同框、C51 代词回指、
C52 置信度不足不补、C53 ``monitor.enabled = false`` 完全退回 1.0.13。

1.0.16 起补上**拆单（一单多件）**（C54/C55）：**同一条**用户消息里既有自我指代词、
又有角色表内角色（「本尊看看，然后穿着爱莉来看看～」＝调用方把一单拆成两张图）时，
主题「不可单独确定」—— 不再让「角色名」把自我指代吞掉（1.0.15 的事故），改为
**强制**问一次 LLM 并按**当前画面描述**判定：LLM 给出角色表内结论就采用（C54），
LLM 没结论就不补（C55）。两者分别出现在不同消息里时行为不变（最后一条点名生效）。

1.0.17 起多一条**让模型自己写标签**的通道（C56~C59）：把「最前面必须先写角色的
标准标签」**直接追加进生图动作的参数说明**（``general.tag_first_hint``，纯内存改
``execute`` 的注解，不写文件）—— 主模型在工具定义里就能看到，比 system reminder
这种旁白强得多。C56 用假 Action（注解是 PEP 563 字符串，与两个上游生图插件写法
一致）跑通「原说明保留 + 追加句进入 schema + 类型没被改坏 + schema 缓存被清」；
C57 关掉开关后说明**一字不改**；C58 空注册表**不抛异常**、返回「未注入」标记；
C59 连续调用只追加一次（幂等），并且卸载时能把说明**还原**成原文。

用法（用实例自带 venv）：

    $env:NEO_ROOT="<你的 Neo-MoFox 框架根>"
    & "$env:NEO_ROOT\.venv\Scripts\python.exe" <插件目录>\tests\check_logic.py

默认只跑离线用例（零成本）。加 ``--llm`` 会额外跑两条需要真调小模型的
用例（角色表为空时的兜底提取；提示词已写表内角色时识别表外角色）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Self

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001, S110 —— 控制台不支持重配置时忽略即可，输出编码由终端决定
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

# 相对导入（from ..config import ...）要求本模块属于 char_prompt_guard.tests 包。
# 直接 `python tests\check_logic.py` 时 __package__ 为空，相对导入会抛
# "attempted relative import with no known parent package"；这里补上包名，
# 于是两种跑法都能用：
#   python <插件目录>\tests\check_logic.py
#   python -m char_prompt_guard.tests.check_logic
if __package__ in (None, ""):
    __package__ = "char_prompt_guard.tests"

from src.kernel.event import EventDecision

from ..config import CharPromptGuardConfig
from ..handler import CharacterGuardHandler

#: 示例角色表（第 4 列是 NAI 标准标签，tags 模式要用）
RS = (
    r"爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | elysia \(honkai impact 3rd\)"
    "\n"
    r"伊甸 | 崩坏三 (Honkai Impact 3rd) | eden | eden \(honkai impact 3rd\)"
)
RS_ONE = r"爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | elysia \(honkai impact 3rd\)"
#: 三角色表（爱莉希雅 / 花火 / 流萤）：一致性检测、多人场景用例用
RS3 = (
    r"爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | elysia \(honkai impact 3rd\)"
    "\n"
    r"花火 | 崩坏：星穹铁道 (Honkai: Star Rail) | hanabi,花火 | hanabi \(honkai: star rail\)"
    "\n"
    r"流萤 | 崩坏：星穹铁道 (Honkai: Star Rail) | firefly,流萤 | firefly \(honkai: star rail\)"
)
TAG_ELYSIA = r"elysia \(honkai impact 3rd\)"
TAG_EDEN = r"eden \(honkai impact 3rd\)"
TAG_HANABI = r"hanabi \(honkai: star rail\)"

_results: list[tuple[str, bool, str]] = []
RUN_LLM = "--llm" in sys.argv


class FakePlugin:
    """最小替身：handler 只用到 .config。"""

    def __init__(self, cfg: object) -> None:
        self.config = cfg


class FakeMessage:
    """假消息：stream_id 留空 → 不读数据库，只用这条触发消息当上文。"""

    def __init__(self, text: str, stream_id: str = "") -> None:
        self.stream_id = stream_id
        self.processed_plain_text = text
        self.content = text
        self.sender_name = "用户"


def make_config(
    *,
    roster: str = RS,
    targets: str = "",
    apply_modes: str = "drawing",
    use_llm: bool = False,
    multi_enabled: bool = True,
    max_characters: int = 6,
    general_enabled: bool = True,
) -> CharPromptGuardConfig:
    """按用例参数构造配置。"""
    cfg = CharPromptGuardConfig()
    cfg.general.enabled = general_enabled
    cfg.general.apply_modes = apply_modes
    cfg.general.use_llm = use_llm
    cfg.roster.characters = roster
    cfg.targets.list = targets
    cfg.multi.enabled = multi_enabled
    cfg.multi.max_characters = max_characters
    return cfg


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并打印一条用例结果。"""
    print(
        f"[{'PASS' if ok else 'FAIL'}] {name}"
        + (f"\n        {detail}" if detail else "")
    )
    _results.append((name, ok, detail))


def history_message(
    text: str,
    minutes_ago: float,
    *,
    is_bot: bool = False,
    sender_id: str = "u-1",
) -> dict:
    """构造一条框架形状的历史消息（字段对齐 message_api._rows_to_message_dicts）。

    ``get_recent_messages(limit_mode="latest")`` 先按 ``-time`` 取最近 N 条，
    再把结果按 ``time`` **升序**返回，所以历史窗口是「旧 → 新」。
    真实环境里「这条是不是 bot 自己说的」要靠
    ``adapter_api.get_bot_info_by_platform`` 拿 bot_id 比对；stub 环境没有
    adapter_api，所以这里用 ``is_bot`` 显式标注（插件对两者的处理一致）。
    """
    return {
        "time": time.time() - minutes_ago * 60.0,
        "stream_id": "s-stub",
        "person_id": "bot-1" if is_bot else sender_id,
        "sender_id": "bot-1" if is_bot else sender_id,
        "sender_name": "bot" if is_bot else "用户",
        "is_bot": is_bot,
        "processed_plain_text": text,
        "content": text,
        "platform": "stub",
    }


class FakeHistory:
    """上下文管理器：替换 handler 读取上文的 ``get_recent_messages``。

    只在 with 块内生效，退出时恢复原函数。配合 ``history_cache_ttl = 0``
    使用，避免进程内缓存把上一个用例的历史带进来。
    """

    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self._original = None

    def __enter__(self) -> Self:
        from .. import handler as handler_module

        self._original = handler_module.get_recent_messages

        async def fake_get_recent_messages(stream_id, hours=24.0, limit=100, **kwargs):
            return [dict(item) for item in self.messages]

        handler_module.get_recent_messages = fake_get_recent_messages
        return self

    def __exit__(self, *exc_info) -> bool:
        from .. import handler as handler_module

        if self._original is not None:
            handler_module.get_recent_messages = self._original
        return False


class _ListHandler(logging.Handler):
    """把日志记录收进列表。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class LogCapture:
    """上下文管理器：抓取 ``char_prompt_guard`` 的日志（含 DEBUG）。

    判定依据是 DEBUG 级别的诊断日志，用户核对时就是看这些行，所以用例里
    真的把它抓下来断言，而不是只看最终内容。
    """

    def __init__(self, name: str = "char_prompt_guard") -> None:
        self.logger = logging.getLogger(name)
        self.handler = _ListHandler()
        self._old_level: int | None = None

    def __enter__(self) -> Self:
        self._old_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *exc_info) -> bool:
        self.logger.removeHandler(self.handler)
        if self._old_level is not None:
            self.logger.setLevel(self._old_level)
        return False

    def debugs(self) -> str:
        """DEBUG 级记录（判定依据、一致性检查结果）。"""
        return "\n".join(
            record.getMessage()
            for record in self.handler.records
            if record.levelno == logging.DEBUG
        )

    def warnings(self) -> str:
        """WARNING 及以上记录。"""
        return "\n".join(
            record.getMessage()
            for record in self.handler.records
            if record.levelno >= logging.WARNING
        )


async def fire(cfg: CharPromptGuardConfig, params: dict) -> dict:
    """触发一次 before_action_call，返回（可能被回写的）params。"""
    handler = CharacterGuardHandler(FakePlugin(cfg))
    decision, out = await handler.execute("before_action_call", copy.deepcopy(params))
    if decision is not EventDecision.SUCCESS:
        raise AssertionError(f"decision 应为 SUCCESS，实际 {decision}")
    return out


def natural_params(
    content: str, mode: str = "drawing", text: str = "我们来聊爱莉希雅吧"
) -> dict:
    """nai_artist:share_visual 的事件参数。"""
    return {
        "signature": "nai_artist:action:share_visual",
        "action_name": "share_visual",
        "action_description": "画一张画",
        "args": {"content": content, "mode": mode},
        "message": FakeMessage(text),
    }


def tags_params(
    content: str, text: str = "好想看她俩同框", extra: dict | None = None
) -> dict:
    """image_generator_plugin-neo:draw_image 的事件参数。"""
    args = {"content_description": content}
    if extra:
        args.update(extra)
    return {
        "signature": "image_generator_plugin-neo:action:draw_image",
        "action_name": "draw_image",
        "action_description": "NovelAI 绘图",
        "args": args,
        "message": FakeMessage(text),
    }


# ── 参数说明注入（1.0.17，C56~C59）用的假动作 ──────────────────────────
#
# 假动作**不继承** BaseAction，但 ``to_schema()`` 走框架真正的 schema 生成器
# （``parse_function_signature``），所以它们是"改注解 → 框架生成参数说明"这条
# 链路的真实替身；不必起组件运行时，也就能在离线用例里验。
#
# 本文件顶部有 ``from __future__ import annotations``，因此下面这些注解在运行时
# 都是**字符串**（PEP 563），与 nai_artist / image_generator_plugin-neo 的写法
# 完全一致 —— 覆盖的正是真实环境里最难的那条路径（要靠 get_type_hints 解析）。

#: 假 nai_artist ``share_photo.content`` 的原始说明（Annotated 元数据）
FAKE_PHOTO_DESC = "原始说明：写画面描述，写给下游翻译模型"
#: 假 image_generator_plugin-neo ``draw_image.content_description`` 的原始说明
FAKE_TAGS_DESC = "原始说明：完整正面提示词，按模型的语言与权重规则组织"


class _FakeActionBase:
    """假动作的公共部分：``to_schema()`` 用框架自己的 schema 生成器。"""

    name: str = "fake_action"
    description: str = "假的生图动作"

    @classmethod
    def to_schema(cls) -> dict:
        """对齐 ``BaseAction.to_schema()``（action.py:141）的调用方式。"""
        from src.core.components.utils.schema_utils import parse_function_signature

        return parse_function_signature(
            cls.execute, f"action-{cls.name}", cls.description
        )


class _FakePhotoAction(_FakeActionBase):
    """nai_artist:share_photo 的替身（提示词参数 ``content``）。"""

    name = "share_photo"

    async def execute(
        self, content: Annotated[str, FAKE_PHOTO_DESC]
    ) -> tuple[bool, str]:
        """执行生图并发送。

        Args:
            content: 自然语言画面描述
        """
        return True, content


class _FakeTagsAction(_FakeActionBase):
    """image_generator_plugin-neo:draw_image 的替身（参数 ``content_description``）。"""

    name = "draw_image"

    async def execute(
        self, content_description: Annotated[str, FAKE_TAGS_DESC]
    ) -> tuple[bool, str]:
        """执行画图动作。

        Args:
            content_description: 完整正面提示词
        """
        return True, content_description


def param_description(action_cls: type, param: str) -> str:
    """读一个动作类当前生成的参数说明（走框架的 schema 生成器）。"""
    return action_cls.to_schema()["function"]["parameters"]["properties"][param][
        "description"
    ]


def action_getter_for(registry: dict[str, type] | None = None):
    """造一个假的「动作注册表」：签名 → 假 Action 类（空表 = 什么都没注册）。"""
    table = dict(registry or {})

    def getter(signature: str):
        return table.get(signature)

    return getter


async def run_cases() -> int:
    """跑全部用例。"""
    print(f"框架根   : {NEO_ROOT}")
    print(f"插件目录 : {PLUGIN_DIR}")
    print(f"LLM 用例 : {'开' if RUN_LLM else '关（加 --llm 打开）'}")
    print("=" * 72)

    # ── C1 natural：未含角色 → 补入 ────────────────────────────────
    try:
        out = await fire(make_config(), natural_params("帮我画一张她战斗的样子"))
        c = out["args"]["content"]
        ok = (
            "爱莉希雅" in c and "崩坏三" in c and c.startswith("帮我画一张她战斗的样子")
        )
        check("C1 natural 补角色", ok, f"content = {c!r}")
    except Exception as e:  # noqa: BLE001
        check("C1 natural 补角色", False, f"{type(e).__name__}: {e}")

    # ── C2 natural：已含角色 → 不再追加中文说明（1.0.6 起可补标准标签）──
    try:
        raw = "爱莉希雅站在月光下，手持长枪"
        out = await fire(make_config(), natural_params(raw))
        c = out["args"]["content"]
        check(
            "C2 已含角色时不再追加中文说明",
            c.startswith(raw) and "画面角色" not in c and c.count("爱莉希雅") == 1,
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C2 已含角色时不再追加中文说明", False, f"{type(e).__name__}: {e}")

    # ── C3 photo 模式 → 不接管 ─────────────────────────────────────
    try:
        raw = "帮我画一张她战斗的样子"
        out = await fire(make_config(), natural_params(raw, mode="photo"))
        c = out["args"]["content"]
        check(
            "C3 photo 模式不接管（apply_modes=drawing）", c == raw, f"content = {c!r}"
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C3 photo 模式不接管（apply_modes=drawing）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C4 tags 模式：追加标准角色标签 ─────────────────────────────
    try:
        raw = "1girl, solo, night city, rooftop"
        out = await fire(make_config(), tags_params(raw, text="我们来聊爱莉希雅吧"))
        c = out["args"]["content_description"]
        ok = TAG_ELYSIA in c and c.startswith("1girl, solo")
        check("C4 tags 模式追加 NAI 标签", ok, f"content_description = {c!r}")
    except Exception as e:  # noqa: BLE001
        check("C4 tags 模式追加 NAI 标签", False, f"{type(e).__name__}: {e}")

    # ── C5 tags 模式多人 → 写入 characters 槽位 + 坐标 ─────────────
    try:
        out = await fire(
            make_config(),
            tags_params("2girls, standing together", text="画爱莉希雅和伊甸站一起"),
        )
        slot = out["args"].get("characters")
        prompts: list[str] = []
        coords: list[tuple] = []
        if slot:
            data = json.loads(slot)
            prompts = [d.get("prompt") for d in data]
            coords = [(d.get("x"), d.get("y")) for d in data]
        ok = (
            len(prompts) == 2
            and TAG_ELYSIA in prompts
            and TAG_EDEN in prompts
            and coords == [(0.333, 0.5), (0.667, 0.5)]
        )
        check("C5 多人 → characters 槽位 + spread 坐标", ok, f"characters = {slot!r}")
    except Exception as e:  # noqa: BLE001
        check(
            "C5 多人 → characters 槽位 + spread 坐标", False, f"{type(e).__name__}: {e}"
        )

    # ── C6 #nochar# → 移除标记且不补 ───────────────────────────────
    try:
        out = await fire(make_config(), natural_params("画一张空荡的街道 #nochar#"))
        c = out["args"]["content"]
        ok = "#nochar#" not in c and "爱莉希雅" not in c and "空荡的街道" in c
        check("C6 #nochar# 移除标记且不补角色", ok, f"content = {c!r}")
    except Exception as e:  # noqa: BLE001
        check("C6 #nochar# 移除标记且不补角色", False, f"{type(e).__name__}: {e}")

    # ── C7 #solo# 强制单人（上文两人）──────────────────────────────
    try:
        out = await fire(
            make_config(),
            natural_params("画她们俩 #solo#", text="画爱莉希雅和伊甸"),
        )
        c = out["args"]["content"]
        ok = "#solo#" not in c and "爱莉希雅" in c and "伊甸" not in c
        check("C7 #solo# 强制只补一个", ok, f"content = {c!r}")
    except Exception as e:  # noqa: BLE001
        check("C7 #solo# 强制只补一个", False, f"{type(e).__name__}: {e}")

    # ── C8 多人场景已写 1 人 → 只补缺失的那个 ──────────────────────
    try:
        out = await fire(
            make_config(),
            natural_params("爱莉希雅在左边挥手", text="画爱莉希雅和伊甸"),
        )
        c = out["args"]["content"]
        ok = c.count("爱莉希雅") == 1 and "伊甸" in c
        check("C8 多人场景只补缺失角色", ok, f"content = {c!r}")
    except Exception as e:  # noqa: BLE001
        check("C8 多人场景只补缺失角色", False, f"{type(e).__name__}: {e}")

    # ── C9 非目标 Action → 完全不动 ────────────────────────────────
    try:
        raw = "随便写点东西"
        out = await fire(
            make_config(),
            {
                "signature": "other_plugin:action:do_something",
                "action_name": "do_something",
                "args": {"content": raw},
                "message": FakeMessage("聊爱莉希雅"),
            },
        )
        check(
            "C9 非目标 Action 不接管",
            out["args"]["content"] == raw,
            f"content = {out['args']['content']!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C9 非目标 Action 不接管", False, f"{type(e).__name__}: {e}")

    # ── C10 自定义 targets.list 覆盖内置默认 ───────────────────────
    try:
        cfg = make_config(
            targets="image_generator_plugin-neo:action:edit_image | content_description | tags"
        )
        raw = "1girl, solo"
        out = await fire(cfg, tags_params(raw))
        ok1 = out["args"]["content_description"] == raw
        out2 = await fire(
            cfg,
            {
                "signature": "image_generator_plugin-neo:action:edit_image",
                "action_name": "edit_image",
                "args": {"content_description": raw},
                "message": FakeMessage("画爱莉希雅"),
            },
        )
        c2 = out2["args"]["content_description"]
        check(
            "C10 自定义 target 表覆盖默认",
            ok1 and TAG_ELYSIA in c2,
            f"draw_image 未动={ok1}; edit_image = {c2!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C10 自定义 target 表覆盖默认", False, f"{type(e).__name__}: {e}")

    # ── C11 总开关关闭 → 直通 ─────────────────────────────────────
    try:
        raw = "帮我画一张她战斗的样子"
        out = await fire(make_config(general_enabled=False), natural_params(raw))
        check("C11 general.enabled=false 直通", out["args"]["content"] == raw)
    except Exception as e:  # noqa: BLE001
        check("C11 general.enabled=false 直通", False, f"{type(e).__name__}: {e}")

    # ── C12 标记冲突按固定优先级 ───────────────────────────────────
    try:
        out = await fire(
            make_config(),
            natural_params("画她们俩 #multi# #solo#", text="画爱莉希雅和伊甸"),
        )
        c = out["args"]["content"]
        ok = "#multi#" not in c and "#solo#" not in c and "伊甸" not in c
        check("C12 冲突标记按优先级（#solo# 压 #multi#）", ok, f"content = {c!r}")
    except Exception as e:  # noqa: BLE001
        check(
            "C12 冲突标记按优先级（#solo# 压 #multi#）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C13 配置表行尾注释 ─────────────────────────────────────────
    try:
        cfg = make_config(
            roster=(
                r"爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia | elysia \(honkai impact 3rd\)  # 主力角色"
                "\n"
                r"伊甸 | 崩坏三 (Honkai Impact 3rd) | eden | eden \(honkai impact 3rd\)"
            )
        )
        out = await fire(cfg, natural_params("帮我画一张她战斗的样子"))
        c = out["args"]["content"]
        ok = "爱莉希雅" in c and "# 主力角色" not in c
        check("C13 角色表支持行尾注释", ok, f"content = {c!r}")
    except Exception as e:  # noqa: BLE001
        check("C13 角色表支持行尾注释", False, f"{type(e).__name__}: {e}")

    # ── C14 工具调用路径：agent 聊天器把 Action 当工具调用 ─────────
    try:
        params = {
            "signature": "nai_artist:tool:share_visual",
            "tool_name": "action-share_visual",
            "args": {"content": "帮我画一张她战斗的样子", "mode": "drawing"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        c = out["args"]["content"]
        check(
            "C14 工具调用路径（tool_name=action-share_visual）",
            "爱莉希雅" in c,
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C14 工具调用路径（tool_name=action-share_visual）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C15 工具路径兜底：signature 没带动作名时靠 tool_name ────────
    try:
        params = {
            "signature": "",
            "tool_name": "action-share_visual",
            "args": {"content": "帮我画一张她战斗的样子", "mode": "drawing"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        c = out["args"]["content"]
        check(
            "C15 signature 缺失时靠 tool_name 匹配", "爱莉希雅" in c, f"content = {c!r}"
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C15 signature 缺失时靠 tool_name 匹配", False, f"{type(e).__name__}: {e}"
        )

    # ── C16 agent 路径 + 发布方漏填动作名（neo 的 name 声明式组件）────
    # 框架补丁若只读旧的 action_name 属性，而组件用新版 name 声明
    # （image_generator_plugin-neo 的 draw_image 就是这种），事件里
    # signature 与 action_name 的动作名都会是空的。插件必须靠参数名兜底，
    # 否则 neo 在 agent 聊天器下会静默失效。
    try:
        params = {
            "signature": "image_generator_plugin-neo:action:",
            "action_name": "",
            "args": {"content_description": "1girl, solo, night city"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        c = out["args"]["content_description"]
        check(
            "C16 动作名为空时按参数名兜底（tags 模式）",
            TAG_ELYSIA in c,
            f"content_description = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C16 动作名为空时按参数名兜底（tags 模式）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C17 同一兜底路径下，补丁写全动作名时按正常匹配 ───────────────
    try:
        params = {
            "signature": "image_generator_plugin-neo:action:draw_image",
            "action_name": "draw_image",
            "args": {"content_description": "1girl, solo, night city"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        c = out["args"]["content_description"]
        check(
            "C17 补丁写全动作名时正常匹配",
            TAG_ELYSIA in c,
            f"content_description = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C17 补丁写全动作名时正常匹配", False, f"{type(e).__name__}: {e}")

    # ── C18 natural 模式下同样的兜底（nai_artist 走参数名 content）────
    try:
        params = {
            "signature": "nai_artist:action:",
            "action_name": "",
            "args": {"content": "帮我画一张她战斗的样子", "mode": "drawing"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        c = out["args"]["content"]
        check(
            "C18 动作名为空时按参数名兜底（natural 模式）",
            "爱莉希雅" in c,
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C18 动作名为空时按参数名兜底（natural 模式）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C19 兜底匹配：多个同参数名目标但注入语义相同 → 取靠前的 ─────
    # （1.0.2 起的行为：语义等价就不再"宁可不处理"，否则 nai_artist 新版
    #   两个动作共用 content 参数时，兜底路径会直接静默失效）
    try:
        cfg = make_config(
            targets=(
                "image_generator_plugin-neo:action:draw_image | content_description | tags"
                " ; image_generator_plugin-neo:action:edit_image | content_description | tags"
            )
        )
        raw = "1girl, solo"
        params = {
            "signature": "image_generator_plugin-neo:action:",
            "action_name": "",
            "args": {"content_description": raw},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(cfg, params)
        c = out["args"]["content_description"]
        check(
            "C19 兜底匹配语义等价时取靠前目标",
            "elysia" in c.lower() and c.startswith(raw),
            f"content_description = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C19 兜底匹配语义等价时取靠前目标", False, f"{type(e).__name__}: {e}")

    # ── C25 兜底匹配：注入语义冲突（tags vs natural）→ 宁可不处理 ──
    try:
        cfg = make_config(
            targets=(
                "nai_artist:action:share_photo | content | natural"
                " ; some_plugin:action:other | content | tags"
            )
        )
        raw = "帮我画一张她战斗的样子"
        params = {
            "signature": ":action:",
            "action_name": "",
            "args": {"content": raw},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(cfg, params)
        check("C25 兜底匹配语义冲突时不处理", out["args"]["content"] == raw)
    except Exception as e:  # noqa: BLE001
        check("C25 兜底匹配语义冲突时不处理", False, f"{type(e).__name__}: {e}")

    # ── C20 内置目标表同时覆盖 nai_artist 新旧动作名 ────────────────
    try:
        from ..handler import parse_targets

        builtin = [spec.action for spec in parse_targets("")]
        ok = (
            "share_photo" in builtin
            and "share_drawing" in builtin
            and "share_visual" in builtin
            and "draw_image" in builtin
        )
        check("C20 内置目标含 nai_artist 新旧动作名", ok, f"内置 = {builtin}")
    except Exception as e:  # noqa: BLE001
        check("C20 内置目标含 nai_artist 新旧动作名", False, f"{type(e).__name__}: {e}")

    # ── C21 nai_artist 1.3.6+ 的 share_photo 被接管 ────────────────
    try:
        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "action_description": "拍一张照片",
            "args": {"content": "帮我拍一张她战斗的样子"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        c = out["args"]["content"]
        check(
            "C21 share_photo 补角色（无 mode 参数也不受影响）",
            "爱莉希雅" in c and c.startswith("帮我拍一张她战斗的样子"),
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C21 share_photo 补角色（无 mode 参数也不受影响）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C22 nai_artist 1.3.6+ 的 share_drawing 被接管 ──────────────
    try:
        params = {
            "signature": "nai_artist:action:share_drawing",
            "action_name": "share_drawing",
            "action_description": "画一张画",
            "args": {"content": "帮我画一张她战斗的样子"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        c = out["args"]["content"]
        check(
            "C22 share_drawing 补角色",
            "爱莉希雅" in c and "崩坏三" in c,
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C22 share_drawing 补角色", False, f"{type(e).__name__}: {e}")

    # ── C23 目标命中但参数名不存在 → 告警放行，不塞未知参数 ────────
    try:
        params = {
            "signature": "nai_artist:action:share_drawing",
            "action_name": "share_drawing",
            "args": {"prompt": "帮我画一张她战斗的样子"},  # 故意不叫 content
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        ok = out["args"] == {"prompt": "帮我画一张她战斗的样子"}
        check("C23 参数名不匹配时放行且不新增参数", ok, f"args = {out['args']!r}")
    except Exception as e:  # noqa: BLE001
        check("C23 参数名不匹配时放行且不新增参数", False, f"{type(e).__name__}: {e}")

    # ── C24 未知动作名 → 放行（不改 args）──────────────────────────
    try:
        params = {
            "signature": "some_other_plugin:action:make_picture",
            "action_name": "make_picture",
            "args": {"content": "帮我画一张她战斗的样子"},
            "message": FakeMessage("我们来聊爱莉希雅吧"),
        }
        out = await fire(make_config(), params)
        check(
            "C24 不在目标表里的动作不改参数",
            out["args"]["content"] == "帮我画一张她战斗的样子",
        )
    except Exception as e:  # noqa: BLE001
        check("C24 不在目标表里的动作不改参数", False, f"{type(e).__name__}: {e}")

    # ── C26 stream_id 解析：params 优先、message 兜底、都没有则为空 ──
    try:
        from ..handler import resolve_stream_id

        class _Msg:
            def __init__(self, sid: str) -> None:
                self.stream_id = sid

        cases = [
            ({"stream_id": "s-params", "message": _Msg("s-msg")}, "s-params"),
            ({"message": _Msg("s-msg")}, "s-msg"),
            ({}, ""),
            ({"message": None}, ""),
            ({"stream_id": "", "message": _Msg("")}, ""),
        ]
        bad = [
            (p, want, resolve_stream_id(p))
            for p, want in cases
            if resolve_stream_id(p) != want
        ]
        check(
            "C26 stream_id 多级解析",
            not bad,
            "" if not bad else f"不符: {bad}",
        )
    except Exception as e:  # noqa: BLE001
        check("C26 stream_id 多级解析", False, f"{type(e).__name__}: {e}")

    # ── C27 LLM 结果缺字段时用角色表补全（游戏名/标签）────────────
    try:
        from ..character import CharacterEntry
        from ..handler import _enrich_from_roster

        roster = [
            CharacterEntry(
                name="爱莉希雅",
                game="崩坏三 (Honkai Impact 3rd)",
                aliases=["elysia", "爱莉"],
                tag=r"elysia \(honkai impact 3rd\)",
            )
        ]
        bare = CharacterEntry(name="爱莉希雅")
        filled = _enrich_from_roster(bare, roster)
        ok = (
            filled.game == "崩坏三 (Honkai Impact 3rd)"
            and filled.tag == r"elysia \(honkai impact 3rd\)"
        )
        check(
            "C27 从角色表补全游戏名与标签",
            ok,
            f"game={filled.game!r} tag={filled.tag!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C27 从角色表补全游戏名与标签", False, f"{type(e).__name__}: {e}")

    # ── C28 描述里的角色优先于上文里的旧角色（用假提取器验证优先级）──
    # 1.0.10 起「用户点名」压过 LLM，所以这条用例把「旧角色（爱莉希雅）」的提及
    # 放到 **bot 的复述**里：bot 说「我们来聊爱莉希雅吧」不算用户点名，此时没有
    # 用户点名信号，LLM 读描述得出的结论（花火）说了算 —— 用例原本要守的意图
    # （不要被上文里非用户点名的旧角色覆盖）不变。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS_ONE, use_llm=True)  # 角色表里只有爱莉希雅
        cfg.general.history_cache_ttl = 0.0

        async def fake_extract(**kwargs):
            # 模拟"LLM 读了画面描述与上文后给出的结论"：这次要画的是花火
            return CharacterExtraction(
                characters=[CharacterEntry(name="花火", game="崩坏：星穹铁道")],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            history = [history_message("我们来聊爱莉希雅吧", 2.0, is_bot=True)]
            with FakeHistory(history):
                params = {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": "台上站着一个人，手里拿着面具"},
                    "message": FakeMessage("画一张舞台照"),
                    "stream_id": "s-c28",
                }
                out = await fire(cfg, params)
            c = out["args"]["content"]
            ok = "花火" in c and "爱莉希雅" not in c
            check(
                "C28 描述结论优先于上文旧角色（bot 复述不算点名）",
                ok,
                f"content = {c!r}",
            )
        finally:
            handler_module.extract_characters_via_llm = original
    except Exception as e:  # noqa: BLE001
        check(
            "C28 描述结论优先于上文旧角色（bot 复述不算点名）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C29 LLM 说"角色已在提示词里"时不得回退到上文旧角色 ─────────
    # 实测原样复现：描述写着 Hanabi，LLM 也答"花火"，但"花火"已在描述里被过滤，
    # 旧代码于是回退到上文命中，把爱莉希雅补了进去。
    # 同 C28：旧角色的提及改由 **bot 复述** 提供，避免与「用户点名优先」冲突。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS_ONE, use_llm=True)
        cfg.general.history_cache_ttl = 0.0

        async def fake_extract(**kwargs):
            return CharacterExtraction(
                characters=[CharacterEntry(name="花火", game="崩坏：星穹铁道")],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            # 描述里用英文 Hanabi 指代花火。原文是「只拍花火本尊一人」，但
            # 1.0.12 起描述里出现「本尊」会直接走「自我指代 → 不补」分支，
            # 那样本用例就不再检验它要守的「LLM 答了就不回退上文旧角色」，
            # 所以这里去掉「本尊」二字（自我指代路径由 C44/C45 专门覆盖）。
            raw = "Hanabi (Honkai: Star Rail). 只拍花火一人"
            history = [history_message("我们来聊爱莉希雅吧", 2.0, is_bot=True)]
            with FakeHistory(history):
                params = {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("画一张舞台照"),
                    "stream_id": "s-c29",
                }
                out = await fire(cfg, params)
            c = out["args"]["content"]
            check(
                "C29 描述已含该角色时不回退上文旧角色",
                c == raw,
                f"content = {c!r}",
            )
        finally:
            handler_module.extract_characters_via_llm = original
    except Exception as e:  # noqa: BLE001
        check("C29 描述已含该角色时不回退上文旧角色", False, f"{type(e).__name__}: {e}")

    # ── C30 标签补全：描述里有角色名但缺标准标签 → 补标签 ──────────
    try:
        cfg = make_config(roster=RS_ONE)  # 第 4 列是 elysia \(honkai impact 3rd\)
        raw = "Elysia 站在灯下，半侧身对着镜头"
        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": raw},
            "message": FakeMessage("随便聊聊"),
            "stream_id": "s-test",
        }
        out = await fire(cfg, params)
        c = out["args"]["content"]
        check(
            "C30 描述含角色名但缺标准标签时补标签",
            c.startswith(raw) and r"elysia \(honkai impact 3rd\)" in c,
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C30 描述含角色名但缺标准标签时补标签", False, f"{type(e).__name__}: {e}")

    # ── C31 标签已在描述里 → 不重复补 ─────────────────────────────
    try:
        cfg = make_config(roster=RS_ONE)
        raw = r"Elysia 站在灯下, elysia \(honkai impact 3rd\)"
        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": raw},
            "message": FakeMessage("随便聊聊"),
            "stream_id": "s-test",
        }
        out = await fire(cfg, params)
        c = out["args"]["content"]
        check(
            "C31 标签已存在时不重复补",
            c == raw,
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C31 标签已存在时不重复补", False, f"{type(e).__name__}: {e}")

    # ── C32 描述已点名 A、上文提到 B+C（多人）→ 不得补 B/C ──────────
    # 实测：用户要爱莉，描述里已有"爱莉"，但上文写过花火与流萤，
    # multi_only 进多人路径后把花火+流萤补了进去。
    # 1.0.10 起这两条提及改由 **bot 复述** 提供（不是用户点名），断言语义不变：
    # 窗口里的旧角色不得覆盖描述点名。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS3, use_llm=True)
        cfg.general.history_cache_ttl = 0.0

        async def fake_extract(**kwargs):
            # LLM 读了描述：这次只有爱莉希雅
            return CharacterExtraction(
                characters=[
                    CharacterEntry(name="爱莉希雅", game="崩坏三 (Honkai Impact 3rd)")
                ],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            raw = "爱莉希雅穿着礼服站在灯下"
            history = [history_message("聊聊花火和流萤吧", 2.0, is_bot=True)]
            with FakeHistory(history):
                params = {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("画一张舞台照"),
                    "stream_id": "s-c32",
                }
                out = await fire(cfg, params)
            c = out["args"]["content"]
            ok = "花火" not in c and "流萤" not in c and TAG_ELYSIA in c
            check("C32 多人上文不得覆盖已点名的角色", ok, f"content = {c!r}")
        finally:
            handler_module.extract_characters_via_llm = original
    except Exception as e:  # noqa: BLE001
        check("C32 多人上文不得覆盖已点名的角色", False, f"{type(e).__name__}: {e}")

    # ── C33 主角粘性：描述没点名，窗口里 A(较早)+B(最近) → 补 B 且不补 A ──
    try:
        cfg = make_config()  # 角色表 RS：爱莉希雅 / 伊甸
        cfg.general.history_cache_ttl = 0.0
        history = [
            history_message("我们来聊爱莉希雅吧", 30.0),  # A：30 分钟前
            history_message("画一张伊甸的图", 1.0),  # B：1 分钟前（最近）
        ]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_drawing",
                    "action_name": "share_drawing",
                    "args": {"content": "画一张战斗场景"},
                    "message": FakeMessage("画一张战斗场景"),
                    "stream_id": "s-c33",
                },
            )
        c = out["args"]["content"]
        ok = (
            "伊甸" in c
            and "爱莉希雅" not in c
            and "判定依据=" in logs.debugs()
            and "伊甸" in logs.debugs()
        )
        check(
            "C33 粘性：取最近一次点名（B），不补更早的 A",
            ok,
            f"content = {c!r}\n        日志 = {logs.debugs()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C33 粘性：取最近一次点名（B），不补更早的 A",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C34 最近一条消息点了两名 → 多人补齐两人 ────────────────────
    try:
        cfg = make_config()
        cfg.general.history_cache_ttl = 0.0
        with LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_drawing",
                    "action_name": "share_drawing",
                    "args": {"content": "画一张战斗场景"},
                    "message": FakeMessage("把爱莉希雅和伊甸画在同一个画面里"),
                    "stream_id": "s-c34",
                },
            )
        c = out["args"]["content"]
        ok = "爱莉希雅" in c and "伊甸" in c
        check(
            "C34 同一条消息点两名 → 补两人",
            ok,
            f"content = {c!r}\n        日志 = {logs.debugs()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C34 同一条消息点两名 → 补两人", False, f"{type(e).__name__}: {e}")

    # ── C35 追加语义（还有/一起）→ 本条 + 上一条点名的角色 ──────────
    try:
        cfg = make_config()
        cfg.general.history_cache_ttl = 0.0
        history = [history_message("画爱莉希雅", 1.0)]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_drawing",
                    "action_name": "share_drawing",
                    "args": {"content": "画一张战斗场景"},
                    "message": FakeMessage("还有伊甸，一起画"),
                    "stream_id": "s-c35",
                },
            )
        c = out["args"]["content"]
        ok = "伊甸" in c and "爱莉希雅" in c
        check(
            "C35 追加语义 → 本条 + 上一条点名都补",
            ok,
            f"content = {c!r}\n        日志 = {logs.debugs()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C35 追加语义 → 本条 + 上一条点名都补", False, f"{type(e).__name__}: {e}")

    # ── C36 改口语义（算了/还是）→ 只补最新那名 ────────────────────
    try:
        cfg = make_config()
        cfg.general.history_cache_ttl = 0.0
        history = [history_message("画爱莉希雅", 1.0)]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_drawing",
                    "action_name": "share_drawing",
                    "args": {"content": "画一张战斗场景"},
                    "message": FakeMessage("算了，还是画伊甸吧"),
                    "stream_id": "s-c36",
                },
            )
        c = out["args"]["content"]
        ok = "伊甸" in c and "爱莉希雅" not in c
        check(
            "C36 改口语义 → 只补最新那名（换人）",
            ok,
            f"content = {c!r}\n        日志 = {logs.debugs()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C36 改口语义 → 只补最新那名（换人）", False, f"{type(e).__name__}: {e}")

    # ── C37 超出 multi.stickiness_hours → 视为无信号，不补 ──────────
    try:
        cfg = make_config()
        cfg.general.history_cache_ttl = 0.0
        cfg.multi.stickiness_hours = 6.0
        raw = "画一张战斗场景"
        history = [
            history_message("画爱莉希雅", 8 * 60.0),  # 8 小时前
            history_message("画伊甸", 7 * 60.0),  # 7 小时前（也超 6h）
        ]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_drawing",
                    "action_name": "share_drawing",
                    "args": {"content": raw},
                    "message": FakeMessage(raw),
                    "stream_id": "s-c37",
                },
            )
        c = out["args"]["content"]
        ok = c == raw and "判定依据=无(不补)" in logs.debugs()
        check(
            "C37 超出粘性时效 → 不补",
            ok,
            f"content = {c!r}\n        日志 = {logs.debugs()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C37 超出粘性时效 → 不补", False, f"{type(e).__name__}: {e}")

    # ── C38 一致性检测 warn：冲突只告警、内容不改 ──────────────────
    # 描述里写着爱莉希雅，用户这次点名的却是花火 —— 典型的「画面里是另一个
    # 角色」冲突。warn 模式只打 WARNING，不动内容。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "warn"
        cfg.general.tag_completion = False  # 隔离标签补全，只看一致性检测
        raw = "爱莉希雅站在月光下"
        with LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("画花火一个人"),
                    "stream_id": "s-c38",
                },
            )
        c = out["args"]["content"]
        warns = logs.warnings()
        ok = (
            c == raw
            and "一致性检查=冲突" in warns
            and "爱莉希雅" in warns
            and "花火" in warns
        )
        check(
            "C38 consistency_check=warn：告警但不改内容",
            ok,
            f"content = {c!r}\n        WARNING = {warns!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C38 consistency_check=warn：告警但不改内容",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C39 一致性检测 fix：把画面里的错误角色替换成正确角色 ────────
    # 同一场景换成 fix：画面里的「爱莉希雅」被替换成用户点名的「花火」，
    # 连角色表里的标准标签一起换（elysia … → hanabi …）。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        raw = "爱莉希雅站在月光下"
        with LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("画花火一个人"),
                    "stream_id": "s-c39",
                },
            )
        c = out["args"]["content"]
        warns = logs.warnings()
        ok = (
            c.startswith("花火站在月光下")
            and "爱莉希雅" not in c
            and "elysia" not in c.lower()
            and TAG_HANABI in c
            and "一致性检查=冲突已修正" in warns
        )
        check(
            "C39 consistency_check=fix：错误角色被替换",
            ok,
            f"content = {c!r}\n        WARNING = {warns!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C39 consistency_check=fix：错误角色被替换",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C40 用户点名压过 LLM 的猜测（本次要修的核心问题）────────────
    # 实测场景：用户 21:52 点名「流萤」，01:03 只说「想看看你」，
    # LLM 却结合描述猜成了爱莉希雅。1.0.10 起用户点名优先于 LLM 仲裁，
    # 而且粘性时效内的点名不会被"描述优先"翻掉。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS3, use_llm=True)
        cfg.general.history_cache_ttl = 0.0

        async def fake_extract(**kwargs):
            # 模拟 LLM 被描述带偏：它猜的是爱莉希雅
            return CharacterExtraction(
                characters=[
                    CharacterEntry(name="爱莉希雅", game="崩坏三 (Honkai Impact 3rd)")
                ],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            history = [history_message("画一张流萤的图", 191.0)]  # 3 小时 11 分钟前
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_drawing",
                        "action_name": "share_drawing",
                        "args": {"content": "画一张战斗场景"},
                        "message": FakeMessage("想看看你"),
                        "stream_id": "s-c40",
                    },
                )
            c = out["args"]["content"]
            ok = "流萤" in c and "爱莉希雅" not in c
            check(
                "C40 用户点名压过 LLM 猜测（粘性时效内）",
                ok,
                f"content = {c!r}\n        日志 = {logs.debugs()!r}",
            )
        finally:
            handler_module.extract_characters_via_llm = original
    except Exception as e:  # noqa: BLE001
        check(
            "C40 用户点名压过 LLM 猜测（粘性时效内）", False, f"{type(e).__name__}: {e}"
        )

    # ── C41 框架写 sender_id="bot" 的复述消息不得算作用户点名 ────────
    # 实测事故（11:50:04）：bot 自己的叙述「…爱莉、昔涟、丽塔轮着来，末了还是回到…」
    # 含角色名 + 改口词，被当成"用户点名爱莉希雅"，于是用户要花火却补了爱莉。
    # 注意：这里模拟**框架真实形状**（只有 sender_id/person_id，没有 is_bot 字段），
    # 与 history_message(is_bot=True) 那种显式标注不同。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        now = time.time()
        bot_line = (
            "绕了一上午的壳：爱莉、昔涟、丽塔轮着来，末了还是回到站在这层底下的那个人。"
        )
        history = [
            {
                "time": now - 30 * 60,
                "stream_id": "s-stub",
                "person_id": "76920378836bdeadbeef",
                "sender_id": "76920378836bdeadbeef",
                "sender_name": "用户",
                "processed_plain_text": "画花火吧",
                "content": "画花火吧",
                "platform": "stub",
            },
            {
                "time": now - 60,
                "stream_id": "s-stub",
                "person_id": "bot",
                "sender_id": "bot",
                "sender_name": "bot",
                "processed_plain_text": bot_line,
                "content": bot_line,
                "platform": "stub",
            },
        ]
        with FakeHistory(history):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": "白发少女站在镜头前"},
                    "message": FakeMessage("嗯～"),
                    "stream_id": "s-c41",
                },
            )
        c = out["args"]["content"]
        check(
            "C41 框架 bot 标记的复述不算用户点名",
            "爱莉希雅" not in c and ("花火" in c or "hanabi" in c.lower()),
            f"content = {c!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C41 框架 bot 标记的复述不算用户点名", False, f"{type(e).__name__}: {e}")

    # ── C42 选择疑问句里的「还是」不算改口（1.0.12 修的事故核心）──────
    # 实测事故：用户消息「是爱莉还是你？」里的「还是」被当成改口词，于是「用户点名
    # 爱莉希雅」压过了 LLM 的结论，把 LLM 正确认出的「花火」顶掉。
    # 1.0.12 起「还是」必须处于改口语境才算改口；选择疑问句不算，判定退回
    # 「换人还是追加不确定 → 交 LLM 仲裁」，LLM 说花火就是花火。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS3, use_llm=True)
        cfg.general.history_cache_ttl = 0.0

        async def fake_extract(**kwargs):
            # 模拟"LLM 读了画面描述与上文后给出的结论"：这次要画的是花火
            return CharacterExtraction(
                characters=[
                    CharacterEntry(
                        name="花火", game="崩坏：星穹铁道 (Honkai: Star Rail)"
                    )
                ],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            # 1 分钟前点过「爱莉希雅」：与触发消息间隔 < multi_window_minutes，
            # 所以判定是 uncertain，而不是"用户点名"直接拍板
            history = [history_message("画爱莉希雅", 1.0)]
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_drawing",
                        "action_name": "share_drawing",
                        "args": {"content": "画一张战斗场景"},
                        "message": FakeMessage("是爱莉还是你？"),
                        "stream_id": "s-c42",
                    },
                )
            c = out["args"]["content"]
            debugs = logs.debugs()
            ok = "花火" in c and "爱莉希雅" not in c and "LLM 仲裁" in debugs
            check(
                "C42 疑问句「是 A 还是你？」不算改口（LLM 结论生效）",
                ok,
                f"content = {c!r}\n        日志 = {debugs!r}",
            )
        finally:
            handler_module.extract_characters_via_llm = original
    except Exception as e:  # noqa: BLE001
        check(
            "C42 疑问句「是 A 还是你？」不算改口（LLM 结论生效）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C43 引用块/媒体块里的文字不参与点名（1.0.12 修的事故原文）────
    # 事故原文（用户消息）：
    #   [回复<的饮水思源(2224****93)>：那你告诉我，是爱莉还是你？][图片(xxx)]
    # 引用块里既有「爱莉」又有「还是」，于是被判成"用户改口点名爱莉希雅"。
    # 1.0.12 起先剥掉 [回复…] / [图片(…)] 这类中括号块（嵌套按最外层整块剥），
    # 剥完没有任何点名信号 → 不补任何角色、内容不变。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        raw = "白发少女站在镜头前"
        trigger = (
            "[回复<的饮水思源(2224****93)>：那你告诉我，是爱莉还是你？][图片(xxx)]"
        )
        with FakeHistory([]), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage(trigger),
                    "stream_id": "s-c43",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = c == raw and "爱莉希雅" not in c and "判定依据=无(不补)" in debugs
        check(
            "C43 引用/图片块里的文字不参与点名",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C43 引用/图片块里的文字不参与点名", False, f"{type(e).__name__}: {e}")

    # ── C44 自我指代在上一条用户消息里 → 不补（1.0.12 修的事故主因）──
    # 事故真实时序（12:14~12:15 同一会话）：
    #   12:14:39 用户「那你再来图，要你自己～你出图了我就道歉～」 ← 真正的意图：要本尊
    #   12:14:59 用户「嗯哼？」                                    ← 触发这次生图
    #   画面描述「室内明亮光线下的半身照。白发少女站在镜头前，粉色眼睛…」（无角色名）
    #   12:15:06 结果：补了爱莉希雅（更早用户点名过「爱莉」→ 粘性命中）
    # 只看触发消息（「嗯哼？」）与描述都看不到自我指代，所以 1.0.12 把检查范围
    # 扩大到「画面描述 + 触发消息 + 触发之前最近 N 条用户消息」
    # （N = min(multi.recent_messages, 4)）。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        raw = "室内明亮光线下的半身照。白发少女站在镜头前，粉色眼睛"
        history = [
            history_message("画爱莉希雅", 60.0),  # 1 小时前：更早的角色点名（粘性来源）
            history_message("那你再来图，要你自己～你出图了我就道歉～", 0.3),
        ]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("嗯哼？"),
                    "stream_id": "s-c44",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            c == raw
            and "爱莉希雅" not in c
            and "自我指代" in debugs
            and "判定依据=无(不补)" in debugs
        )
        check(
            "C44 自我指代在上一条用户消息里 → 不补（不沿用旧点名）",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C44 自我指代在上一条用户消息里 → 不补（不沿用旧点名）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C45 自我指代写在触发消息里 → 同样不补 ──────────────────────
    # 与 C44 同一策略的另一条入口：自我指代就在触发消息本身（描述里无角色名，
    # 上文一小时前点名过爱莉希雅）。断言内容不变，且日志里有自我指代诊断。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        raw = "白发少女站在镜头前"
        history = [history_message("画爱莉希雅", 60.0)]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("那你再来图，要你自己～"),
                    "stream_id": "s-c45",
                },
            )
        c = out["args"]["content"]
        ok = c == raw and "爱莉希雅" not in c and "自我指代" in logs.debugs()
        check(
            "C45 自我指代在触发消息里 → 不补",
            ok,
            f"content = {c!r}\n        日志 = {logs.debugs()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("C45 自我指代在触发消息里 → 不补", False, f"{type(e).__name__}: {e}")

    # ── C46 自我指代 + general.true_self 指向角色表内角色 → 正常注入（1.0.13）──
    # 1.0.12 里自我指代一律不补；1.0.13 起先把「本尊」定位到角色表里的角色：
    #   general.true_self = "花火"（在 RS3 里）→ 必须补花火，而不是按粘性补更早
    #   点名过的爱莉希雅。日志要能一眼看出「本尊身份」与「角色表命中」。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.true_self = "花火"
        raw = "白发少女站在镜头前，粉色眼睛"
        history = [history_message("画爱莉希雅", 60.0)]  # 更早的角色点名（粘性来源）
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("那你再来图，要你自己～"),
                    "stream_id": "s-c46",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "花火" in c
            and "爱莉希雅" not in c
            and c.startswith(raw)
            and "本尊身份" in debugs
            and "角色表命中" in debugs
            and "(来源=配置)" in debugs
        )
        check(
            "C46 自我指代 + true_self=花火 → 补花火（本尊身份）",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C46 自我指代 + true_self=花火 → 补花火（本尊身份）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C47 true_self 留空 → 回落框架人格配置的 nickname（1.0.13）────────
    # stub 方式：把 handler 模块里读人格配置的函数 ``_read_personality_identity``
    # 换成返回 "伊甸"（伊甸在 RS 里），等价于框架人格配置里
    # core.toml [personality] nickname = "伊甸"。
    # 注意这里刻意返回**裸字符串**：真实实现返回 list[str]，插件对两种形状都做了
    # 归一化（handler._normalize_identity_names），stub 成字符串也必须能用。
    try:
        from .. import handler as handler_module

        cfg = make_config(roster=RS, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.true_self = ""  # 留空 → 读人格配置
        raw = "白发少女站在镜头前，粉色眼睛"
        history = [history_message("画爱莉希雅", 60.0)]
        original = handler_module._read_personality_identity
        handler_module._read_personality_identity = lambda: "伊甸"
        try:
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_photo",
                        "action_name": "share_photo",
                        "args": {"content": raw},
                        "message": FakeMessage("那你再来图，要你自己～"),
                        "stream_id": "s-c47",
                    },
                )
        finally:
            handler_module._read_personality_identity = original
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "伊甸" in c
            and "爱莉希雅" not in c
            and "本尊身份" in debugs
            and "(来源=人格配置)" in debugs
            and "角色表命中" in debugs
        )
        check(
            "C47 true_self 留空 → 回落人格配置 nickname",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C47 true_self 留空 → 回落人格配置 nickname",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C48 人格 nickname 不在角色表里 → 不补（1.0.13）──────────────────
    # 同一个 stub 位置，这次让身份名指向角色表里没有的名字（真实实现返回
    # list[str]，故这里用列表形状）：必须**不补**（不得退化成"按粘性补爱莉希雅"），
    # 且日志写明不在角色表中、提示把该角色加入角色表。
    try:
        from .. import handler as handler_module

        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.true_self = ""
        raw = "白发少女站在镜头前，粉色眼睛"
        history = [history_message("画爱莉希雅", 60.0)]
        original = handler_module._read_personality_identity
        handler_module._read_personality_identity = lambda: ["不存在的人"]
        try:
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_photo",
                        "action_name": "share_photo",
                        "args": {"content": raw},
                        "message": FakeMessage("那你再来图，要你自己～"),
                        "stream_id": "s-c48",
                    },
                )
        finally:
            handler_module._read_personality_identity = original
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            c == raw
            and "爱莉希雅" not in c
            and "本尊身份" in debugs
            and "不在角色表中" in debugs
            and "判定依据=无(不补)" in debugs
        )
        check(
            "C48 人格 nickname 不在角色表 → 不补（提示加入角色表）",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C48 人格 nickname 不在角色表 → 不补（提示加入角色表）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C49 主题跟踪：旧主题 →「本尊」切换 → 花火（1.0.14）───────────────
    # 窗口里先「画爱莉希雅」（3 小时前）、再「我现在要看本尊」（10 分钟前），触发消息
    # 只是一句「嗯哼？」。1.0.14 起由主题跟踪器按时间正序演进主题：
    #     爱莉希雅(3h前) → [本尊:人格配置] → 花火 ← 当前主题（置信度 0.76）
    # 本尊身份用 monkeypatch 替换 handler._read_personality_identity（等价于框架人格
    # 配置里 nickname = "花火"），所以日志里应能看到「本尊身份」「角色表命中」与
    # 「(来源=人格配置)」。断言注入的是**花火**（0.95 × exp(-10/45) ≈ 0.76 ≥ 0.5），
    # 而不是按粘性补更早的爱莉希雅。
    try:
        from .. import handler as handler_module

        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.true_self = ""  # 留空 → 读人格配置（下面 stub 成「花火」）
        raw = "室内明亮光线下的半身照，白发少女站在镜头前"
        history = [
            history_message("画爱莉希雅", 180.0),  # 3 小时前：旧主题
            history_message("我现在要看本尊", 10.0),  # 10 分钟前：本尊切换
        ]
        original = handler_module._read_personality_identity
        handler_module._read_personality_identity = lambda: "花火"
        try:
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_photo",
                        "action_name": "share_photo",
                        "args": {"content": raw},
                        "message": FakeMessage("嗯哼？"),
                        "stream_id": "s-c49",
                    },
                )
        finally:
            handler_module._read_personality_identity = original
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "花火" in c
            and "爱莉希雅" not in c
            and c.startswith(raw)
            and "主题跟踪" in debugs
            and "本尊" in debugs
            and "判定依据=主题跟踪" in debugs
        )
        check(
            "C49 主题跟踪：本尊切换把主题从爱莉希雅换成花火",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C49 主题跟踪：本尊切换把主题从爱莉希雅换成花火",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C50 主题跟踪：一条消息点两名 → 多人同框（1.0.14）─────────────────
    # 触发消息「我要爱莉希雅和流萤一起」既含 ≥2 个角色表内角色、也含追加词，
    # 主题 = {爱莉希雅, 流萤}（多人）。natural 模式下两人各追加一段说明，
    # 所以「画面角色」出现 2 次即证明走的是多人路径（而不是只补了第一个）。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        raw = "舞台上的双人合影"
        with LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_drawing",
                    "action_name": "share_drawing",
                    "args": {"content": raw},
                    "message": FakeMessage("我要爱莉希雅和流萤一起"),
                    "stream_id": "s-c50",
                },
            )
        c = out["args"]["content"]
        ok = (
            "爱莉希雅" in c
            and "流萤" in c
            and c.count("画面角色") == 2
            and c.startswith(raw)
            and "主题跟踪" in logs.debugs()
        )
        check(
            "C50 主题跟踪：一条消息点两名 → 多人补齐两人",
            ok,
            f"content = {c!r}\n        日志 = {logs.debugs()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C50 主题跟踪：一条消息点两名 → 多人补齐两人",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C51 主题跟踪：代词回指不改主题（1.0.14）──────────────────────────
    # 「画花火吧」（10 分钟前）→「她今天穿什么来着？」（9 分钟前）→ 触发「嗯哼？」。
    # 「她」是人称代词：主题**保持不变**（花火），只是按「粘性」重新计时
    # （0.7 × exp(-9/45) ≈ 0.57 ≥ 0.5）。既不能被代词清空，也不能猜成别人。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        raw = "室内明亮光线下的半身照"
        history = [
            history_message("画花火吧", 10.0),
            history_message("她今天穿什么来着？", 9.0),
        ]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("嗯哼？"),
                    "stream_id": "s-c51",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "花火" in c
            and "爱莉希雅" not in c
            and "流萤" not in c
            and "主题跟踪" in debugs
            and "代词" in debugs
        )
        check(
            "C51 主题跟踪：代词回指保持主题（补花火）",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C51 主题跟踪：代词回指保持主题（补花火）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C52 主题跟踪：置信度不足不补（1.0.14）────────────────────────────
    # 窗口内唯一的主题信号是 3 小时前那句「画爱莉希雅」，其后没有任何新信号：
    # 置信度 = 1.0 × exp(-180 / 45) ≈ 0.02 < 0.5 → **不补**（内容一字不改），
    # 且不得退化成「按粘性补爱莉希雅」。日志要写清是置信度不足挡下来的。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        raw = "室内明亮光线下的半身照"
        history = [history_message("画爱莉希雅", 180.0)]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("嗯哼？"),
                    "stream_id": "s-c52",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            c == raw
            and "爱莉希雅" not in c
            and "主题跟踪" in debugs
            and "置信度" in debugs
            and "判定依据=无(不补)" in debugs
        )
        check(
            "C52 主题跟踪：置信度低于阈值 → 不补（内容不变）",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C52 主题跟踪：置信度低于阈值 → 不补（内容不变）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C53 monitor.enabled=false → 完全退回 1.0.13（1.0.14）─────────────
    # 同一个 C51 场景，把主题跟踪关掉后必须回到旧链路：用户点名(粘性) 补出花火，
    # 而且日志里**不该**再出现主题跟踪的诊断行 —— 证明是真的退回了旧行为，
    # 而不是"新逻辑碰巧给出同样结果"。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        cfg.monitor.enabled = False
        raw = "室内明亮光线下的半身照"
        history = [
            history_message("画花火吧", 10.0),
            history_message("她今天穿什么来着？", 9.0),
        ]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("嗯哼？"),
                    "stream_id": "s-c53",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "花火" in c
            and "爱莉希雅" not in c
            and "主题跟踪" not in debugs
            and "判定依据=用户点名" in debugs
        )
        check(
            "C53 monitor.enabled=false → 退回 1.0.13（用户点名/粘性）",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C53 monitor.enabled=false → 退回 1.0.13（用户点名/粘性）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C54 拆单：一条消息里既有「本尊」又有角色名 → 交 LLM 按画面判定（1.0.16）──
    # 线上事故：用户「本尊看看，然后穿着爱莉来看看～」是**一单两件**（调用方把它拆成
    # 两张图：先看本尊、再看壳），当前这张是**本尊**那张。
    # 1.0.15 的状态机里「角色名」命中就整条 continue，同一条消息里的自我指代被吞掉：
    #   主题跟踪(15条): … → 爱莉希雅(刚刚) ← 当前主题（置信度 0.99）
    #   判定依据=主题跟踪:用户点名→爱莉希雅（置信度 0.99）
    # 于是 LLM 按画面正确认出的「花火」（本尊）被主题压掉，补成了爱莉希雅。
    # 1.0.16 起同一条消息里「自我指代 + 角色名」= **拆单**：主题「不可单独确定」，
    # 强制问一次 LLM 并**采纳它按当前画面描述给出的角色表内结论**（唯一允许 LLM
    # 压过主题的情形）。
    # 这里刻意把 description_first 关掉、llm_mode 设为 fallback，且上文史里本来就有
    # 爱莉希雅的命中 —— 1.0.16 之前的 need_llm 条件**全部为假**，只有「拆单强制走
    # LLM」才会真的问一次，用例因此守得住「强制」这条。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS3, use_llm=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.description_first = False  # 旧条件全不成立 → 只能靠拆单强制问
        cfg.multi.llm_mode = "fallback"
        raw = "清晨室内明亮柔和的光线。白发少女刚起床，站在镜头前，粉色眼睛看向镜头"
        calls = {"n": 0}

        async def fake_extract(**kwargs):
            calls["n"] += 1
            # 模拟「LLM 读了画面描述与上文后的结论」：这张要画的是本尊（花火）
            return CharacterExtraction(
                characters=[
                    CharacterEntry(
                        name="花火", game="崩坏：星穹铁道 (Honkai: Star Rail)"
                    )
                ],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            history = [history_message("本尊看看，然后穿着爱莉来看看～", 1.0)]
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_photo",
                        "action_name": "share_photo",
                        "args": {"content": raw},
                        "message": FakeMessage("嗯～"),
                        "stream_id": "s-c54",
                    },
                )
        finally:
            handler_module.extract_characters_via_llm = original
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "花火" in c
            and "爱莉希雅" not in c
            and c.startswith(raw)
            and "拆单" in debugs
            and "花火" in debugs
            and calls["n"] == 1  # 拆单强制走 LLM（旧条件全为假，不强制就不会问）
        )
        check(
            "C54 拆单：本尊+爱莉 → LLM 按画面判定补花火",
            ok,
            f"content = {c!r}\n        LLM 调用次数 = {calls['n']}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C54 拆单：本尊+爱莉 → LLM 按画面判定补花火",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C55 拆单但 LLM 没有结论 → 不补（内容一字不改）（1.0.16）────────────
    # 同一个拆单场景，LLM 这次什么都没给（返回空 / 失败 / 超时都走这条）：主题是
    # 「不可单独确定」，插件**不补**任何角色 —— 既不按 LLM 猜，也绝不回退到上文里
    # 那个爱莉希雅；日志写明「拆单且无法按画面判定」。
    try:
        from .. import handler as handler_module
        from ..character import CharacterExtraction

        cfg = make_config(roster=RS3, use_llm=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.description_first = False  # 同上：只有拆单会强制问 LLM
        cfg.multi.llm_mode = "fallback"
        raw = "清晨室内明亮柔和的光线。白发少女刚起床，站在镜头前，粉色眼睛看向镜头"

        async def fake_extract(**kwargs):
            return CharacterExtraction(characters=[], multi=False)

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            history = [history_message("本尊看看，然后穿着爱莉来看看～", 1.0)]
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_photo",
                        "action_name": "share_photo",
                        "args": {"content": raw},
                        "message": FakeMessage("嗯～"),
                        "stream_id": "s-c55",
                    },
                )
        finally:
            handler_module.extract_characters_via_llm = original
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            c == raw
            and "爱莉希雅" not in c
            and "拆单" in debugs
            and "拆单且无法按画面判定" in debugs
        )
        check(
            "C55 拆单但 LLM 无结论 → 不补（内容不变，不回退旧点名）",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C55 拆单但 LLM 无结论 → 不补（内容不变，不回退旧点名）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C56 参数说明注入：原说明保留 + 追加「标签在最前」（1.0.17）────────
    # 真实的生图插件给 content 写的说明是"写画面描述，写给下游翻译模型"，
    # 主模型照着写，标准标签就没了。这里验的是注入本身：改的是假动作的注解，
    # 说明由**框架自己的 schema 生成器**重新生成后必须同时满足：
    #   · 原文还在、且仍在最前面（只追加，不覆盖）；
    #   · 追加句进来了；
    #   · type 等说明以外的字段一字未变（别把第三方动作的 schema 改坏）；
    #   · ActionManager 的 schema 缓存被清（否则 API 侧还会拿到旧 schema）。
    # 顺带钉住「注入用的签名/参数名」与 handler 的内置目标表一致。
    try:
        from .. import tag_hint
        from ..handler import _BUILTIN_TARGETS, TargetSpec

        tag_hint.reset_state()
        spec = TargetSpec(
            plugin="nai_artist", action="share_photo", prompt_arg="content"
        )
        builtin_shape = {
            tag_hint.target_signature(item): item.prompt_arg
            for item in _BUILTIN_TARGETS
        }
        cleared: list[str] = []
        before = _FakePhotoAction.to_schema()["function"]["parameters"]["properties"][
            "content"
        ]
        result = tag_hint.inject_tag_first_hint(
            [spec],
            config=make_config(),
            action_getter=action_getter_for(
                {"nai_artist:action:share_photo": _FakePhotoAction}
            ),
            cache_clearer=cleared.append,
        )
        after = _FakePhotoAction.to_schema()["function"]["parameters"]["properties"][
            "content"
        ]
        desc = after.get("description", "")
        ok = (
            result.ok
            and result.injected == ["nai_artist:action:share_photo.content"]
            and builtin_shape
            == {
                "nai_artist:action:share_photo": "content",
                "nai_artist:action:share_drawing": "content",
                "nai_artist:action:share_visual": "content",
                "image_generator_plugin-neo:action:draw_image": "content_description",
            }
            and FAKE_PHOTO_DESC in desc  # 原说明一字未丢
            and desc.startswith(FAKE_PHOTO_DESC)  # 且仍在最前面
            and tag_hint.HINT_MARKER in desc  # 追加句进来了
            and desc.count("附加要求") == 1
            and after.get("type") == before.get("type") == "string"  # 类型没被改坏
            and cleared == ["nai_artist:action:share_photo"]  # schema 缓存被清
        )
        check(
            "C56 注入参数说明：原说明保留 + 追加「标准标签在最前」",
            ok,
            f"description = {desc!r}\n"
            f"        类型 before/after = {before.get('type')!r}/{after.get('type')!r}\n"
            f"        清理缓存 = {cleared!r}\n"
            f"        结果 = {result.log_line()}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C56 注入参数说明：原说明保留 + 追加「标准标签在最前」",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C57 tag_first_hint=False → 一个字节都不改（1.0.17）───────────────
    # 关掉开关时**完全不碰**第三方动作：说明仍等于原始字符串，缓存也不清。
    # 用另一个假动作（neo 的 draw_image / content_description），
    # 免得受 C56 已经改过的那个类影响。
    try:
        from .. import tag_hint
        from ..handler import TargetSpec

        tag_hint.reset_state()
        spec = TargetSpec(
            plugin="image_generator_plugin-neo",
            action="draw_image",
            prompt_arg="content_description",
        )
        cfg = make_config()
        cfg.general.tag_first_hint = False
        getter = action_getter_for(
            {"image_generator_plugin-neo:action:draw_image": _FakeTagsAction}
        )
        cleared: list[str] = []
        before = param_description(_FakeTagsAction, "content_description")
        result = tag_hint.inject_tag_first_hint(
            [spec], config=cfg, action_getter=getter, cache_clearer=cleared.append
        )
        after = param_description(_FakeTagsAction, "content_description")
        ok = (
            result.enabled is False
            and result.ok is False
            and result.injected == []
            and before == FAKE_TAGS_DESC
            and after == FAKE_TAGS_DESC  # 等于原始字符串，不是"看起来没变"
            and tag_hint.HINT_MARKER not in after
            and cleared == []
        )
        check(
            "C57 tag_first_hint=false 时参数说明完全未被修改",
            ok,
            f"before = {before!r}\n        after  = {after!r}\n"
            f"        结果 = {result.log_line()}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C57 tag_first_hint=false 时参数说明完全未被修改",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C58 找不到目标动作（空注册表）→ 不抛异常、返回「未注入」（1.0.17）──
    # 插件通常比生图插件先加载完，那一刻注册表里什么都没有。这条路径必须
    # 静默降级：既不抛异常，也不谎报注入成功，只把签名记进 unavailable。
    try:
        from .. import tag_hint
        from ..handler import _BUILTIN_TARGETS

        tag_hint.reset_state()
        cleared: list[str] = []
        result = tag_hint.inject_tag_first_hint(
            _BUILTIN_TARGETS,
            config=make_config(),
            action_getter=action_getter_for(),  # 空注册表
            cache_clearer=cleared.append,
        )
        ok = (
            result.ok is False
            and result.injected == []
            and result.enabled is True
            and result.unavailable
            == [
                "nai_artist:action:share_photo",
                "nai_artist:action:share_drawing",
                "nai_artist:action:share_visual",
                "image_generator_plugin-neo:action:draw_image",
            ]
            and cleared == []
        )
        check(
            "C58 目标动作未注册时静默降级（不抛异常、返回未注入）",
            ok,
            f"结果 = {result.log_line()}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C58 目标动作未注册时静默降级（不抛异常、返回未注入）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C59 注入幂等 + 卸载可还原（1.0.17）─────────────────────────────
    # 加载时试一次、事件里又试一次，不能出现两句「附加要求」；
    # 插件卸载时把参数说明还原回原文（内存改动不该在卸载后继续生效）。
    # 用的假动作与 C57 同一个，而 C57 开关是关的、没改过它，所以起点是原文。
    try:
        from .. import tag_hint
        from ..handler import TargetSpec

        tag_hint.reset_state()
        spec = TargetSpec(
            plugin="image_generator_plugin-neo",
            action="draw_image",
            prompt_arg="content_description",
        )
        getter = action_getter_for(
            {"image_generator_plugin-neo:action:draw_image": _FakeTagsAction}
        )
        cfg = make_config()
        first = tag_hint.inject_tag_first_hint([spec], config=cfg, action_getter=getter)
        once = param_description(_FakeTagsAction, "content_description")
        second = tag_hint.inject_tag_first_hint(
            [spec], config=cfg, action_getter=getter
        )
        twice = param_description(_FakeTagsAction, "content_description")
        restored = tag_hint.restore_tag_first_hint(action_getter=getter)
        after_restore = param_description(_FakeTagsAction, "content_description")
        key = "image_generator_plugin-neo:action:draw_image.content_description"
        ok = (
            first.injected == [key]
            and once.count("附加要求") == 1
            and second.injected == []
            and second.already == [key]  # 第二次识别为"已注入过"
            and twice == once  # 连续两次调用，说明一模一样（只追加一次）
            and restored == [key]
            and after_restore == FAKE_TAGS_DESC  # 卸载后回到原文
        )
        check(
            "C59 注入幂等（只追加一次）且卸载可还原",
            ok,
            f"一次后 = {once!r}\n        二次后 = {twice!r}\n"
            f"        还原后 = {after_restore!r}\n"
            f"        还原列表 = {restored!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C59 注入幂等（只追加一次）且卸载可还原",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C14/C15 需要真调小模型 ─────────────────────────────────────
    if RUN_LLM:
        try:
            from src.core.config.model_config import init_model_config

            init_model_config(str(NEO_ROOT / "config" / "model.toml"))
        except Exception as e:  # noqa: BLE001
            print(f"        （模型配置初始化失败：{type(e).__name__}: {e}）")

        try:
            cfg = make_config(roster="", use_llm=True)
            out = await fire(
                cfg, natural_params("帮我画一张她战斗的样子", text="我们来聊爱莉希雅吧")
            )
            c = out["args"]["content"]
            check(
                "[LLM] 角色表为空时兜底提取",
                "爱莉希雅" in c or "elysia" in c.lower(),
                f"content = {c!r}",
            )
        except Exception as e:  # noqa: BLE001
            check("[LLM] 角色表为空时兜底提取", False, f"{type(e).__name__}: {e}")

        try:
            cfg = make_config(roster=RS_ONE, use_llm=True)
            out = await fire(
                cfg,
                natural_params("爱莉希雅站在左边挥手", text="爱莉希雅和铃兰要同框"),
            )
            c = out["args"]["content"]
            check(
                "[LLM] 已含表内角色时仍兜底识别表外角色",
                "铃兰" in c,
                f"content = {c!r}",
            )
        except Exception as e:  # noqa: BLE001
            check(
                "[LLM] 已含表内角色时仍兜底识别表外角色",
                False,
                f"{type(e).__name__}: {e}",
            )

    print("=" * 72)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"结果：{passed}/{len(_results)} 通过")
    for name, ok, _ in _results:
        if not ok:
            print(f"  未通过：{name}")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_cases()))
