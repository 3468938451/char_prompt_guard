r"""char_prompt_guard 逻辑自检。

在框架环境里真实加载插件、构造 ``before_action_call`` 事件，验证
「检测 → 候选提取 → 注入 → 回写 args」的完整行为，覆盖自然语言模式
（nai_artist）、标签模式（image_generator_plugin-neo）、多角色同框、
运行时标记、自定义目标表等场景。

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
import os
import sys
from pathlib import Path

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
    r"爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | elysia \(honkai impact 3rd\)" "\n"
    r"伊甸 | 崩坏三 (Honkai Impact 3rd) | eden | eden \(honkai impact 3rd\)"
)
RS_ONE = r"爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | elysia \(honkai impact 3rd\)"
TAG_ELYSIA = r"elysia \(honkai impact 3rd\)"
TAG_EDEN = r"eden \(honkai impact 3rd\)"

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
        check("C21 share_photo 补角色（无 mode 参数也不受影响）", False, f"{type(e).__name__}: {e}")

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
        bad = [(p, want, resolve_stream_id(p)) for p, want in cases if resolve_stream_id(p) != want]
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
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS_ONE, use_llm=True)  # 角色表里只有爱莉希雅

        async def fake_extract(**kwargs):
            # 模拟"LLM 读了画面描述与上文后给出的结论"：这次要画的是花火
            return CharacterExtraction(
                characters=[CharacterEntry(name="花火", game="崩坏：星穹铁道")],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            params = {
                "signature": "nai_artist:action:share_photo",
                "action_name": "share_photo",
                "args": {"content": "台上站着一个人，手里拿着面具"},
                "message": FakeMessage("我们来聊爱莉希雅吧"),
                "stream_id": "s-test",
            }
            out = await fire(cfg, params)
            c = out["args"]["content"]
            ok = "花火" in c and "爱莉希雅" not in c
            check("C28 描述结论优先于上文里的旧角色", ok, f"content = {c!r}")
        finally:
            handler_module.extract_characters_via_llm = original
    except Exception as e:  # noqa: BLE001
        check("C28 描述结论优先于上文里的旧角色", False, f"{type(e).__name__}: {e}")

    # ── C29 LLM 说"角色已在提示词里"时不得回退到上文旧角色 ─────────
    # 实测原样复现：描述写着 Hanabi，LLM 也答"花火"，但"花火"已在描述里被过滤，
    # 旧代码于是回退到上文命中，把爱莉希雅补了进去。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS_ONE, use_llm=True)

        async def fake_extract(**kwargs):
            return CharacterExtraction(
                characters=[CharacterEntry(name="花火", game="崩坏：星穹铁道")],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            raw = "Hanabi (Honkai: Star Rail). 只拍花火本尊一人"
            params = {
                "signature": "nai_artist:action:share_photo",
                "action_name": "share_photo",
                "args": {"content": raw},
                "message": FakeMessage("我们来聊爱莉希雅吧"),
                "stream_id": "s-test",
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
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        roster3 = (
            r"爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | elysia \(honkai impact 3rd\)" "\n"
            r"花火 | 崩坏：星穹铁道 (Honkai: Star Rail) | hanabi,花火 | hanabi \(honkai: star rail\)" "\n"
            r"流萤 | 崩坏：星穹铁道 (Honkai: Star Rail) | firefly,流萤 | firefly \(honkai: star rail\)"
        )
        cfg = make_config(roster=roster3, use_llm=True)

        async def fake_extract(**kwargs):
            # LLM 读了描述：这次只有爱莉希雅
            return CharacterExtraction(
                characters=[CharacterEntry(name="爱莉希雅", game="崩坏三 (Honkai Impact 3rd)")],
                multi=False,
            )

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            raw = "爱莉希雅穿着礼服站在灯下"
            params = {
                "signature": "nai_artist:action:share_photo",
                "action_name": "share_photo",
                "args": {"content": raw},
                "message": FakeMessage("聊聊花火和流萤吧"),
                "stream_id": "s-test",
            }
            out = await fire(cfg, params)
            c = out["args"]["content"]
            ok = "花火" not in c and "流萤" not in c and r"elysia \(honkai impact 3rd\)" in c
            check("C32 多人上文不得覆盖已点名的角色", ok, f"content = {c!r}")
        finally:
            handler_module.extract_characters_via_llm = original
    except Exception as e:  # noqa: BLE001
        check("C32 多人上文不得覆盖已点名的角色", False, f"{type(e).__name__}: {e}")

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
