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
    f"{PLUGIN_NAME}.compat",
    f"{PLUGIN_NAME}.config",
    f"{PLUGIN_NAME}.handler",
    f"{PLUGIN_NAME}.memory",
    f"{PLUGIN_NAME}.monitor",
    f"{PLUGIN_NAME}.outfit",
    f"{PLUGIN_NAME}.plugin",
    f"{PLUGIN_NAME}.region",
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
        # 1.0.34 新增：框架运行时兼容（探测到框架不发执行前事件时，在内存里补一层）
        check(
            "1.0.34 默认启用框架运行时兼容（runtime_framework_compat）",
            cfg.general.runtime_framework_compat is True,
            f"runtime_framework_compat={cfg.general.runtime_framework_compat!r}",
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
        # 1.0.21 新增：角色表之外的角色用已配置的模型解析成标准标签
        check(
            "1.0.21 默认启用角色解析（auto_resolve_characters）",
            cfg.general.auto_resolve_characters is True,
            f"auto_resolve_characters={cfg.general.auto_resolve_characters!r}",
        )
        # 1.0.21：角色表示例只留一行「爱莉希雅」（方便照抄改掉），
        # 但四列写法、分隔符与注释规则仍然写在说明里。
        # 说明文本从模型字段里读：框架自己的 Field 与 pydantic v2 的 FieldInfo
        # 都有 .description（分别从类属性 / model_fields 取），
        # 两处都取不到时用空串 —— 断言会因此失败，正好暴露差异。
        roster_field = type(cfg.roster).__dict__.get("characters")
        if roster_field is None:
            roster_field = type(cfg.roster).model_fields.get("characters")
        roster_desc = str(getattr(roster_field, "description", "") or "")
        check(
            "1.0.21 角色表示例只留一行爱莉希雅（照抄改掉即可）",
            "爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | "
            r"elysia \(honkai impact 3rd\)"
            in roster_desc
            and "伊甸" not in roster_desc
            and "留空则只用 LLM 从上文识别" in roster_desc,
            f"characters.description={roster_desc!r}",
        )
        # 1.0.22 新增：皮肤/时装（[outfits] 节）
        check(
            "1.0.22 默认启用皮肤/时装识别（outfits.enabled）",
            cfg.outfits.enabled is True,
            f"outfits.enabled={cfg.outfits.enabled!r}",
        )
        check(
            "1.0.22 皮肤表默认留空（表外的皮肤交给模型解析）",
            cfg.outfits.entries == "",
            f"outfits.entries={cfg.outfits.entries!r}",
        )
        check(
            "1.0.22 默认允许把表外皮肤交给模型解析（auto_resolve）",
            cfg.outfits.auto_resolve is True,
            f"outfits.auto_resolve={cfg.outfits.auto_resolve!r}",
        )
        check(
            "1.0.22 默认把解析成功的皮肤写回记忆库（write_back）",
            cfg.outfits.write_back is True,
            f"outfits.write_back={cfg.outfits.write_back!r}",
        )
        # 1.0.23 新增：没说名字的那一套（模型推断）**默认关** —— 有编造风险，
        # 必须用户显式打开；关掉时 0 次模型调用、无相关日志（见 check_logic.py 的 C83）。
        check(
            "1.0.23 默认不让模型推断「没说名字的那一套」（outfits.infer_unnamed=false）",
            cfg.outfits.infer_unnamed is False,
            f"outfits.infer_unnamed={cfg.outfits.infer_unnamed!r}",
        )
        # 1.0.22：皮肤表也只留一行示例「粉色妖精小姐」（方便照抄改掉），
        # 四列写法、分隔符与注释规则仍然写在说明里（与角色表那一条同一口径）。
        outfits_field = type(cfg.outfits).__dict__.get("entries")
        if outfits_field is None:
            outfits_field = type(cfg.outfits).model_fields.get("entries")
        outfits_desc = str(getattr(outfits_field, "description", "") or "")
        check(
            "1.0.22 皮肤表示例只留一行粉色妖精小姐（照抄改掉即可）",
            "粉色妖精小姐 | 爱莉希雅 | pink elf dress, fairy wings | "
            "爱莉希雅的粉色妖精装"
            in outfits_desc
            and "\n" not in outfits_desc
            and "照抄改掉即可" in outfits_desc
            and "流萤" not in outfits_desc
            and "春婵丽娟" not in outfits_desc,
            f"entries.description={outfits_desc!r}",
        )
        # 1.0.28 新增：泛指的穿着类型（学生 / 居家 / 泳衣 …）——**默认留空** =
        # 只用内置表 WEAR_TYPE_KEYWORDS；填了与内置表**合并**、同名覆盖内置
        # （行为见 check_logic.py 的 C89~C106）。
        check(
            "1.0.28 穿着类型表默认留空（只用内置表）",
            cfg.outfits.wear_types == "",
            f"outfits.wear_types={cfg.outfits.wear_types!r}",
        )
        wear_field = type(cfg.outfits).__dict__.get("wear_types")
        if wear_field is None:
            wear_field = type(cfg.outfits).model_fields.get("wear_types")
        wear_desc = str(getattr(wear_field, "description", "") or "")
        check(
            "1.0.28 穿着类型表示例只留一行（照抄改掉即可，且写明不是具体皮肤名）",
            "学生 | school uniform, pleated skirt" in wear_desc
            and "\n" not in wear_desc
            and "照抄改掉即可" in wear_desc
            and "不是具体皮肤名" in wear_desc,
            f"wear_types.description={wear_desc!r}",
        )
        # 1.0.36 新增：**实验性功能**「分区覆盖」（[region_override]）—— 默认关闭，
        # 独立的条目列表（不写进 [roster] / [outfits]），五个分区字段 + 一个角色名。
        check(
            "1.0.36 分区覆盖默认关闭（region_override.enabled=false）",
            cfg.region_override.enabled is False,
            f"region_override.enabled={cfg.region_override.enabled!r}",
        )
        check(
            "1.0.36 分区覆盖条目默认空列表（独立列表，不塞进 [roster]/[outfits]）",
            cfg.region_override.entries == [],
            f"region_override.entries={cfg.region_override.entries!r}",
        )
        check(
            "1.0.36 分区覆盖默认：negative_swap/anti_mix 开、stylized_swap 关",
            cfg.region_override.negative_swap is True
            and cfg.region_override.anti_mix is True
            and cfg.region_override.stylized_swap is False,
            (
                f"negative_swap={cfg.region_override.negative_swap!r} "
                f"anti_mix={cfg.region_override.anti_mix!r} "
                f"stylized_swap={cfg.region_override.stylized_swap!r}"
            ),
        )
        check(
            "1.0.36 分区顺序默认 head→body→outfit→pose→background",
            cfg.region_override.regions
            == ["head", "body", "outfit", "pose", "background"],
            f"region_override.regions={cfg.region_override.regions!r}",
        )
        region_enabled_field = type(cfg.region_override).__dict__.get("enabled")
        if region_enabled_field is None:
            region_enabled_field = type(cfg.region_override).model_fields.get(
                "enabled"
            )
        region_desc = str(getattr(region_enabled_field, "description", "") or "")
        check(
            "1.0.36 分区覆盖的说明第一句就是「实验性功能（默认关闭）」+ 效果警告",
            region_desc.startswith("实验性功能（默认关闭）：分区覆盖。")
            and "生图效果可能不理想" in region_desc
            and "默认关闭，普通使用完全不受影响" in region_desc,
            f"enabled.description={region_desc!r}",
        )
        from ..region import (
            DEFAULT_REGIONS,
            RegionSwap,
            parse_region_entries,
            parse_region_overrides,
            parse_regions,
        )

        entry = {
            "name": "流萤",
            "head": "silver hair, blue eyes",
            "body": "slender",
        }
        parsed_entries = parse_region_entries([entry])
        check(
            "1.0.36 分区条目解析：name + 各分区字段 → RegionEntry",
            len(parsed_entries) == 1
            and parsed_entries[0].name == "流萤"
            and parsed_entries[0].keywords("head") == ("silver hair", "blue eyes")
            and parsed_entries[0].keywords("body") == ("slender",)
            and parsed_entries[0].keywords("pose") == (),
            f"entries={parsed_entries!r}",
        )
        check(
            "1.0.36 分区顺序解析：非法值丢弃、整份都非法时用默认五个分区",
            parse_regions("body, head, 不存在") == ("body", "head")
            and parse_regions("") == DEFAULT_REGIONS,
            (
                f"parse_regions('body, head, 不存在')="
                f"{parse_regions('body, head, 不存在')!r}"
            ),
        )
        check(
            "1.0.36 三重条件缺一不认：只有角色名 / 只有分区词都不解析",
            parse_region_overrides("画流萤", [], parsed_entries) == {}
            and parse_region_overrides("头疼", [], parsed_entries) == {}
            and parse_region_overrides(
                "头换成流萤", [], parsed_entries
            )
            == {"head": "流萤"},
            (
                f"画流萤={parse_region_overrides('画流萤', [], parsed_entries)!r} "
                f"头换成流萤="
                f"{parse_region_overrides('头换成流萤', [], parsed_entries)!r}"
            ),
        )
        check(
            "1.0.36 RegionSwap 空结果不算成功（没拼出东西就不注入）",
            RegionSwap().ok is False and RegionSwap().parts == (),
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
        # 1.0.20：追加句再补第三句 —— 用户点名的角色**不在角色表里**时，主模型没有
        # 可用的标准标签，最容易做的事就是"拿备忘里的旧标签顶上"（实测事故就是
        # hanabi 被挂在了一张外观完全不同的画面上）。这一句把正确做法写死：
        # 不写任何标准标签，只写画面描述。前两句同样一字不动。
        check(
            "1.0.20 追加句要求「角色表之外的角色不写任何标准标签、只写画面描述」",
            "角色表之外" in tag_hint.HINT_SUFFIX
            and "不要写任何标准标签" in tag_hint.HINT_SUFFIX
            and "只写画面描述" in tag_hint.HINT_SUFFIX
            and "不要拿备忘" in tag_hint.HINT_SUFFIX
            and tag_hint.HINT_SUFFIX.index("不要照搬")
            < tag_hint.HINT_SUFFIX.index("角色表之外"),
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

    # ── 5.20 表外点名（1.0.20）：提取函数与冲突检测入口都在、开关语义不变 ──
    # 这一节只做"装得上、调得动"的冒烟 —— 完整的判定行为在 check_logic.py 的
    # C68~C71；这里钉住三件事：① 提取规则对实测事故那句话给出「刻律德拉」，
    # 且角色表内的名字不会被当成表外；② 「删除角色引用」在没有可删文本时
    # 原样返回、处数为 0（调用方据此退回 warn）；③ 复用 1.0.19 的开关，
    # 没有新增配置项。
    try:
        from .. import handler as guard_handler
        from ..character import (
            CharacterEntry,
            delete_character_reference,
            extract_outside_names,
        )
        from ..config import CharPromptGuardConfig as OutsideCfg

        roster_one = [
            CharacterEntry(
                name="花火",
                game="崩坏：星穹铁道 (Honkai: Star Rail)",
                aliases=["hanabi"],
                tag=r"hanabi \(honkai: star rail\)",
            )
        ]
        quoted = extract_outside_names("这身是「刻律德拉」？", roster_one)
        inside = extract_outside_names("这身是「花火」吧？", roster_one)
        check(
            "1.0.20 引号里的表外角色名能被提取出来（表内的不算）",
            quoted == ["刻律德拉"] and inside == [],
            f"表外={quoted!r} 表内={inside!r}",
        )

        # 1.0.23：皮肤名豁免 —— 本次皮肤识别认定的名字不再算「表外角色名」，
        # 被豁免的候选会记进 dropped（只记那些"本来会被报出来"的候选）。
        exempted_dropped: list[str] = []
        exempted = extract_outside_names(
            "画「粉色妖精小姐」这套",
            roster_one,
            exclude=["粉色妖精小姐"],
            dropped=exempted_dropped,
        )
        check(
            "1.0.23 皮肤名豁免：exclude 里的名字不再算表外角色名（并记录被豁免的候选）",
            exempted == []
            and exempted_dropped == ["粉色妖精小姐"]
            and extract_outside_names("画「粉色妖精小姐」这套", roster_one)
            == ["粉色妖精小姐"],  # 不传 exclude 时照旧抓得到（否则这条是空转）
            f"豁免后={exempted!r} 被豁免的候选={exempted_dropped!r}",
        )

        hanabi = roster_one[0]
        cleaned, hits = delete_character_reference(
            "Hanabi (Honkai: Star Rail) 银白色长发的少女站在镜头前", hanabi
        )
        no_hit, no_hits = delete_character_reference(
            "银白色长发的少女站在镜头前", hanabi
        )
        check(
            "1.0.20 「删除角色引用」删掉标签与作品名括号、保留外观描述",
            hits >= 1
            and "hanabi" not in cleaned.lower()
            and "花火" not in cleaned
            and "Honkai" not in cleaned
            and cleaned.startswith("银白色长发的少女站在镜头前")
            and no_hits == 0
            and no_hit == "银白色长发的少女站在镜头前",
            f"cleaned={cleaned!r} hits={hits} / 无命中时={no_hit!r}",
        )

        check(
            "1.0.20 冲突检测入口存在、开关仍复用 consistency_check（默认 warn）",
            hasattr(guard_handler.CharacterGuardHandler, "_scan_user_naming")
            and hasattr(
                guard_handler.CharacterGuardHandler, "_apply_outside_naming_check"
            )
            and isinstance(guard_handler.UserNamingScope().names, list)
            and OutsideCfg().general.consistency_check == "warn",
        )
    except Exception as e:  # noqa: BLE001
        check("表外点名模块可调用", False, f"{type(e).__name__}: {e}")

    # ── 5.21 角色解析（1.0.21）：标签规范、严格校验、入口与开关 ──────────
    # 这一节只做「装得上、调得动」的冒烟 —— 完整的替换 / 补入行为在
    # check_logic.py 的 C72~C76；这里钉住三件事：
    # ① 标签规范真的会拦住模型的常见错法（大写、括号未转义、全角括号）；
    # ② unknown / 标签非法 / confidence 太低一律判成**解析失败**（返回 None）；
    # ③ 入口与缓存函数都在，且「关掉开关时一次模型都不问」。
    try:
        from .. import handler as guard_handler
        from ..character import (
            CHARACTER_RESOLVE_MIN_CONFIDENCE,
            RESOLVE_CACHE_TTL_SECONDS,
            parse_resolution_payload,
            reset_resolve_cache,
            resolve_cache_get,
            resolve_cache_put,
            validate_tag,
        )
        from ..character import resolve_character_via_llm as _resolve_fn
        from ..config import CharPromptGuardConfig as ResolveCfg

        good = r"kleidra \(honkai: star rail\)"
        check(
            "1.0.21 标签规范：合法标签通过",
            validate_tag(good)[0] is True,
            f"validate_tag({good!r}) = {validate_tag(good)!r}",
        )
        bad_tags = {
            "大写 + 括号未转义": "Kleidra (Honkai: Star Rail)",
            "全角括号": "kleidra（honkai: star rail）",
            "括号未转义": "kleidra (honkai: star rail)",
            "没有作品名括号": "kleidra",
            "大写作品名": "kleidra \\(Honkai: Star Rail\\)",
            "空": "",
        }
        rejected = {
            label: validate_tag(tag)[0] is False for label, tag in bad_tags.items()
        }
        check(
            "1.0.21 标签规范：大写 / 未转义括号 / 全角括号 / 空标签全部拦下",
            all(rejected.values()),
            f"逐项 = {rejected!r}",
        )

        ok_payload = {
            "unknown": False,
            "name_cn": "刻律德拉",
            "name_en": "Kleidra",
            "game_cn": "崩坏：星穹铁道",
            "game_en": "Honkai: Star Rail",
            "tag": good,
            "confidence": 0.9,
        }
        entry, reason, confidence = parse_resolution_payload(ok_payload)
        check(
            "1.0.21 解析结果：合法 JSON → 条目（名字 / 作品名 / 标签 / 英文别名）",
            entry is not None
            and entry.name == "刻律德拉"
            and entry.game == "崩坏：星穹铁道 (Honkai: Star Rail)"
            and entry.resolved_tag() == good
            # 别名**按模型给的原文**保留（这里模型写的是 name_en=`Kleidra`，就存
            # `Kleidra`，不会被改写成小写）；比较走大小写不敏感 —— 与实现
            # （``aliases`` 用小写去重、但保留第一次出现的写法）保持一致
            and "Kleidra" in entry.aliases
            and any(alias.lower() == "kleidra" for alias in entry.aliases)
            and confidence == 0.9
            and reason == "",
            f"entry={entry!r} reason={reason!r} confidence={confidence!r}",
        )
        unknown_entry, unknown_reason, _ = parse_resolution_payload({"unknown": True})
        low_entry, low_reason, low_conf = parse_resolution_payload(
            {**ok_payload, "confidence": 0.2}
        )
        bad_entry, bad_reason, _ = parse_resolution_payload(
            {**ok_payload, "tag": "Kleidra (Honkai: Star Rail)"}
        )
        check(
            "1.0.21 解析结果：unknown / 低 confidence / 非法标签都判失败",
            unknown_entry is None
            and "unknown" in unknown_reason
            and low_entry is None
            and low_conf == 0.2
            and str(CHARACTER_RESOLVE_MIN_CONFIDENCE) in low_reason
            and bad_entry is None
            and bad_reason != "",
            f"unknown={unknown_reason!r} low={low_reason!r} bad={bad_reason!r}",
        )

        # 1.0.23：作品名去重 —— 模型把英文作品名也塞进 game_cn 时不再拼第二遍
        # （实测日志：`… (Honkai: Star Rail) (Honkai: Star Rail)`）
        dup_game, dup_game_reason, _ = parse_resolution_payload(
            {
                **ok_payload,
                "game_cn": "崩坏：星穹铁道 (Honkai: Star Rail)",
                "game_en": "Honkai: Star Rail",
            }
        )
        check(
            "1.0.23 作品名去重：game_cn 已含 game_en 时只保留一个",
            dup_game is not None
            and dup_game.game == "崩坏：星穹铁道 (Honkai: Star Rail)"
            and "(Honkai: Star Rail) (Honkai: Star Rail)" not in dup_game.game
            and dup_game_reason == "",
            f"game={getattr(dup_game, 'game', '')!r}",
        )

        # 1.0.23：名字去重 —— name_cn / name_en 同名（忽略大小写 / 空白 / 括号）时
        # 别名表里不再多出一条同义项；不同名则都保留（上面那条已断言 Kleidra 在别名里）
        same_name, _, _ = parse_resolution_payload(
            {**ok_payload, "name_cn": "Kleidra", "name_en": "Kleidra"}
        )
        check(
            "1.0.23 名字去重：name_cn 与 name_en 同名时只留一个",
            same_name is not None
            and same_name.name == "Kleidra"
            and same_name.aliases == [],
            f"entry={same_name!r}",
        )

        check(
            "1.0.21 解析入口 / 缓存函数存在，缓存 TTL 为 24 小时",
            callable(_resolve_fn)
            and callable(reset_resolve_cache)
            and RESOLVE_CACHE_TTL_SECONDS == 24 * 60 * 60.0
            and hasattr(
                guard_handler.CharacterGuardHandler, "_resolve_outside_character"
            )
            and hasattr(guard_handler.CharacterGuardHandler, "_resolve_with_cache")
            and hasattr(guard_handler.CharacterGuardHandler, "_resolved_for_injection")
            and hasattr(guard_handler.CharacterGuardHandler, "_inject_resolved")
            # 1.0.21 修正：缓存**读写在调用方**（`_resolve_with_cache`），因此
            # character 侧必须真的提供可用的读 / 写 / 清空入口 —— 缓存写在被
            # 用例替换掉的那一层里时，「第二次还要不要问模型」（C75）永远绕不过模型
            and callable(resolve_cache_get)
            and callable(resolve_cache_put),
        )

        async def _check_resolve_off() -> None:
            from ..character import CharacterEntry as _Entry

            cfg = ResolveCfg()
            cfg.general.auto_resolve_characters = False
            reset_resolve_cache()
            instance = guard_handler.CharacterGuardHandler(None)
            scope = guard_handler.UserNamingScope(names=["刻律德拉"])
            off_result = await instance._resolve_outside_character(
                config=cfg,
                naming_scope=scope,
                texts=[r"kleidra \(honkai: star rail\)"],
                roster=[
                    _Entry(
                        name="花火",
                        game="崩坏：星穹铁道 (Honkai: Star Rail)",
                        aliases=["hanabi"],
                        tag=r"hanabi \(honkai: star rail\)",
                    )
                ],
            )
            # 开关开着、但画面里的角色是**用户这次点过的**表内角色 →
            # 既没有冲突可替换、也没有空缺要补入 → 同样一次模型都不问
            cfg.general.auto_resolve_characters = True
            idle_result = await instance._resolve_outside_character(
                config=cfg,
                naming_scope=guard_handler.UserNamingScope(
                    names=["刻律德拉"],
                    texts=["画花火吧，顺便看看这身"],
                    sources={"刻律德拉": "画花火吧，顺便看看这身"},
                ),
                texts=[r"hanabi \(honkai: star rail\), 白发少女"],
                roster=[
                    _Entry(
                        name="花火",
                        game="崩坏：星穹铁道 (Honkai: Star Rail)",
                        aliases=["hanabi"],
                        tag=r"hanabi \(honkai: star rail\)",
                    )
                ],
            )
            check(
                "1.0.21 auto_resolve_characters=false / 没有入口用得上 → 一次模型都不问",
                off_result is None and idle_result is None,
                f"off={off_result!r} idle={idle_result!r}",
            )

        asyncio.run(_check_resolve_off())
    except Exception as e:  # noqa: BLE001
        check("角色解析模块可调用", False, f"{type(e).__name__}: {e}")

    # ── 5.22 皮肤/时装（1.0.22）：皮肤表、关键词规范、严格校验与入口 ────────
    # 与 §5.21 同一口径：这里只做「装得上、调得动」的冒烟（完整的识别 / 注入行为
    # 在 check_logic.py 的 C77~C102）。重点钉三件事：
    # ① 皮肤表的四列能解析、"只写一部分"能命中；
    # ② 模型给的关键词真的会过规范（大写 / 中文 / 未转义括号一律不要）；
    # ③ unknown / confidence 太低 / 没有可用关键词一律判失败（什么都不注入）。
    # 1.0.23 再补：解析结构多一个可选字段 `inferred`、「<角色>的那一套皮肤」的识别、
    # 推断入口与缓存键都在（默认关见上面的配置默认值断言）。
    # 1.0.28 再补：内置「穿着类型 → 关键词」表（含同义写法）、容错匹配、用户补充表
    # 与内置表的合并（同名覆盖）与丢弃规则、穿着模型入口与 `<原词>|__wear__` 缓存键。
    # 1.0.29：匹配口径由"包含"收紧为"**相等或后缀**"（见下面那条断言的新措辞）——
    # 这是按新语义**收紧**断言（多钉两条否定样例），不是放宽。
    try:
        from .. import handler as outfit_handler
        from ..character import parse_roster
        from ..outfit import (
            OUTFIT_CACHE_TTL_SECONDS,
            OUTFIT_FAILURE_TTL_SECONDS,
            OUTFIT_RESOLVE_MIN_CONFIDENCE,
            WEAR_TYPE_KEYWORDS,
            find_outfit_entry,
            find_unnamed_outfit_owner,
            find_wear_type,
            infer_outfit_via_llm,
            outfit_cache_get,
            outfit_cache_put,
            outfit_infer_cache_key,
            outfit_wear_cache_key,
            parse_outfit_resolution_payload,
            parse_outfits,
            parse_wear_types,
            reset_outfit_cache,
            resolve_wear_via_llm,
            validate_outfit_keyword,
            validate_outfit_tag,
            wear_type_table,
        )

        table = parse_outfits(
            "粉色妖精小姐 | 爱莉希雅 | pink elf dress, fairy wings | 爱莉希雅的粉色妖精装"
        )
        check(
            "1.0.22 皮肤表：四列解析（关键词按逗号切开）",
            len(table) == 1
            and table[0].name == "粉色妖精小姐"
            and table[0].character == "爱莉希雅"
            and table[0].keywords == ["pink elf dress", "fairy wings"]
            and table[0].description == "爱莉希雅的粉色妖精装",
            f"table={table!r}",
        )
        check(
            "1.0.22 皮肤表：允许只写一部分（粉色妖精 → 粉色妖精小姐）",
            find_outfit_entry("粉色妖精", table) is table[0]
            and find_outfit_entry("粉色妖精小姐", table) is table[0]
            and find_outfit_entry("不存在的皮肤", table) is None,
        )
        rejected = {
            label: validate_outfit_keyword(word)[0] is False
            for label, word in {
                "大写": "Pink Elf Dress",
                "中文": "粉色长裙",
                "未转义括号": "pink dress (fairy)",
                "全角括号": "pink dress（fairy）",
                "空": "",
                "含逗号（不是一个关键词）": "pink elf dress, fairy wings",
            }.items()
        }
        check(
            "1.0.22/1.0.26 关键词规范：大写 / 中文 / 未转义与全角括号 / 空 / 含逗号 / "
            "形如角色标签（含 \\( … \\)）全部拦下，纯描述性关键词通过",
            all(rejected.values())
            and validate_outfit_keyword("pink elf dress")[0] is True
            # 1.0.26：`\\(…\\)` 是角色标签的形状 —— 皮肤通道绝不能把它带进画面
            and validate_outfit_keyword(r"ribbon \(hair\)")[0] is False,
            f"逐项 = {rejected!r}",
        )
        check(
            "1.0.22 皮肤标签规范：逗号分隔的安全词串合法，大写 / 中文串非法",
            validate_outfit_tag("pink elf dress, fairy wings")[0] is True
            and validate_outfit_tag("Pink Elf Dress")[0] is False
            and validate_outfit_tag("粉色长裙")[0] is False
            and validate_outfit_tag("")[0] is False,
        )

        good_payload = {
            "unknown": False,
            "outfit_cn": "粉色妖精小姐",
            "outfit_en": "Pink Elf Lady",
            "tag": "pink elf dress, fairy wings",
            "keywords": ["pink elf dress", "fairy wings"],
            "confidence": 0.9,
        }
        outfit, reason, confidence = parse_outfit_resolution_payload(good_payload)
        check(
            "1.0.22 解析结果：合法 JSON → 皮肤名 / 关键词 / confidence",
            outfit is not None
            and outfit.name == "粉色妖精小姐"
            and outfit.keywords == ["pink elf dress", "fairy wings"]
            and outfit.ok
            and confidence == 0.9
            and reason == "",
            f"outfit={outfit!r} reason={reason!r}",
        )
        # 非 ASCII / 大写关键词是**逐条丢弃**（不是整条判失败）：剩下能用的照样用
        mixed, mixed_reason, _ = parse_outfit_resolution_payload(
            {
                **good_payload,
                "tag": "",
                "keywords": ["pink elf dress", "粉色长裙", "Bad Tag"],
            }
        )
        check(
            "1.0.22 解析结果：非 ASCII / 大写关键词被逐条丢弃，剩下的照常用",
            mixed is not None
            and mixed.keywords == ["pink elf dress"]
            and mixed_reason == "",
            f"mixed={mixed!r} reason={mixed_reason!r}",
        )
        unknown_outfit, unknown_reason, _ = parse_outfit_resolution_payload(
            {"unknown": True}
        )
        low_outfit, low_reason, low_conf = parse_outfit_resolution_payload(
            {**good_payload, "confidence": 0.2}
        )
        empty_outfit, empty_reason, _ = parse_outfit_resolution_payload(
            {**good_payload, "tag": "", "keywords": ["粉色长裙"]}
        )
        check(
            "1.0.22 解析结果：unknown / 低 confidence / 没有可用关键词都判失败",
            unknown_outfit is None
            and "unknown" in unknown_reason
            and low_outfit is None
            and low_conf == 0.2
            and str(OUTFIT_RESOLVE_MIN_CONFIDENCE) in low_reason
            and empty_outfit is None
            and "关键词" in empty_reason,
            f"unknown={unknown_reason!r} low={low_reason!r} empty={empty_reason!r}",
        )
        check(
            "1.0.22 入口 / 缓存函数存在，成功 TTL 24 小时、失败负缓存 10 分钟",
            callable(reset_outfit_cache)
            and callable(outfit_cache_get)
            and callable(outfit_cache_put)
            and OUTFIT_CACHE_TTL_SECONDS == 24 * 60 * 60.0
            and OUTFIT_FAILURE_TTL_SECONDS == 10 * 60.0
            and hasattr(outfit_handler.CharacterGuardHandler, "_scan_outfit_names")
            and hasattr(outfit_handler.CharacterGuardHandler, "_resolve_outfit")
            and hasattr(
                outfit_handler.CharacterGuardHandler, "_resolve_outfit_with_cache"
            )
            and hasattr(outfit_handler.CharacterGuardHandler, "_apply_outfit")
            # 解析函数在 handler 里是模块级导入（用例打桩要同时替换两处）
            and callable(outfit_handler.resolve_outfit_via_llm),
        )

        # 1.0.23：解析结构多一个**可选**字段 `inferred`（用户没说皮肤名、模型推断时）
        inferred_outfit, inferred_reason, _ = parse_outfit_resolution_payload(
            {**good_payload, "inferred": True}
        )
        legacy_outfit, legacy_reason, _ = parse_outfit_resolution_payload(
            dict(good_payload)
        )
        check(
            "1.0.23 解析结构多一个可选字段 inferred（缺省 false，1.0.22 的结构照旧）",
            inferred_outfit is not None
            and inferred_outfit.inferred is True
            and inferred_reason == ""
            and legacy_outfit is not None
            and legacy_outfit.inferred is False
            and legacy_reason == "",
            f"inferred={getattr(inferred_outfit, 'inferred', None)!r} / "
            f"缺省={getattr(legacy_outfit, 'inferred', None)!r}",
        )

        # 1.0.23：「<角色名>的那一套皮肤/时装」里的角色 —— 只认**角色表内**的角色
        unnamed_roster = parse_roster(
            r"流萤 | 崩坏：星穹铁道 (Honkai: Star Rail) | firefly,流萤 | "
            r"firefly \(honkai: star rail\)"
        )
        check(
            "1.0.23「<角色>的那一套皮肤」识别：表内角色认得出、表外的认不出",
            find_unnamed_outfit_owner("我想看流萤的那一套皮肤～", unnamed_roster)
            == "流萤"
            and find_unnamed_outfit_owner("流萤的皮肤", unnamed_roster) == "流萤"
            and find_unnamed_outfit_owner("我想看刻律德拉的那一套皮肤", unnamed_roster)
            == ""
            and find_unnamed_outfit_owner("画花火吧", unnamed_roster) == "",
        )
        check(
            "1.0.23 推断入口 / 缓存键都在（handler 与 outfit 两处都要能打桩）",
            callable(infer_outfit_via_llm)
            and callable(outfit_handler.infer_outfit_via_llm)
            and outfit_infer_cache_key("流萤") == "流萤|__infer__"
            and hasattr(
                outfit_handler.CharacterGuardHandler, "_infer_unnamed_outfit"
            ),
            f"缓存键={outfit_infer_cache_key('流萤')!r}",
        )

        # 1.0.28：泛指的穿着 —— 内置「穿着类型 → 关键词」表 + 容错匹配 + 模型入口。
        # 完整的端到端行为在 check_logic.py 的 C89~C106，这里只做"装得上、调得动"冒烟。
        wear_hits = {
            word: find_wear_type(f"我想看你的{word}") for word in ("学生", "居家", "泳衣")
        }
        wear_hit_words = {
            word: getattr(match, "word", None) for word, match in wear_hits.items()
        }
        check(
            "1.0.29 内置穿着类型表：至少含 学生/居家/泳衣，且匹配口径是「相等或后缀」"
            "（学生装 / 居家服 / 学生 / 穿学生装 命中；学生会 / 学生时代 不命中）",
            {"学生", "居家", "泳衣"} <= set(WEAR_TYPE_KEYWORDS)
            and all(match is not None for match in wear_hits.values())
            and find_wear_type("学生装") is not None
            and find_wear_type("居家服") is not None
            and find_wear_type("穿学生装") is not None
            # 1.0.29：**本身是键**（相等）也照旧命中（「我想看你的学生」这种后缀写法
            # 已经在 wear_hits 里断言过了）—— 这一条是防"收紧收过头"。
            and find_wear_type("学生") is not None
            # 1.0.29：只有前缀 / 中缀含表中词的话**不再命中**（审计 🟠1 的两条样例），
            # 否则「学生会」「学生时代」会被零模型调用地注入 school uniform。
            and find_wear_type("学生会") is None
            and find_wear_type("学生时代") is None
            and find_wear_type("画花火吧") is None
            and all(
                validate_outfit_keyword(keyword)[0]
                for keywords in WEAR_TYPE_KEYWORDS.values()
                for keyword in keywords
            ),
            f"命中 = {wear_hit_words!r}",
        )
        wedding_match = find_wear_type("婚服")
        student_match = find_wear_type("学生")
        jk_match = find_wear_type("JK")
        school_match = find_wear_type("校服")
        check(
            "1.0.28 同义写法映射到同一组关键词（婚纱 / 婚服 / 结婚礼服 / 新娘装）",
            all(
                find_wear_type(word) is not None
                and find_wear_type(word).keywords  # type: ignore[union-attr]
                == ("wedding dress", "bridal veil", "white dress")
                for word in ("婚纱", "婚服", "结婚礼服", "新娘装")
            )
            and student_match is not None
            and jk_match is not None
            and school_match is not None
            and student_match.keywords == jk_match.keywords == school_match.keywords,
            f"婚服={wedding_match!r} 学生={student_match!r}",
        )
        user_wear = parse_wear_types("学生 | sailor uniform\n女警 | police uniform")
        merged_wear = wear_type_table("学生 | sailor uniform\n女警 | police uniform")
        check(
            "1.0.28 用户补充的穿着类型与内置表合并、同名覆盖内置、非法关键词整行丢弃",
            user_wear
            == {"学生": ("sailor uniform",), "女警": ("police uniform",)}
            and merged_wear["学生"].keywords == ("sailor uniform",)
            and merged_wear["学生"].source == "配置表"
            and merged_wear["居家"].keywords == WEAR_TYPE_KEYWORDS["居家"]
            and merged_wear["居家"].source == "内置表"
            and "女警" in merged_wear
            and parse_wear_types("学生 | 中文关键词") == {}
            and wear_type_table("学生 | 中文关键词")["学生"].keywords
            == WEAR_TYPE_KEYWORDS["学生"],
            f"user={user_wear!r} 学生={merged_wear['学生']!r} 女警={merged_wear.get('女警')!r}",
        )
        check(
            "1.0.28 穿着模型解析入口 / 缓存键 / handler 那一层的方法都在",
            callable(resolve_wear_via_llm)
            and callable(outfit_handler.resolve_wear_via_llm)
            and outfit_wear_cache_key("雨衣") == "雨衣|__wear__"
            and hasattr(
                outfit_handler.CharacterGuardHandler, "_resolve_wear_with_cache"
            )
            and hasattr(outfit_handler.CharacterGuardHandler, "_resolve_outfit")
            and callable(outfit_handler.find_wear_type),
            f"缓存键={outfit_wear_cache_key('雨衣')!r}",
        )
    except Exception as e:  # noqa: BLE001
        check("皮肤/时装模块可调用", False, f"{type(e).__name__}: {e}")

    # ── 5.23 框架运行时兼容（1.0.34）：五种状态 + 静态补丁检测口径 ────────
    # 这里只做「装得上、判得对」的冒烟 —— 完整的安装 / 包装 / 回写行为在
    # check_logic.py 的 C116~C118（那里用离线桩，不真导入宿主模块）。
    # 本节点**不调用 install()**：那会在本进程里真的替换宿主模块的函数，
    # 冒烟脚本不该产生这种副作用。
    try:
        from .. import compat

        statuses = {
            compat.STATUS_DISABLED,
            compat.STATUS_UNSUPPORTED,
            compat.STATUS_ALREADY_PATCHED,
            compat.STATUS_ALREADY_INSTALLED,
            compat.STATUS_APPLIED,
        }
        check(
            "1.0.34 install 的 5 种状态都有定义、入口可调用",
            callable(compat.install) and len(statuses) == 5,
            f"statuses={sorted(statuses)!r}",
        )
        # 插件入口按状态各打一条 INFO / WARNING：五种状态都要有文案、且互不重复
        from ..plugin import _RUNTIME_COMPAT_LOG, _RUNTIME_COMPAT_WARNING

        check(
            "1.0.34 五种状态各有一条日志文案（unsupported 是唯一的 WARNING）",
            set(_RUNTIME_COMPAT_LOG) == statuses
            and _RUNTIME_COMPAT_WARNING == compat.STATUS_UNSUPPORTED
            and len(set(_RUNTIME_COMPAT_LOG.values())) == 5,
            f"WARNING={_RUNTIME_COMPAT_WARNING!r} 文案={sorted(_RUNTIME_COMPAT_LOG)!r}",
        )
        # 三类标记齐了才算「已打补丁」：少任何一类都不能误判（否则会重复发事件）
        patched_src = (
            "event = EventType.BEFORE_ACTION_CALL\n"
            'modified = {"args": kwargs}\n'
            "await bus.publish(event, params)\n"
        )
        check(
            "1.0.34 静态补丁检测：三类标记齐了才判「已打补丁」，缺一类就判「没有」",
            compat.looks_statically_patched(patched_src) is True
            and compat.looks_statically_patched("args = dict(call.args)") is False
            and compat.looks_statically_patched(
                "event = EventType.BEFORE_ACTION_CALL\n"
                'modified = {"args": kwargs}\n'
            )
            is False  # 缺 .publish(
            and compat.looks_statically_patched(
                'modified = {"args": kwargs}\nbws.publish(e, p)\n'
            )
            is False,  # 缺事件名
            f"patched_src={patched_src!r}",
        )
        check(
            "1.0.34 宿主结构常量与实际框架路径一致（不写文件、只读结构）",
            compat.HOST_MODULE == "src.core.utils.llm_tool_call"
            and compat.TARGET_FUNC == "run_tool_call"
            and compat.WRAP_FLAG == "__cpg_runtime_wrapped__",
            f"{compat.HOST_MODULE}.{compat.TARGET_FUNC}",
        )
    except Exception as e:  # noqa: BLE001
        check("框架运行时兼容模块可调用", False, f"{type(e).__name__}: {e}")

    print("-" * 72)
    if _failures:
        print(f"✗ {len(_failures)} 项未通过：" + "、".join(_failures))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
