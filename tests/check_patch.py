r"""框架补丁自检：判断 agent 聊天器路径下的「执行前事件」是否已经补上。

背景：上游在 ``src/core/utils/llm_tool_call.py`` 的 ``create_llm_usable_execution``
里直接 ``_wrap_execute``，不发任何事件；agent 聊天器（default_chatter /
neo_default_chatter）走的正是这条路径，于是依赖「执行前改写参数」的插件
（本插件就是）会**装了毫无反应**。

本脚本只读框架源码，不改任何东西，三种结论：

- ``未打补丁``：静态补丁没打上（**1.0.34 起插件会用「运行时兼容」在内存里补一层**，
  启动日志出现 ``已启用框架运行时兼容（…）`` 就说明已接管；本脚本只看静态补丁这一种方案）；
- ``原版补丁``：事件补上了，但动作名只读旧属性 ``action_name`` → 用新版
  ``name`` 声明的组件（image_generator_plugin-neo 的 draw_image）动作名为空，
  插件侧虽已做参数名兜底，仍建议换成修正版补丁；
- ``修正版补丁``：动作名以 ``name`` 优先解析，两条路径都正常。

用法：

    $env:NEO_ROOT="<你的 Neo-MoFox 框架根>"
    & "$env:NEO_ROOT\.venv\Scripts\python.exe" <插件目录>\tests\check_patch.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001, S110 —— 控制台不支持重配置时忽略即可，输出编码由终端决定
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

TARGET = Path("src") / "core" / "utils" / "llm_tool_call.py"

_failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    """打印一条检查结果。"""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {extra}" if extra else ""))
    if not ok:
        _failures.append(name)
    return ok


def main() -> int:
    """入口。"""
    print(f"框架根   : {NEO_ROOT}")
    print(f"插件目录 : {PLUGIN_DIR}")
    print("-" * 72)

    target = NEO_ROOT / TARGET
    if not check(f"框架文件存在 {TARGET.as_posix()}", target.is_file(), str(target)):
        print("（NEO_ROOT 是否指向 Neo-MoFox 框架根？）")
        return 1

    text = target.read_text(encoding="utf-8", errors="replace")

    # 1. 有没有补发事件
    has_events = "BEFORE_ACTION_CALL" in text and "get_subscribers" in text
    if not has_events:
        check("agent 聊天器路径补发执行前事件", False, "未检测到补丁代码")
        print()
        print(
            "结论：**未打静态补丁** —— agent 聊天器（default_chatter / neo_default_chatter）下"
        )
        print(
            "      上游本身不会发这个事件。**1.0.34 起插件会自己补上**：启动日志里出现"
        )
        print(
            "      `已启用框架运行时兼容（等价 llm_tool_call-pre-events 补丁 v2）`"
            " 就说明运行时兼容已接管，"
        )
        print(
            "      不需要再打补丁；只有那条日志是 `框架版本不支持运行时兼容` 时才需要手工打："
        )
        print(
            "      git apply <交付包>/framework-patch/llm_tool_call-pre-events.patch"
        )
        return 1

    check("agent 聊天器路径补发执行前事件", True)

    # 2. 动作名怎么取的：只看 Action 分支，别被 Tool 分支的 name 误导
    action_branch = ""
    marker = "BEFORE_ACTION_CALL"
    if marker in text:
        snippet = text.split(marker, 1)[1]
        for stop in ("BEFORE_TOOL_CALL", "elif issubclass", "else:"):
            if stop in snippet:
                snippet = snippet.split(stop, 1)[0]
        action_branch = snippet

    resolved_name_first = bool(
        re.search(r"_action_name\s*=\s*str\(", action_branch)
    ) or bool(
        re.search(
            r'getattr\(instance,\s*"name",\s*""\)\s*or\s*getattr\(instance,\s*"action_name"',
            action_branch,
        )
    )
    legacy_only = bool(
        re.search(r'"action_name":\s*getattr\(instance,\s*"action_name"', action_branch)
    )

    if resolved_name_first:
        check("动作名按新版 name 优先解析", True, "修正版补丁")
    elif legacy_only:
        check(
            "动作名按新版 name 优先解析",
            False,
            "检测到原版补丁：Action 分支只读 action_name",
        )
    else:
        check("动作名解析方式可识别", False, "未找到预期的取名字段，请人工确认补丁来源")

    # 3. 订阅者判断（零开销）+ 回写 args
    check("带订阅者判断（无订阅者零开销）", "get_subscribers" in text)
    check("回写插件对 args 的修改", "_new_args" in text or "modified" in text)

    print("-" * 72)
    if _failures:
        print(f"✗ {len(_failures)} 项未通过：{'、'.join(_failures)}")
        if legacy_only:
            print()
            print(
                "提示：原版补丁下，用新版 name 声明动作的组件（如 image_generator_plugin-neo"
            )
            print(
                "      的 draw_image）签名会退化成 `插件:action:`。本插件已做参数名兜底，"
            )
            print("      但仍建议换成修正版补丁（同时修好工具分支与描述字段）。")
        return 1

    print("✓ 框架补丁状态正常（修正版），agent 聊天器路径下本插件可正常接管")
    return 0


if __name__ == "__main__":
    sys.exit(main())
