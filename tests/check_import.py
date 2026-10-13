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
    f"{PLUGIN_NAME}.plugin",
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
    except Exception as e:  # noqa: BLE001
        check("配置模型可实例化", False, f"{type(e).__name__}: {e}")

    print("-" * 72)
    if _failures:
        print(f"✗ {len(_failures)} 项未通过：" + "、".join(_failures))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
