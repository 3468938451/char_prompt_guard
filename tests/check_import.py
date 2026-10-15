r"""真实导入冒烟 + manifest 自检。

「语法对」和「框架能加载」是两件事：框架的插件加载器是把入口文件当
``char_prompt_guard.plugin`` 直接执行的，导入期一处循环引用或一个写错的
API 路径就会让整个插件加载失败，而 ``py_compile`` 完全看不出来。

本脚本做四件事：

1. 逐个 import 插件模块，真实暴露加载期错误；
2. 校验 manifest.json：无 BOM、必需字段、name 与目录名一致、entry_point 存在；
3. 校验 manifest 声明的 api_version 模块在框架里真的存在（§11.1 踩过的坑）；
4. 实例化配置模型，钉住几处关键默认值。

用法（用实例自带 venv，别用系统 python）：

    $env:NEO_ROOT="<你的 Neo-MoFox 框架根>"
    & "$env:NEO_ROOT\.venv\Scripts\python.exe" <插件目录>\tests\check_import.py

不设 ``NEO_ROOT`` 时按「插件装在 <框架根>\plugins\<插件名>」倒推两级。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

# 控制台可能是 GBK，脚本会打 ✓，直接 print 会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001, S110 —— 控制台不支持重配置时忽略即可，输出编码由终端决定
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent
PLUGIN_NAME = PLUGIN_DIR.name

MODULES = [
    PLUGIN_NAME,
    f"{PLUGIN_NAME}.character",
    f"{PLUGIN_NAME}.config",
    f"{PLUGIN_NAME}.handler",
    f"{PLUGIN_NAME}.memory",
    f"{PLUGIN_NAME}.monitor",
    f"{PLUGIN_NAME}.plugin",
    f"{PLUGIN_NAME}.tag_hint",
]

# manifest 必需字段（框架缺一个直接拒绝加载）
REQUIRED_FIELDS = (
    "name",
    "version",
    "description",
    "author",
    "dependencies",
    "entry_point",
)

_failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    """打印一条检查结果。"""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {extra}" if extra else ""))
    if not ok:
        _failures.append(name)
    return ok


def main() -> int:
    """入口。"""
    sys.path.insert(0, str(NEO_ROOT))
    sys.path.insert(0, str(PLUGIN_DIR.parent))

    # 相对导入（from ..handler import ...）要求本模块属于 char_prompt_guard.tests 包。
    # 直接 `python tests\check_import.py` 时 __package__ 为空，这里补上包名，
    # 于是两种跑法都能用：
    #   python <插件目录>\tests\check_import.py
    #   python -m char_prompt_guard.tests.check_import
    if __package__ in (None, ""):
        globals()["__package__"] = "char_prompt_guard.tests"

    print(f"框架根   : {NEO_ROOT}")
    print(f"插件目录 : {PLUGIN_DIR}")
    print("-" * 72)

    # ── 1. 逐个 import ────────────────────────────────────────────
    for module in MODULES:
        try:
            importlib.import_module(module)
            check(f"import {module}", True)
        except Exception as e:  # noqa: BLE001
            check(f"import {module}", False, f"{type(e).__name__}: {e}")

    # ── 2. manifest.json ──────────────────────────────────────────
    mf_path = PLUGIN_DIR / "manifest.json"
    if not check("manifest.json 存在且根级", mf_path.is_file()):
        return 1

    raw = mf_path.read_bytes()
    check("manifest.json 无 BOM", not raw.startswith(b"\xef\xbb\xbf"))

    try:
        mf = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        check("manifest.json 可解析", False, f"{type(e).__name__}: {e}")
        return 1

    for field in REQUIRED_FIELDS:
        check(f"manifest 必需字段 {field}", field in mf)

    check(
        "manifest.name 与目录名一致",
        mf.get("name") == PLUGIN_NAME,
        f"{mf.get('name')!r} vs {PLUGIN_NAME!r}",
    )
    entry = str(mf.get("entry_point") or "")
    check("entry_point 文件存在", bool(entry) and (PLUGIN_DIR / entry).is_file(), entry)
    check("LICENSE 随包（MIT 条款要求）", (PLUGIN_DIR / "LICENSE").is_file())
    check(
        "dependencies.plugins 只写真正用到的",
        not (mf.get("dependencies") or {}).get("plugins"),
    )

    # ── 3. api_version 声明的模块真实存在 ─────────────────────────
    for module_name in mf.get("api_version") or {}:
        target = f"src.app.plugin_system.api.{module_name}"
        try:
            importlib.import_module(target)
            check(f"api_version 模块存在 {target}", True)
        except Exception as e:  # noqa: BLE001
            check(f"api_version 模块存在 {target}", False, f"{type(e).__name__}: {e}")

    # ── 4. include 声明的组件与代码一致 ───────────────────────────
    handler_mod = sys.modules.get(f"{PLUGIN_NAME}.handler")
    declared = [
        item
        for item in (mf.get("include") or [])
        if item.get("component_type") == "event_handler"
    ]
    for item in declared:
        comp_name = str(item.get("component_name") or "")
        found = False
        if handler_mod is not None:
            for attr in vars(handler_mod).values():
                if isinstance(attr, type) and getattr(attr, "name", None) == comp_name:
                    found = True
                    break
        check(f"include 声明的组件存在 [{comp_name}]", found)

    # ── 4.5 订阅事件必须同时覆盖 action 与 tool 两条路径 ──────────
    try:
        from ..handler import CharacterGuardHandler

        events = [
            e.value if hasattr(e, "value") else str(e)
            for e in CharacterGuardHandler.init_subscribe
        ]
        check("订阅 before_action_call", "before_action_call" in events, str(events))
        check(
            "订阅 before_tool_call（agent 聊天器把 Action 当工具调用）",
            "before_tool_call" in events,
        )
        # 1.0.17：参数说明注入的重试时机（本插件常比生图插件先加载完）。
        # 老核心没有这个事件时不该算失败 —— 那种情况插件会自动退回「首次生图事件」。
        from src.core.components.types import EventType as _EventType

        has_all_loaded_event = (
            getattr(_EventType, "ON_ALL_PLUGIN_LOADED", None) is not None
        )
        check(
            "订阅 on_all_plugin_loaded（参数说明注入的重试时机）",
            ("on_all_plugin_loaded" in events) or not has_all_loaded_event,
            "核心未提供该事件" if not has_all_loaded_event else str(events),
        )
    except Exception as e:  # noqa: BLE001
        check("读取订阅事件", False, f"{type(e).__name__}: {e}")

    # ── 5. 配置默认值 ─────────────────────────────────────────────
    try:
        from ..config import CharPromptGuardConfig

        cfg = CharPromptGuardConfig()
        check("默认启用", cfg.general.enabled is True)
        check("默认只对 drawing 生效", cfg.general.apply_modes == "drawing")
        check("默认开启角色表匹配", cfg.roster.enabled is True)
        check("默认开启多角色", cfg.multi.enabled is True)
        check("多人判定默认由 bot 决定", cfg.multi.decision == "bot")
        check("新的多人判定开关默认 bot", cfg.multi.decide == "bot")
        check("主角粘性时效默认 6 小时", cfg.multi.stickiness_hours == 6.0)
        check("连点两人判定窗口默认 5 分钟", cfg.multi.multi_window_minutes == 5.0)
        check("二次一致性检测默认 warn", cfg.general.consistency_check == "warn")
        check("坐标默认分散", cfg.multi.coord_plan == "spread")
        check("natural 人数上限默认 2", cfg.multi.natural_max == 2)
        check("LLM 提取有超时兜底", cfg.general.llm_timeout > 0)
        # 1.0.12 新增的两个开关
        check(
            "1.0.12 默认提醒主模型写标准标签",
            cfg.general.notify_tag_rule is True,
            f"notify_tag_rule={cfg.general.notify_tag_rule!r}",
        )
        check(
            "1.0.12 默认自我指代不补",
            cfg.general.skip_on_self_reference is True,
            f"skip_on_self_reference={cfg.general.skip_on_self_reference!r}",
        )
        # 1.0.13 新增：本尊身份（留空时回落到框架人格配置的 personality.nickname）
        check(
            "1.0.13 本尊身份默认留空（留空读人格配置 nickname）",
            cfg.general.true_self == "",
            f"true_self={cfg.general.true_self!r}",
        )
        # 1.0.14 新增：主题跟踪（[monitor] 节）
        check(
            "1.0.14 默认启用主题跟踪",
            cfg.monitor.enabled is True,
            f"monitor.enabled={cfg.monitor.enabled!r}",
        )
        check(
            "1.0.14 主题跟踪窗口默认 15 条",
            cfg.monitor.window == 15,
            f"monitor.window={cfg.monitor.window!r}",
        )
        check(
            "1.0.14 主题置信度阈值默认 0.5",
            cfg.monitor.min_confidence == 0.5,
            f"monitor.min_confidence={cfg.monitor.min_confidence!r}",
        )
        check(
            "1.0.14 主题时间衰减半衰期默认 45 分钟",
            cfg.monitor.half_life_minutes == 45.0,
            f"monitor.half_life_minutes={cfg.monitor.half_life_minutes!r}",
        )
        # 1.0.17 新增：把「标签在最前」写进生图动作的**参数说明**
        check(
            "1.0.17 默认注入参数说明（tag_first_hint）",
            cfg.general.tag_first_hint is True,
            f"tag_first_hint={cfg.general.tag_first_hint!r}",
        )
        # 1.0.18 新增：外部记忆库（[memory] 节，Booku Memory）
        check(
            "1.0.18 默认启用记忆库关联",
            cfg.memory.enabled is True,
            f"memory.enabled={cfg.memory.enabled!r}",
        )
        check(
            "1.0.18 记忆库地址默认 localhost:8000/booku-memory",
            isinstance(cfg.memory.base_url, str)
            and cfg.memory.base_url.startswith("http://localhost:8000/booku-memory"),
            f"memory.base_url={cfg.memory.base_url!r}",
        )
        check(
            "1.0.18 记忆库请求超时默认 2.0 秒",
            cfg.memory.timeout == 2.0,
            f"memory.timeout={cfg.memory.timeout!r}",
        )
        check(
            "1.0.18 记忆库检索条数上限默认 5",
            cfg.memory.limit == 5,
            f"memory.limit={cfg.memory.limit!r}",
        )
        check(
            "1.0.18 默认把确认结果写回记忆库",
            cfg.memory.write_back is True,
            f"memory.write_back={cfg.memory.write_back!r}",
        )
        check(
            '1.0.18 写入 bucket 默认 "memory"',
            cfg.memory.bucket == "memory",
            f"memory.bucket={cfg.memory.bucket!r}",
        )
        check(
            '1.0.18 写入 memory_type 默认 "knowledge"',
            cfg.memory.memory_type == "knowledge",
            f"memory.memory_type={cfg.memory.memory_type!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("配置模型可实例化", False, f"{type(e).__name__}: {e}")

    # ── 5.1 reminder 正文：标记说明必须保留，标签硬规则随开关增减 ────
    try:
        from ..plugin import _REMINDER_CONTENT, build_reminder_content

        cfg = CharPromptGuardConfig()
        with_rule = build_reminder_content(cfg)
        cfg.general.notify_tag_rule = False
        without_rule = build_reminder_content(cfg)
        check(
            "reminder 保留原有标记说明",
            "#multi#" in with_rule and "#nochar#" in with_rule,
        )
        check(
            "reminder 默认含「标签写在最前面」硬规则",
            with_rule != without_rule
            and "最前面" in with_rule
            and "content_description" in with_rule,
        )
        check(
            "notify_tag_rule=false 时 reminder 只保留标记说明",
            without_rule == _REMINDER_CONTENT and "硬规则" not in without_rule,
        )
        check(
            "配置读不到时 reminder 仍含标签规则（按默认）",
            "最前面" in build_reminder_content(object()),
        )
    except Exception as e:  # noqa: BLE001
        check("reminder 正文可拼装", False, f"{type(e).__name__}: {e}")

    # ── 5.15 参数说明注入（1.0.17）：文案 + 开关语义 ────────────────
    try:
        from .. import tag_hint
        from ..config import CharPromptGuardConfig as HintConfig

        check(
            "追加句含「标准标签」与转义示例",
            "标准标签" in tag_hint.HINT_SUFFIX
            and "Hanabi \\(Honkai: Star Rail\\)" in tag_hint.HINT_SUFFIX
            and tag_hint.HINT_MARKER in tag_hint.HINT_SUFFIX,
            f"HINT_SUFFIX = {tag_hint.HINT_SUFFIX!r}",
        )
        # 1.0.19：追加句在原句之后**再补一句** —— 标签必须与本次画面的角色一致，
        # 不要照搬备忘 / 历史记录 / 上一张图的标签（实测事故：主模型照搬了备忘里
        # 过期的固定标签 Hanabi，而这次画的是爱莉希雅）。原句一字不动。
        check(
            "1.0.19 追加句要求「标签与本次画面一致、不要照搬备忘」",
            "不要照搬" in tag_hint.HINT_SUFFIX
            and "本次画面描述的角色一致" in tag_hint.HINT_SUFFIX
            and "备忘" in tag_hint.HINT_SUFFIX
            and "历史记录" in tag_hint.HINT_SUFFIX
            and "elysia \\(honkai impact 3rd\\)" in tag_hint.HINT_SUFFIX
            and "爱莉希雅" in tag_hint.HINT_SUFFIX,
            f"HINT_SUFFIX = {tag_hint.HINT_SUFFIX!r}",
        )
        check(
            "追加句与原说明用换行分隔（只追加、不覆盖）",
            tag_hint.HINT_SEPARATOR == "\n",
        )

        hint_cfg = HintConfig()
        check(
            "默认开启参数说明注入",
            tag_hint.tag_hint_enabled(hint_cfg) is True,
        )
        hint_cfg.general.tag_first_hint = False
        check(
            "tag_first_hint=false 时不注入",
            tag_hint.tag_hint_enabled(hint_cfg) is False,
        )
        hint_cfg.general.tag_first_hint = True
        hint_cfg.general.enabled = False
        check(
            "general.enabled=false 时同样不注入",
            tag_hint.tag_hint_enabled(hint_cfg) is False,
        )
        hint_cfg.general.enabled = True
        hint_cfg.targets.enabled = False
        check(
            "targets.enabled=false 时同样不注入",
            tag_hint.tag_hint_enabled(hint_cfg) is False,
        )
        check(
            "配置读不到时按默认（开）处理，且不抛异常",
            tag_hint.tag_hint_enabled(object()) is True,
        )

        # 空表/无签名不炸：这是「静默降级」的最小回归
        empty = tag_hint.inject_tag_first_hint(
            [], config=HintConfig(), action_getter=lambda _sig: None
        )
        check(
            "没有目标动作时返回「未注入」而不抛异常",
            empty.ok is False and empty.injected == [],
            empty.log_line(),
        )
        check(
            "未知结构（plugin 为空的签名）被跳过",
            tag_hint.target_signature(object()) == "",
        )
    except Exception as e:  # noqa: BLE001
        check("参数说明注入模块可调用", False, f"{type(e).__name__}: {e}")

    # ── 5.17 记忆库关联（1.0.18）：地址容错、关闭时不发请求、结构解析 ────
    try:
        from .. import memory as memory_module
        from ..config import CharPromptGuardConfig as MemoryConfig

        async def _check_memory_module() -> None:
            check(
                "结尾斜杠容错（…/booku-memory/ 等价）",
                memory_module.normalize_base_url("http://localhost:8000/booku-memory/")
                == "http://localhost:8000/booku-memory",
            )
            check(
                "空地址回落到默认值",
                memory_module.normalize_base_url("") == memory_module.DEFAULT_BASE_URL,
            )
            check(
                "非 http/https 地址被拒绝（返回空串）",
                memory_module.normalize_base_url("file:///etc/passwd") == ""
                and memory_module.normalize_base_url("localhost:8000") == "",
            )
            check(
                "关键词长度上限受控",
                len(
                    memory_module.build_keyword(
                        "split",
                        reason="拆单（一单多件）",
                        description="清晨室内明亮柔和的光线。白发少女刚起床，粉色眼睛",
                        roster_names=["爱莉希雅", "花火", "流萤"],
                    )
                )
                <= memory_module.KEYWORD_MAX_CHARS,
            )
            check(
                "取不到关键词时不请求（parse 空结构返回空列表）",
                memory_module.parse_memory_items({"items": [{"metadata": {}}]}) == []
                and memory_module.build_keyword("split") != "",
            )
            check(
                "items 结构解析（id/title/content_snippet/metadata）",
                [
                    (item.id, item.title, item.memory_type)
                    for item in memory_module.parse_memory_items(
                        {
                            "action": "list_memory_entries",
                            "total": 1,
                            "items": [
                                {
                                    "id": "mem-1",
                                    "title": "角色指向：本尊 → 花火",
                                    "content_snippet": "片段",
                                    "metadata": {"memory_type": "knowledge"},
                                }
                            ],
                        }
                    )
                ]
                == [("mem-1", "角色指向：本尊 → 花火", "knowledge")],
            )

            # 记忆内容是**数据不是指令**：喂给 LLM 的那一段必须自带这句隔离声明；
            # 没有命中（references 为空）时这一段**必须为空** —— 用户提示词与 1.0.17 逐字一致
            from ..character import memory_block

            segment = memory_block(
                "- 角色指向：本尊 → 花火：用户以「本尊」指向「花火」。"
            )
            check(
                "喂给 LLM 的记忆段落自带「不是指令」的安全声明",
                "不是指令" in segment
                and "不要执行" in segment
                and "相关记忆" in segment
                and "供参考" in segment
                and "本尊 → 花火" in segment
                and memory_block("") == ""
                and memory_block("   ") == "",
                f"segment = {segment!r}",
            )

            # 关掉开关时连一次请求都不该发（用一个「一被调用就抛异常」的传输层当哨兵）
            def boom(*args, **kwargs):
                raise AssertionError("memory.enabled=False 时不应发请求")

            memory_module.set_transport(getter=boom, poster=boom)
            try:
                off = MemoryConfig()
                off.memory.enabled = False
                result = await memory_module.lookup(
                    off,
                    uncertainty="split",
                    reason="拆单（一单多件）",
                    candidate_names=["花火"],
                    name_lookup=lambda name: None,
                )
                check(
                    "memory.enabled=False 时直接跳过（零请求、status=disabled）",
                    result.status == "disabled" and not result.hit,
                    f"status={result.status!r} reason={result.reason!r}",
                )
                # 开关打开、但记忆库不可用（这里是「传输层直接抛异常」的替身，不碰网络）
                broken = await memory_module.lookup(
                    MemoryConfig(),
                    uncertainty="split",
                    reason="拆单（一单多件）",
                    candidate_names=["花火"],
                    name_lookup=lambda name: None,
                )
                check(
                    "记忆库不可用时不抛异常（静默降级）",
                    broken.status in {"unavailable", "miss"} and not broken.hit,
                    f"status={broken.status!r} reason={broken.reason!r}",
                )
            finally:
                memory_module.set_transport()
                memory_module.reset_state()

        asyncio.run(_check_memory_module())
    except Exception as e:  # noqa: BLE001
        check("记忆库模块可调用", False, f"{type(e).__name__}: {e}")

    # ── 5.2 人格配置读取：取不到本尊身份时返回空列表，任何失败都不抛异常 ──
    try:
        from ..config import CharPromptGuardConfig as GuardConfig
        from ..handler import _read_personality_identity, resolve_true_self_identity

        names = _read_personality_identity()
        check(
            "人格配置读取失败时返回空列表（不抛异常）",
            isinstance(names, list) and all(isinstance(name, str) for name in names),
            f"names={names!r}",
        )

        cfg = GuardConfig()
        cfg.general.true_self = "爱莉希雅"
        identity = resolve_true_self_identity(cfg)
        check(
            "true_self 非空时优先于人格配置（来源=配置）",
            identity.candidates == ["爱莉希雅"] and identity.source == "配置",
            f"candidates={identity.candidates!r} source={identity.source!r}",
        )

        cfg.general.true_self = ""
        fallback = resolve_true_self_identity(cfg)
        check(
            "true_self 留空时候选名来自人格配置（取不到则为空）",
            isinstance(fallback.candidates, list)
            and fallback.source in {"", "人格配置"},
            f"candidates={fallback.candidates!r} source={fallback.source!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("人格配置读取函数可调用", False, f"{type(e).__name__}: {e}")

    print("-" * 72)
    if _failures:
        print(f"✗ {len(_failures)} 项未通过：" + "、".join(_failures))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
