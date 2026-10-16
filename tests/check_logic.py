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

1.0.18 起关联**外部记忆库**（Booku Memory，``[memory]`` 节，C60~C63）：判定
**不确定**时（主题跟踪的 split / unavailable / low_confidence / empty）去检索相关
信息做「再确认」，命中「触发词 → 角色表内角色」的明确对应就直接用作判定依据，
并把这次确认写回记忆库。测试方式是**注入假传输层**（:class:`FakeMemory` 替换
``memory._http_get_json`` / ``memory._http_post_json``，结构与实测的 Booku Memory
一致）：C60 拆单 + 命中「本尊 → 花火」→ 直接补花火（且不再问 LLM）；C61 记忆库超时
→ 不崩、内容与关掉记忆库时逐字一致；C62 ``write_back`` 开 → 恰好一次 POST（标题 /
正文 / core_tags 全对），关 → 零次；C63 ``memory.enabled = false`` → 一次 HTTP 调用
都没有、判定行为同 1.0.17。**C1~C59 的 ``make_config`` 因此默认把记忆库关掉**，
免得本机恰好开着 Booku Memory 时旧用例被影响。

1.0.19 起修一次**真实事故**（C64~C67）：调用方这次画的是**爱莉希雅**（粉长发 /
蓝眼 / 尖耳 / 粉白短裙），却把**花火的标签** ``Hanabi (Honkai: Star Rail)`` 写进了
画面描述 —— 因为它的**备忘**里存着过期的固定规则「标签必须是 Hanabi 挂最前头」。
1.0.18 的判定链一见「描述里有角色表内角色」就放行（描述点名优先级最高，且这种
情况下**根本不查记忆库**），于是没有任何对账。1.0.19 补两件事：

1. 二次一致性检测的对账基准从「判定集合」扩成**期望集合**（判定集合 + 主题跟踪
   主题 + 记忆库确认结果）：提示词里的角色表内角色不在期望集合里 → 冲突，
   ``warn`` 只报不改、``fix`` 仅在「期望集合恰好 1 人 + 画面里冲突角色恰好 1 个」时
   替换（见 C64/C65 与既有 C38/C39）；
2. **描述已点名时**额外做一次「**对账用**」记忆库查询（关键词 = 描述里那个角色的
   名字 / 别名 + 自我指代词；只用于上面那条对账，**不改判定、不写回**）。

C64 描述里写着花火、记忆库明确写着「本尊 → 爱莉希雅」→ ``warn`` 打
``一致性检查=冲突``（两侧角色名与依据都在里面）、**内容一字不改**；
C65 同一场景换成 ``fix`` → 画面里的 ``hanabi`` 被替换成 ``elysia \(honkai impact 3rd\)``；
C66（防回归）记忆库里只有「顺带提到爱莉希雅、但没有触发词对应」的无关条目 →
**不算证据、不报冲突**，内容与关掉记忆库时逐字一致；C67 对账查询超时 → 不崩、
内容不变、日志里能看到 ``记忆库不可用``。

1.0.20 起再补一条**独立**的检查（C68~C71），修的是同一次事故的**另一半**：用户
这次点名的是**角色表之外**的角色（「刻律德拉」，不在角色表里），画面里却挂着
角色表内的花火标签 ``hanabi (honkai: star rail)``（来自调用方过期的备忘规则）。
实测日志里 1.0.19 的三份证据（判定集合 / 主题跟踪 / 记忆库）**全是空的** ——
``记忆库对账查询…status=unavailable``、``判定依据=无(不补)`` —— 于是期望集合为空、
一致性检查**直接跳过**，该报警却没报。1.0.20 因此不看期望集合，改看**用户最近
说过什么**：扫描「触发消息 + 触发前最近 4 条用户消息」（只取用户消息，bot 复述不算），
用引号模式（``「」 『』 《》 “” ""``）与句式模式（``披上X这层壳 / 扮演X / 画X / 要X /
看看X / 换成X / 来张X``）提取**表外**角色名（过滤：长度 / 纯标点 / 常见词黑名单 /
已在角色表内），若最终提示词里出现**用户这次没点过**的角色表内角色即为冲突：

- C68（warn，默认）：内容**一字不变**，WARNING 里写清两侧名字与建议，且此时
  1.0.19 的期望集合对账仍在日志里显示 ``一致性检查=跳过``（两条独立）；
- C69（fix）：**删除**画面里的 ``hanabi \(honkai: star rail\)``（没有表外角色的标准
  标签可替换，只能删），**外观描述原文一字不动**、不留 ``, ,`` / 行首标点等残渣；
- C70（防误报）：用户自己也提过画面里的角色（「画花火吧…」）→ 即使另有表外点名，
  也**不判冲突**；
- C71（防误报）：用户消息里没有任何表外点名（只有「嗯」「行～」）→ 行为与 1.0.19
  完全一致（既有对账怎么跳过的、这次还怎么跳过）。

1.0.21 起补上「删掉之后没有补上正确角色的标签」这半边（C72~C76），起因是用户反馈
「这个角色的提示词没有进去」：1.0.20 的 `fix` 只把画面里那个过期的错误标签**删掉**，
没有补上用户真正点名的角色。1.0.21 新增 `general.auto_resolve_characters`（默认开），
用**已配置的模型**把表外角色解析成「正式名 + 作品名 + NAI 标签」，再走两个入口
（**替换** / **补入**）。这几条用例用假解析函数（monkeypatch
`character.resolve_character_via_llm`，见 :class:`FakeResolve`）跑，不需要真调模型：

- C72：表外点名「刻律德拉」+ 画面含 `hanabi \(honkai: star rail\)` +
  `consistency_check="fix"` + 解析成功 → 画面里的错误标签被**替换**成
  `kleidra \(honkai: star rail\)`，并带上 natural 模式的中文说明；日志里的依据必须
  写明**来源**（`依据=角色解析(LLM):刻律德拉 → Kleidra…`，而不是 `判定集合=…`），
  且解析成功一律额外打一条 `建议加入角色表：…`（只打日志、不改配置文件）；
- C73：解析返回 `unknown=true` → 退化为 1.0.20 的删除行为（不含 hanabi，也不补新标签）；
- C74：解析抛超时 / 异常 → 不崩、同样按 1.0.20 行为处理；
- C75：同一角色解析两次 → 第二次**不再调用模型**（计数器为 1），日志 `角色解析(缓存)`。
  **缓存必须由调用方持有**（`handler._resolve_with_cache`：读 / 写都在被打桩的那一层
  **之外**）—— 缓存留在 `character.resolve_character_via_llm` 里时，桩一装上去缓存就
  一起没了，第二次照样问模型，这条用例正是为此守的；
- C76（防回归）：角色**在角色表内**时**完全不做解析**（调用次数 0），行为同 1.0.20；
  预判不满足只留一条 DEBUG `角色解析=跳过（<原因>）`（没有 `角色解析(LLM)` 成败行、
  也没有 `角色解析(缓存)`）。

1.0.22 起补上**「皮肤 / 时装」这一维**（C77~C82，配置在 `[outfits]` 节），起因是
用户需求：「当说『我想看看你的 xxxx（皮肤）』，可以搜索并加进去。例如
『我想看粉色妖精小姐 / 春婵丽娟』→ 就会出现**爱莉穿着这一套**的衣服；也包括
**流萤的那一套皮肤时装**。」**角色那一侧一个字都没改**（描述点名 > 主题跟踪 >
本尊身份 > 记忆库 > LLM 仲裁 > 不补），新增的只是「这次要穿哪一套」：皮肤名只从
**用户消息**里找（触发消息 + 触发前最近 4 条用户消息，bot 消息跳过，**不扫画面
描述**），句式 + 引号两种模式提取后过五道过滤，命中 `outfits.entries` 的候选优先；
解析走「**表优先 → 模型兜底**」，失败（`unknown` / JSON 坏 / `confidence < 0.5` /
没有可用关键词）时**什么都不注入**。这几条用例用假解析函数（monkeypatch
`outfit.resolve_outfit_via_llm`，见 :class:`FakeOutfit`）跑，不需要真调模型：

- C77：皮肤在 `[outfits]` 表里 + 用户消息「我想看你的粉色妖精小姐」+ 角色判定
  （上文主题跟踪）= 爱莉希雅 → content 同时含 `pink elf dress`、`fairy wings`、
  皮肤名「粉色妖精小姐」与「爱莉希雅」，且**模型调用 0 次**（表命中）；
- C78：表里没有 + `auto_resolve = true` + 假解析返回合法 keywords → 注入成功，
  日志含 `皮肤解析(模型)` 与 `建议加入皮肤表`；
- C79：假解析返回 `unknown=true` → **没有**注入任何皮肤关键词，角色注入照旧
  （内容与关掉 `[outfits]` 时**逐字一致**），日志含 `皮肤解析(LLM)失败`；
- C80（防误报）：用户消息里没有任何皮肤意图（「画花火吧」）→ 不触发皮肤逻辑
  （模型 0 次调用、日志里没有 `皮肤解析`）；
- C81：皮肤 + 角色同时命中 → 角色标签与皮肤关键词**同时存在**且顺序合理
  （角色在前、皮肤在后）；
- C82：`[outfits] enabled=false` → 0 次模型调用、日志无 `皮肤解析`、
  内容与「开着但不触发」时逐字一致。

**C1~C76 的 ``make_config`` 因此默认把 `[outfits]` 关掉**（与 1.0.18 起默认关掉
记忆库同一个理由：旧用例是更早版本的行为回归，不该因为本机恰好配了皮肤表 /
模型而变红；插件配置类里的默认值仍是 `true`，见 tests/check_import.py 的断言）。

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
import urllib.parse
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
    memory: bool = False,
    outfits: str = "",
    outfits_enabled: bool = False,
    outfits_auto_resolve: bool = True,
) -> CharPromptGuardConfig:
    """按用例参数构造配置。

    1.0.18 起 ``[memory]``（外部记忆库）**默认在这里关掉**：C1~C59 是 1.0.17 及
    以前的行为回归，它们不该因为「本机恰好开着 Booku Memory」而变红；需要记忆库
    的用例（C60~C63）显式传 ``memory=True`` 并配 :class:`FakeMemory` 假传输层。
    1.0.19 的 C64~C67 同理（也都是 ``memory=True`` + :class:`FakeMemory`）。
    1.0.22 起 ``[outfits]``（皮肤/时装）**同样默认关掉**：C1~C76 是 1.0.21 及以前的
    行为回归，不该因为本机恰好配了皮肤表或可用模型而变红；需要皮肤功能的用例
    （C77~C82）显式传 ``outfits_enabled=True`` 与 ``outfits=...``。
    （插件配置类里的默认值仍是 ``true`` —— 见 tests/check_import.py 的默认值断言。）
    """
    cfg = CharPromptGuardConfig()
    cfg.general.enabled = general_enabled
    cfg.general.apply_modes = apply_modes
    cfg.general.use_llm = use_llm
    cfg.roster.characters = roster
    cfg.targets.list = targets
    cfg.multi.enabled = multi_enabled
    cfg.multi.max_characters = max_characters
    cfg.memory.enabled = memory
    cfg.outfits.enabled = outfits_enabled
    cfg.outfits.entries = outfits
    cfg.outfits.auto_resolve = outfits_auto_resolve
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


class FakeMemory:
    """上下文管理器：用假传输层替换记忆库的 HTTP 调用（1.0.18）。

    插件侧唯一需要替换的就是 ``memory._http_get_json`` / ``memory._http_post_json``
    这两个模块级函数（:meth:`memory.MemoryClient.search` / ``write`` 都把它们包在
    ``asyncio.to_thread`` 里执行），所以这里 monkeypatch 掉它们，并记录每一次调用：

    - ``get`` / ``post`` 是调用记录（URL 字符串），用例据此断言
      「发了几次请求 / 带了哪些关键词 / 写回时的 JSON 体是什么」；
    - ``items`` 是 ``GET /api/memories`` 的返回条目（结构对齐实测的 Booku Memory：
      ``{"id", "title", "content_snippet", "metadata": {...}}``）；
    - ``error`` 非空时两个传输函数都抛 ``TimeoutError``（超时的替身），
      用来验「记忆库不可用 → 静默降级」；
    - ``post_error`` 单独控制写入失败。

    退出 with 块时恢复原函数，并把「失败退避」状态清干净（``memory.reset_state``），
    免得一条用例的失败退避影响下一条。
    """

    def __init__(
        self,
        items: list[dict] | None = None,
        *,
        error: BaseException | None = None,
        post_error: BaseException | None = None,
        post_response: dict | None = None,
    ) -> None:
        self.items = list(items or [])
        self.error = error
        self.post_error = post_error
        self.post_response = (
            post_response if post_response is not None else {"id": "mem-new-1"}
        )
        self.get: list[str] = []
        self.post: list[tuple[str, dict]] = []
        self._original_get = None
        self._original_post = None

    def __enter__(self) -> Self:
        from .. import memory as memory_module

        self._original_get = memory_module._http_get_json
        self._original_post = memory_module._http_post_json
        memory_module.reset_state()

        def fake_get(url: str, timeout: float):
            self.get.append(url)
            if self.error is not None:
                raise self.error
            return {
                "action": "list_memory_entries",
                "total": len(self.items),
                "items": self.items,
            }

        def fake_post(url: str, payload: dict, timeout: float):
            self.post.append((url, dict(payload)))
            if self.post_error is not None:
                raise self.post_error
            return dict(self.post_response)

        memory_module._http_get_json = fake_get
        memory_module._http_post_json = fake_post
        return self

    def __exit__(self, *exc_info) -> bool:
        from .. import memory as memory_module

        if self._original_get is not None:
            memory_module._http_get_json = self._original_get
        if self._original_post is not None:
            memory_module._http_post_json = self._original_post
        memory_module.reset_state()
        return False

    def keyword(self) -> str:
        """最近一次 GET 里带的 ``keyword`` 参数（没发过请求时为空串）。"""
        if not self.get:
            return ""
        query = urllib.parse.urlsplit(self.get[-1]).query
        return urllib.parse.parse_qs(query).get("keyword", [""])[0]


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

    def records_text(self) -> str:
        """全部级别的记录（1.0.18 的「记忆库已记住 / 记忆库确认」是 INFO）。"""
        return "\n".join(record.getMessage() for record in self.handler.records)


async def fire(cfg: CharPromptGuardConfig, params: dict) -> dict:
    """触发一次 before_action_call，返回（可能被回写的）params。"""
    handler = CharacterGuardHandler(FakePlugin(cfg))
    decision, out = await handler.execute("before_action_call", copy.deepcopy(params))
    if decision is not EventDecision.SUCCESS:
        raise AssertionError(f"decision 应为 SUCCESS，实际 {decision}")
    return out


class FakeResolve:
    """上下文管理器：替换角色解析函数，模拟（或统计）模型调用（1.0.21）。

    插件里唯一需要替换的是 ``character.resolve_character_via_llm``
    （``handler`` 在模块级 ``from .character import resolve_character_via_llm``，
    所以两个位置都要替换），因此用例不必起 LLM 运行时、也不会真的发请求：

    - ``payload`` 是模型「返回」的 JSON 字典 —— 用
      ``character.parse_resolution_payload`` 走**与插件完全同一条**校验链，
      于是「非法标签 / unknown / confidence 太低」这些分支都被真实覆盖；
    - ``error`` 非空时抛该异常（超时 / 网络故障的替身）；
    - ``calls`` 记录每一次调用（原始角色名），用例据此断言调用次数。

    退出 with 块时恢复原函数，并清掉解析缓存 —— 免得上一条用例的缓存把
    下一条用例的「该不该问模型」判断带偏。
    """

    def __init__(
        self,
        payload: dict | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {"unknown": True}
        self.error = error
        self.calls: list[str] = []
        self._original_character = None
        self._original_handler = None

    async def __call__(self, name: str, **kwargs):
        """假解析函数：记录调用，再按 payload / error 返回。"""
        from ..character import ResolvedCharacter, parse_resolution_payload

        self.calls.append(name)
        if self.error is not None:
            raise self.error
        entry, reason, confidence = parse_resolution_payload(self.payload)
        if entry is None:
            return ResolvedCharacter(
                reason=reason, confidence=confidence, raw_name=name
            )
        return ResolvedCharacter(
            entry=entry,
            source="模型",
            confidence=confidence,
            raw_name=name,
        )

    def __enter__(self) -> Self:
        from .. import character as character_module
        from .. import handler as handler_module

        self._original_character = character_module.resolve_character_via_llm
        self._original_handler = handler_module.resolve_character_via_llm
        character_module.resolve_character_via_llm = self  # type: ignore[assignment]
        handler_module.resolve_character_via_llm = self  # type: ignore[assignment]
        # 清缓存放在**替换之后**：这里清的是上一条用例留下的结果，
        # 免得它把本条的「该不该问模型」判断带偏。
        character_module.reset_resolve_cache()
        return self

    def __exit__(self, *exc_info) -> bool:
        from .. import character as character_module
        from .. import handler as handler_module

        if self._original_character is not None:
            character_module.resolve_character_via_llm = self._original_character
        if self._original_handler is not None:
            handler_module.resolve_character_via_llm = self._original_handler
        character_module.reset_resolve_cache()
        return False


#: 表外角色「刻律德拉」的假解析结果（与需求里的字段一一对应）
KLEIDRA_PAYLOAD = {
    "unknown": False,
    "name_cn": "刻律德拉",
    "name_en": "Kleidra",
    "game_cn": "崩坏：星穹铁道 (Honkai: Star Rail)",
    "game_en": "Honkai: Star Rail",
    "tag": r"kleidra \(honkai: star rail\)",
    "confidence": 0.9,
}
TAG_KLEIDRA = r"kleidra \(honkai: star rail\)"
#: 「刻律德拉」场景的画面描述（与 C68/C69 同一段外观描述）
KLEIDRA_SCENE = (
    "Hanabi (Honkai: Star Rail) 银白色长发的少女站在镜头前，"
    "头上戴着蓝白饰冠，一身蓝裙，手里握着长杖"
)


class FakeOutfit:
    """上下文管理器：替换皮肤解析函数，模拟（或统计）模型调用（1.0.22）。

    与 :class:`FakeResolve` 同一套做法：插件里唯一需要替换的是
    ``outfit.resolve_outfit_via_llm``（``handler`` 在模块级
    ``from .outfit import resolve_outfit_via_llm``，所以两个位置都要替换），
    因此用例不必起 LLM 运行时、也不会真的发请求：

    - ``payload`` 是模型「返回」的 JSON 字典 —— 用
      ``outfit.parse_outfit_resolution_payload`` 走**与插件完全同一条**校验链，
      于是「unknown / confidence 太低 / 关键词非 ASCII」这些分支都被真实覆盖；
    - ``error`` 非空时抛该异常（超时 / 网络故障的替身）；
    - ``calls`` 记录每一次调用（皮肤名），用例据此断言调用次数
      （C77 表命中、C80 无皮肤意图、C82 开关关掉时都必须是 **0 次**）。

    退出 with 块时恢复原函数，并清掉解析缓存 —— 免得上一条用例的缓存把下一条
    用例的「该不该问模型」判断带偏。
    """

    def __init__(
        self,
        payload: dict | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {"unknown": True}
        self.error = error
        self.calls: list[str] = []
        self._original_outfit = None
        self._original_handler = None

    async def __call__(self, name: str, *, character: str = "", **kwargs):
        """假解析函数：记录调用，再按 payload / error 返回。"""
        from ..outfit import ResolvedOutfit, parse_outfit_resolution_payload

        self.calls.append(name)
        if self.error is not None:
            raise self.error
        outfit, reason, confidence = parse_outfit_resolution_payload(self.payload)
        if outfit is None:
            return ResolvedOutfit(
                name=name,
                character=character,
                reason=reason,
                confidence=confidence,
                raw_name=name,
            )
        return ResolvedOutfit(
            name=outfit.name or name,
            character=outfit.character or character,
            keywords=outfit.keywords,
            description=outfit.description,
            source="模型",
            confidence=confidence,
            raw_name=name,
        )

    def __enter__(self) -> Self:
        from .. import handler as handler_module
        from .. import outfit as outfit_module

        self._original_outfit = outfit_module.resolve_outfit_via_llm
        self._original_handler = handler_module.resolve_outfit_via_llm
        outfit_module.resolve_outfit_via_llm = self  # type: ignore[assignment]
        handler_module.resolve_outfit_via_llm = self  # type: ignore[assignment]
        # 清缓存放在**替换之后**：这里清的是上一条用例留下的结果
        outfit_module.reset_outfit_cache()
        return self

    def __exit__(self, *exc_info) -> bool:
        from .. import handler as handler_module
        from .. import outfit as outfit_module

        if self._original_outfit is not None:
            outfit_module.resolve_outfit_via_llm = self._original_outfit
        if self._original_handler is not None:
            handler_module.resolve_outfit_via_llm = self._original_handler
        outfit_module.reset_outfit_cache()
        return False


#: 示例皮肤表（1.0.22）：`皮肤名 | 角色名 | 英文关键词 | 中文描述`
RS_OUTFIT = (
    "粉色妖精小姐 | 爱莉希雅 | pink elf dress, fairy wings | 爱莉希雅的粉色妖精装"
)
#: 表外皮肤的假解析结果（与需求里的字段一一对应）
OUTFIT_PAYLOAD = {
    "unknown": False,
    "outfit_cn": "粉色妖精小姐",
    "outfit_en": "Pink Elf Lady",
    "tag": "pink elf dress, fairy wings",
    "keywords": ["pink elf dress", "fairy wings"],
    "confidence": 0.9,
}
#: 期望出现在提示词里的英文关键词
PINK_KEYWORDS = ("pink elf dress", "fairy wings")
#: 「我想看你这套皮肤」这句话（用户需求里的原话）
PINK_REQUEST = "我想看你的粉色妖精小姐"
#: 皮肤用例共用的画面描述（不含任何角色，角色由上文决定）
PINK_SCENE = "帮我画一张她战斗的样子"


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
    # 1.0.21：先清掉角色解析的进程内缓存 —— 否则同一次进程里重跑时，
    # C68/C69 这类**没配假解析函数**的用例会（按 10 分钟负缓存）跳过模型调用，
    # 缓存状态就会影响「该不该问模型」的断言。
    # 1.0.22：皮肤解析缓存同理（C77~C82 断言的都是"问了几次模型"）。
    from ..character import reset_resolve_cache
    from ..outfit import reset_outfit_cache

    reset_resolve_cache()
    reset_outfit_cache()
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

    # ── C60 记忆库再确认：不确定（拆单）+ 命中「本尊 → 花火」→ 直接补花火（1.0.18）──
    # 与 C54 完全同一个「拆单」场景（同一条消息里既有「本尊」又有「爱莉」），
    # 区别只在**本机记忆库里已经记过这件事**：检索命中一条标题含「本尊 → 花火」的
    # 记忆 → 判定链直接采用（日志 `记忆库确认:本尊 → 花火（mem-xxxx）`），
    # 不必再问 LLM（下面是调用计数器，断言 LLM 一次都没被调用）。
    try:
        from .. import handler as handler_module
        from ..character import CharacterExtraction

        cfg = make_config(roster=RS3, use_llm=True, memory=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.description_first = False
        cfg.multi.llm_mode = "fallback"
        raw = "清晨室内明亮柔和的光线。白发少女刚起床，站在镜头前，粉色眼睛看向镜头"
        calls = {"n": 0}

        async def fake_extract(**kwargs):
            calls["n"] += 1
            return CharacterExtraction(characters=[], multi=False)

        memory_items = [
            {
                "id": "mem-hanabi-1",
                "title": "角色指向：本尊 → 花火",
                "content_snippet": "用户以「本尊」指向角色表里的「花火」。",
                "metadata": {
                    "bucket": "memory",
                    "memory_type": "knowledge",
                    "status": "active",
                },
            }
        ]
        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            history = [history_message("本尊看看，然后穿着爱莉来看看～", 1.0)]
            with (
                FakeMemory(memory_items) as mem,
                FakeHistory(history),
                LogCapture() as logs,
            ):
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_photo",
                        "action_name": "share_photo",
                        "args": {"content": raw},
                        "message": FakeMessage("嗯～"),
                        "stream_id": "s-c60",
                    },
                )
        finally:
            handler_module.extract_characters_via_llm = original
        c = out["args"]["content"]
        logs_text = logs.debugs() + "\n" + logs.records_text()
        keyword = mem.keyword()
        ok = (
            "花火" in c
            and "爱莉希雅" not in c
            and c.startswith(raw)
            and "记忆库" in logs_text
            and "mem-hanabi-1" in logs_text
            and calls["n"] == 0  # 记忆库给了明确对应，不必再问 LLM
            and len(mem.get) == 1  # 只检索一次
            and "本尊" in keyword  # 关键词里带上了触发词
            and len(keyword) <= 60  # 关键词长度受控
            and len(mem.post) == 1  # 命中即写回（write_back 默认开）
        )
        check(
            "C60 记忆库再确认（拆单 + 命中「本尊 → 花火」）→ 直接补花火",
            ok,
            f"content = {c!r}\n        keyword = {keyword!r}\n"
            f"        GET = {mem.get!r}\n        POST = {mem.post!r}\n"
            f"        LLM 调用次数 = {calls['n']}\n        日志 = {logs_text!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C60 记忆库再确认（拆单 + 命中「本尊 → 花火」）→ 直接补花火",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C61 记忆库不可用（超时/异常）→ 静默降级回 1.0.17 原路径（1.0.18）──────
    # 与 C54 同一个场景、同一个假 LLM 结论，只是把记忆库换成「一请求就超时」：
    # 必须不崩、按 1.0.17 的拆单链路（LLM 按画面判定）补出花火，并且**内容与
    # 关闭记忆库时逐字一致**；日志里要能看出是记忆库不可用被跳过。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        async def fake_extract(**kwargs):
            return CharacterExtraction(
                characters=[
                    CharacterEntry(
                        name="花火", game="崩坏：星穹铁道 (Honkai: Star Rail)"
                    )
                ],
                multi=False,
            )

        def scenario_cfg(memory: bool) -> CharPromptGuardConfig:
            cfg = make_config(roster=RS3, use_llm=True, memory=memory)
            cfg.general.history_cache_ttl = 0.0
            cfg.general.description_first = False
            cfg.multi.llm_mode = "fallback"
            return cfg

        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {
                "content": "清晨室内明亮柔和的光线。白发少女刚起床，站在镜头前，粉色眼睛看向镜头"
            },
            "message": FakeMessage("嗯～"),
            "stream_id": "s-c61",
        }
        history = [history_message("本尊看看，然后穿着爱莉来看看～", 1.0)]

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            # 基线：完全关掉记忆库（1.0.17 的行为）
            with FakeHistory(history):
                gold = (await fire(scenario_cfg(False), params))["args"]["content"]
            # 记忆库开着但一请求就超时
            with (
                FakeMemory(error=TimeoutError("timed out")) as mem,
                FakeHistory(history),
                LogCapture() as logs,
            ):
                out = await fire(scenario_cfg(True), params)
        finally:
            handler_module.extract_characters_via_llm = original
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            c == gold
            and "花火" in c
            and "记忆库不可用" in debugs
            and "拆单" in debugs
            and len(mem.get) == 1
            and not mem.post  # 没命中就不该写回
        )
        check(
            "C61 记忆库不可用 → 不崩、静默降级（内容与 1.0.17 一致）",
            ok,
            f"gold  = {gold!r}\n        content = {c!r}\n"
            f"        GET = {mem.get!r} POST = {mem.post!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C61 记忆库不可用 → 不崩、静默降级（内容与 1.0.17 一致）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C62 写回（记住）：write_back 开 → 恰好一次 POST；关 → 一次都没有（1.0.18）──
    # 写回内容按需求钉死：title 含触发词与角色名、content 含角色与标准标签、
    # core_tags 同时含「角色指向」/ 触发词 / 角色名；bucket 与 memory_type 用配置值。
    try:
        from .. import handler as handler_module
        from ..character import CharacterExtraction

        memory_items = [
            {
                "id": "mem-hanabi-2",
                "title": "角色指向：本尊 → 花火",
                "content_snippet": "用户以「本尊」指向角色表里的「花火」（崩坏：星穹铁道）。",
                "metadata": {"bucket": "memory", "memory_type": "knowledge"},
            }
        ]

        async def fake_extract(**kwargs):
            return CharacterExtraction(characters=[], multi=False)

        def scenario_cfg(write_back: bool) -> CharPromptGuardConfig:
            cfg = make_config(roster=RS3, use_llm=True, memory=True)
            cfg.general.history_cache_ttl = 0.0
            cfg.general.description_first = False
            cfg.multi.llm_mode = "fallback"
            cfg.memory.write_back = write_back
            cfg.memory.bucket = "memory"
            cfg.memory.memory_type = "knowledge"
            return cfg

        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": "清晨室内明亮柔和的光线。白发少女刚起床"},
            "message": FakeMessage("嗯～"),
            "stream_id": "s-c62",
        }
        history = [history_message("本尊看看，然后穿着爱莉来看看～", 1.0)]

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            with (
                FakeMemory(memory_items) as on,
                FakeHistory(history),
                LogCapture() as logs,
            ):
                out_on = await fire(scenario_cfg(True), params)
            with FakeMemory(memory_items) as off, FakeHistory(history):
                out_off = await fire(scenario_cfg(False), params)
        finally:
            handler_module.extract_characters_via_llm = original

        body = on.post[0][1] if on.post else {}
        tags = body.get("core_tags") or []
        ok = (
            len(on.post) == 1
            and on.post[0][0].endswith("/api/memories")
            and "本尊" in str(body.get("title") or "")
            and "花火" in str(body.get("title") or "")
            and "花火" in str(body.get("content") or "")
            and TAG_HANABI in str(body.get("content") or "")
            and "角色指向" in tags
            and "本尊" in tags
            and "花火" in tags
            and body.get("bucket") == "memory"
            and body.get("memory_type") == "knowledge"
            and body.get("status") == "active"
            and "记忆库已记住" in logs.records_text()
            and not off.post  # write_back=False：一次 POST 都没有
            and "花火" in out_on["args"]["content"]
            and "花火" in out_off["args"]["content"]  # 开关只影响写回，不影响判定
        )
        check(
            "C62 写回记忆库：write_back 开→一次 POST（含标题/正文/标签），关→零次",
            ok,
            f"POST = {on.post!r}\n        write_back=False 的 POST = {off.post!r}\n"
            f"        content = {body.get('content')!r}\n"
            f"        日志 = {logs.records_text()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C62 写回记忆库：write_back 开→一次 POST（含标题/正文/标签），关→零次",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C63 memory.enabled=False → 完全没有 HTTP 调用，判定同 1.0.17（1.0.18）──
    # 三个层面一起断言：
    #   ① 假传输层（GET/POST）一次都没被调用；
    #   ② 内容与本项目 1.0.17 的实现（同一场景用假 LLM 跑）逐字一致；
    #   ③ 顺手钉住「记忆库里的词表没有和判定链漂移」（memory 只加不减）。
    try:
        from .. import handler as handler_module
        from .. import memory as memory_module
        from ..character import (
            SELF_REFERENCE_WORDS,
            CharacterEntry,
            CharacterExtraction,
        )

        async def fake_extract(**kwargs):
            return CharacterExtraction(
                characters=[
                    CharacterEntry(
                        name="花火", game="崩坏：星穹铁道 (Honkai: Star Rail)"
                    )
                ],
                multi=False,
            )

        # ① 关掉记忆库时，连一次请求都不该发出去 —— FakeMemory 里配了 error，
        #    插件若真的发请求，这条用例会以异常的形式直接失败。
        def scenario_cfg(memory: bool) -> CharPromptGuardConfig:
            cfg = make_config(roster=RS3, use_llm=True, memory=memory)
            cfg.general.history_cache_ttl = 0.0
            cfg.general.description_first = False
            cfg.multi.llm_mode = "fallback"
            return cfg

        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": "清晨室内明亮柔和的光线。白发少女刚起床"},
            "message": FakeMessage("嗯～"),
            "stream_id": "s-c63",
        }
        history = [history_message("本尊看看，然后穿着爱莉来看看～", 1.0)]

        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            # 记忆库关闭：零请求（FakeMemory 的 error 就是哨兵），日志里也不该出现「记忆库」
            with (
                FakeMemory(error=AssertionError("不该被调用")) as mem,
                FakeHistory(history),
                LogCapture() as logs,
            ):
                out = await fire(scenario_cfg(False), params)
            # ② 对照组：同一个场景、记忆库开着但一请求就超时 —— 内容必须逐字一致
            with (
                FakeMemory(error=TimeoutError("timed out")) as mem_on,
                FakeHistory(history),
                LogCapture() as logs_on,
            ):
                out_on = await fire(scenario_cfg(True), params)
        finally:
            handler_module.extract_characters_via_llm = original

        c = out["args"]["content"]
        # 词表漂移检查：memory 模块刻意不 import character / monitor（避免循环依赖），
        # 自我指代词表是与它们各抄一份的 —— 这条断言保证「只加不减、顺序一致」。
        drift = [
            word
            for word in SELF_REFERENCE_WORDS
            if word not in memory_module.SPEAKER_TRIGGER_WORDS
        ]
        head_mismatch = [
            (index, word)
            for index, word in enumerate(SELF_REFERENCE_WORDS)
            if index >= len(memory_module.SPEAKER_TRIGGER_WORDS)
            or memory_module.SPEAKER_TRIGGER_WORDS[index] != word
        ]
        ok = (
            not mem.get
            and not mem.post
            and "记忆库" not in logs.records_text()
            and "花火" in c
            and "本尊" not in c
            and "拆单" in logs.debugs()
            and out_on["args"]["content"] == c  # 不可用时逐字退回原路径
            and "记忆库不可用" in logs_on.debugs()
            and len(mem_on.get) == 1
            and not drift
            and not head_mismatch
            and "本尊" in memory_module.SPEAKER_TRIGGER_WORDS
        )
        check(
            "C63 memory.enabled=False → 零 HTTP 调用、判定同 1.0.17",
            ok,
            f"GET = {mem.get!r} POST = {mem.post!r}\n        content = {c!r}\n"
            f"        对照组（记忆库超时）content = {out_on['args']['content']!r}\n"
            f"        漂移的自我指代词 = {drift!r} 顺序不符 = {head_mismatch!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C63 memory.enabled=False → 零 HTTP 调用、判定同 1.0.17",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C64 描述点名 vs 记忆库对账：warn 只报不改（1.0.19）──────────────────
    # 真实事故原文：调用方这次画的是**爱莉希雅**（粉长发 / 蓝眼 / 尖耳 / 粉白短裙），
    # 写进画面描述的却是它**过期备忘**里的固定标签 `Hanabi (Honkai: Star Rail)`。
    # 上下文（触发前那句「…你找找有没有那种有亲切感的壳子啊…」）里只有代词「你」、
    # 没有先行词 → 主题跟踪 status=unavailable、判定集合为空；1.0.18 一见描述里有
    # 角色表内角色就放行，连记忆库都不查 —— 于是没有任何对账。
    # 1.0.19：描述点名时额外做一次**对账用**查询，记忆里明确写着「本尊 → 爱莉希雅」
    # → 期望集合 = {爱莉希雅}，画面里的「花火」落在期望集合之外 → warn 记一条
    # `一致性检查=冲突`（两侧角色名与依据都在里面），**内容一字不改**；
    # 这次查询只用于对账，**绝不写回**（POST 次数必须是 0）。
    try:
        cfg = make_config(roster=RS3, memory=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "warn"
        raw = "Hanabi (Honkai: Star Rail) 粉长发、尖耳朵、粉白短裙的少女"
        memory_items = [
            {
                "id": "mem-elysia-1",
                "title": "角色指向：本尊 → 爱莉希雅",
                "content_snippet": (
                    "用户想要「有亲切感的壳子」，本尊指向角色表里的「爱莉希雅」"
                    "（崩坏三 (Honkai Impact 3rd)）。"
                ),
                "metadata": {
                    "bucket": "memory",
                    "memory_type": "knowledge",
                    "status": "active",
                },
            }
        ]
        history = [
            history_message("唔，你找找有没有那种有亲切感的壳子啊，穿着拍一张～", 1.0)
        ]
        with (
            FakeMemory(memory_items) as mem,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("行～"),
                    "stream_id": "s-c64",
                },
            )
        c = out["args"]["content"]
        warns = logs.warnings()
        ok = (
            c == raw  # warn 不改内容
            and "一致性检查=冲突" in warns
            and "花火" in warns  # 画面那一侧
            and "hanabi" in warns  # …连标准标签一起写出来
            and "爱莉希雅" in warns  # 上下文 / 记忆那一侧
            and "记忆库确认" in warns  # 依据写清是记忆库确认
            and "无先行词" in warns  # 判定依据（主题跟踪不可用）也在
            and len(mem.get) == 1  # 只做一次对账查询
            and not mem.post  # 对账查询绝不写回
        )
        check(
            "C64 描述点名 vs 记忆库对账：warn 报冲突、内容不改",
            ok,
            f"content = {c!r}\n        WARNING = {warns!r}\n"
            f"        GET = {mem.get!r}\n        POST = {mem.post!r}\n"
            f"        日志 = {logs.records_text()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C64 描述点名 vs 记忆库对账：warn 报冲突、内容不改",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C65 同一场景 consistency_check=fix：把画面里的错误角色换成期望角色（1.0.19）──
    # 期望集合恰好 1 人（爱莉希雅）、画面里冲突的角色恰好 1 个（花火）→ 唯一可定位，
    # 走 replace_character_reference：`Hanabi` → `爱莉希雅`，并按 tag_completion
    # 补上期望角色的标准标签 `elysia \(honkai impact 3rd\)`。
    try:
        cfg = make_config(roster=RS3, memory=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        raw = "Hanabi (Honkai: Star Rail) 粉长发、尖耳朵、粉白短裙的少女"
        memory_items = [
            {
                "id": "mem-elysia-1",
                "title": "角色指向：本尊 → 爱莉希雅",
                "content_snippet": (
                    "用户想要「有亲切感的壳子」，本尊指向角色表里的「爱莉希雅」。"
                ),
                "metadata": {"bucket": "memory", "memory_type": "knowledge"},
            }
        ]
        history = [
            history_message("唔，你找找有没有那种有亲切感的壳子啊，穿着拍一张～", 1.0)
        ]
        with (
            FakeMemory(memory_items) as mem,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("行～"),
                    "stream_id": "s-c65",
                },
            )
        c = out["args"]["content"]
        warns = logs.warnings()
        ok = (
            "hanabi" not in c.lower()  # 错的标签没了
            and "花火" not in c
            and ("elysia" in c.lower() or "爱莉希雅" in c)  # 换成期望角色
            and "一致性检查=冲突已修正" in warns
            and "爱莉希雅" in warns
            and len(mem.get) == 1
            and not mem.post  # 对账查询绝不写回
        )
        check(
            "C65 同场景 fix：画面里的花火被替换为爱莉希雅",
            ok,
            f"content = {c!r}\n        WARNING = {warns!r}\n        POST = {mem.post!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C65 同场景 fix：画面里的花火被替换为爱莉希雅",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C66（防回归）描述点名 + 记忆库没有冲突信息 → 不算证据、不报冲突（1.0.19）──
    # 记忆库里只有一条**顺带提到**爱莉希雅、却没有「触发词 → 角色」明确对应的闲聊
    # （没有 本尊 / 扮演 / 口吻 这类触发词）→ attribute_items 判 miss → 期望集合为空
    # → 一致性检查跳过。内容必须与「关掉记忆库」（= 1.0.18 行为）**逐字一致**。
    # 这条守的是 1.0.19 最容易踩的坑：把模糊检索结果当成「该画她」的依据，
    # 于是把正常的描述点名误判成冲突。
    try:
        raw = "Hanabi (Honkai: Star Rail) 粉长发、尖耳朵、粉白短裙的少女"
        history = [
            history_message("唔，你找找有没有那种有亲切感的壳子啊，穿着拍一张～", 1.0)
        ]
        unrelated_items = [
            {
                "id": "mem-chat-1",
                "title": "日常闲聊",
                "content_snippet": "聊到过爱莉希雅，用户说想要有亲切感的壳子。",
                "metadata": {"bucket": "memory", "memory_type": "knowledge"},
            }
        ]

        def scenario_cfg(memory: bool) -> CharPromptGuardConfig:
            cfg = make_config(roster=RS3, memory=memory)
            cfg.general.history_cache_ttl = 0.0
            cfg.general.consistency_check = "warn"
            return cfg

        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": raw},
            "message": FakeMessage("行～"),
            "stream_id": "s-c66",
        }
        with FakeHistory(history):
            gold = (await fire(scenario_cfg(False), params))["args"]["content"]
        with (
            FakeMemory(unrelated_items) as mem,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(scenario_cfg(True), params)
        c = out["args"]["content"]
        all_text = logs.records_text()
        ok = (
            c == raw
            and c == gold  # 与关掉记忆库（1.0.18 行为）逐字一致
            and "一致性检查=冲突" not in all_text
            and "一致性检查=跳过" in logs.debugs()  # 没有可对账的期望集合
            and len(mem.get) == 1  # 查询照发（关键词抓的是画面里那个角色）
            and not mem.post
        )
        check(
            "C66 描述点名 + 记忆库无明确对应 → 不误报冲突（与 1.0.18 逐字一致）",
            ok,
            f"content = {c!r}\n        gold = {gold!r}\n"
            f"        GET = {mem.get!r}\n        日志 = {all_text!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C66 描述点名 + 记忆库无明确对应 → 不误报冲突（与 1.0.18 逐字一致）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C67 描述点名 + 记忆库查询超时 → 不崩、内容不变、日志说明不可用（1.0.19）──
    try:
        cfg = make_config(roster=RS3, memory=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "warn"
        raw = "Hanabi (Honkai: Star Rail) 粉长发、尖耳朵、粉白短裙的少女"
        history = [
            history_message("唔，你找找有没有那种有亲切感的壳子啊，穿着拍一张～", 1.0)
        ]
        with (
            FakeMemory(error=TimeoutError("timed out")) as mem,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("行～"),
                    "stream_id": "s-c67",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            c == raw  # 不崩、内容不变
            and "记忆库不可用" in debugs
            and "一致性检查=冲突" not in logs.records_text()
            and len(mem.get) == 1  # 只试一次，失败即静默降级
            and not mem.post
        )
        check(
            "C67 对账查询超时 → 不崩、内容不变、静默降级",
            ok,
            f"content = {c!r}\n        GET = {mem.get!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C67 对账查询超时 → 不崩、内容不变、静默降级",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C68 用户点名了角色表外的角色 vs 画面里的表内角色：warn 只报不改（1.0.20）──
    # 实测日志（1.0.20 要修的那次事故）：
    #   22:59:43 用户：这身是「刻律德拉」？                      ← 点名的是**表外**角色
    #   22:59:51 nai_artist tags：… hanabi (honkai: star rail), …, blue eyes,
    #                              silver hair, blue dress, …, staff …
    #   22:59:49 记忆库对账查询（画面点名=花火）: status=unavailable
    #            主题跟踪: … ← 不可用；判定依据=无(不补)
    #            提示词已包含角色，放行
    #            一致性检查=跳过（没有可对账的期望集合…）        ← ★ 该报警却没报
    # 1.0.19 的三份证据全空（判定集合空、主题跟踪不可用、记忆库不可用）→ 期望集合为空
    # → 既有对账**照旧跳过**（这条用例会断言日志里仍有那一行）；1.0.20 的独立检查
    # 不看期望集合，只看「用户最近说过什么」：表外点名「刻律德拉」+ 画面里是
    # 用户**这次没点过**的「花火」→ warn 只打 WARNING，内容一字不改。
    try:
        cfg = make_config(roster=RS3, memory=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "warn"
        raw = (
            "Hanabi (Honkai: Star Rail) 银白色长发的少女站在镜头前，"
            "头上戴着蓝白饰冠，一身蓝裙，手里握着长杖"
        )
        history = [history_message("这身是「刻律德拉」？", 1.0)]
        with (
            FakeMemory(error=TimeoutError("timed out")) as mem,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("嗯"),
                    "stream_id": "s-c68",
                },
            )
        c = out["args"]["content"]
        warns = logs.warnings()
        debugs = logs.debugs()
        ok = (
            c == raw  # warn 一字不改
            and "一致性检查=冲突（warn 不改内容）" in warns
            and "用户点名「刻律德拉」" in warns  # 用户点名那一侧
            and "不在角色表中" in warns
            and "刻律德拉" in warns
            and ("花火" in warns and "hanabi" in warns)  # 画面那一侧（名字 + 标准标签）
            and "建议：不要用备忘里的旧标签，只写画面描述" in warns
            and warns.count("一致性检查=冲突") == 1  # 只有这条独立检查报了冲突
            and "冲突已修正" not in logs.records_text()  # warn 不做任何修正
            and "一致性检查=跳过" in debugs  # 1.0.19 的期望集合对账照旧跳过（两条独立）
            and "表外点名" in debugs  # 新检查确实跑了
            and len(mem.get) == 1  # 对账用的记忆库查询照发（这里超时）
            and not mem.post  # 但它绝不写回
        )
        check(
            "C68 表外点名 vs 画面内表内角色：warn 报冲突、内容一字不改",
            ok,
            f"content = {c!r}\n        WARNING = {warns!r}\n"
            f"        GET = {mem.get!r}\n        日志 = {logs.records_text()!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C68 表外点名 vs 画面内表内角色：warn 报冲突、内容一字不改",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C69 同一场景 consistency_check=fix：**删除**画面里的表内角色标签（1.0.20）──
    # 与 warn 的唯一区别是行为：表外角色没有标准标签可用（插件绝不编造游戏名 / 标签），
    # 所以 fix 是**删除**而不是替换 —— 删掉 `Hanabi (Honkai: Star Rail)`（名字 / 别名 /
    # 标准标签三种写法都认），并按需清掉作品名括号、行首空格与连续分隔符；
    # **外观描述原文一字不动**（银白色长发的少女站在镜头前 / 蓝白饰冠 / 长杖 都要在）。
    try:
        cfg = make_config(roster=RS3, memory=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        raw = (
            "Hanabi (Honkai: Star Rail) 银白色长发的少女站在镜头前，"
            "头上戴着蓝白饰冠，一身蓝裙，手里握着长杖"
        )
        history = [history_message("这身是「刻律德拉」？", 1.0)]
        with (
            FakeMemory(error=TimeoutError("timed out")) as mem,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("嗯"),
                    "stream_id": "s-c69",
                },
            )
        c = out["args"]["content"]
        warns = logs.warnings()
        records = logs.records_text()
        ok = (
            "hanabi" not in c.lower()  # 错误标签被删掉
            and "Hanabi" not in c
            and "花火" not in c
            and "Honkai" not in c  # 连带作品名括号一起清掉，不留半截残渣
            and "银白色长发的少女站在镜头前" in c  # 外观描述原文保留
            and "蓝白饰冠" in c
            and "长杖" in c
            and c.startswith("银白色长发的少女站在镜头前")  # 行首不留空格/标点
            and ", ," not in c
            and ",," not in c
            and "，，" not in c
            and ",，" not in c
            and not c.lstrip().startswith((",", "，"))
            and not any(
                line.lstrip().startswith((",", "，")) for line in c.splitlines()
            )
            and "一致性检查=冲突已修正（fix）：删除画面里的「花火」标签" in records
            and "用户点名的是角色表外角色「刻律德拉」" in records
            and "一致性检查=冲突（warn 不改内容）" not in warns  # fix 模式不该退回 warn
            and "一致性检查=跳过" in logs.debugs()  # 既有对账仍是跳过（两条独立）
            and len(mem.get) == 1
            and not mem.post
        )
        check(
            "C69 同场景 fix：删除画面里的花火标签、保留外观描述原文",
            ok,
            f"content = {c!r}\n        WARNING = {warns!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C69 同场景 fix：删除画面里的花火标签、保留外观描述原文",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C70（防误报）用户自己也提过画面里的角色 → 不判冲突（1.0.20）──────────
    # 这条守的是 1.0.20 最容易踩的坑：只要看到"表外点名"就报警，把**用户自己说的**
    # 正常画面判成冲突。用户这批消息里既有表外点名（「刻律德拉」），也点过画面里
    # 那个角色（「画花火吧」）—— 冲突的第 3 个条件（用户这次没点过 Y）不成立，
    # 必须**一个警告都不打**、内容一字不改，日志里能看到「表外点名检查=正常」。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "warn"
        raw = "Hanabi (Honkai: Star Rail) 银白色长发的少女站在镜头前，头上戴着蓝白饰冠"
        history = [
            history_message("说起来，「刻律德拉」那身也不错", 1.5),
            history_message("画花火吧，顺便看看这身", 1.0),
        ]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage("嗯"),
                    "stream_id": "s-c70",
                },
            )
        c = out["args"]["content"]
        warns = logs.warnings()
        debugs = logs.debugs()
        ok = (
            c == raw  # 内容不变
            and "冲突" not in warns  # 一条冲突告警都没有
            and "表外点名检查=正常" in debugs  # 新检查跑了，且判成"不是冲突"
            and "刻律德拉" in debugs  # 表外点名确实被抓到了（否则这条用例是空转）
            and "用户点名「" not in warns
        )
        check(
            "C70 用户自己也提过画面里的角色 → 不误报冲突",
            ok,
            f"content = {c!r}\n        WARNING = {warns!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C70 用户自己也提过画面里的角色 → 不误报冲突",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C71（防误报）没有任何表外点名 → 行为与 1.0.19 一致（1.0.20）────────
    # 用户消息只有「嗯」「行～」这类普通回应：1.0.20 这条逻辑必须**完全不动**
    # （跳过），既有对账该跳过还是跳过 —— 内容与 `consistency_check="off"`
    # （完全不检查）时逐字一致。
    try:
        raw = "Hanabi (Honkai: Star Rail) 银白色长发的少女站在镜头前，头上戴着蓝白饰冠"
        history = [history_message("行～", 1.0)]
        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": raw},
            "message": FakeMessage("嗯"),
            "stream_id": "s-c71",
        }

        def scenario_cfg(mode: str) -> CharPromptGuardConfig:
            cfg = make_config(roster=RS3)
            cfg.general.history_cache_ttl = 0.0
            cfg.general.consistency_check = mode
            return cfg

        with FakeHistory(history):
            gold = (await fire(scenario_cfg("off"), params))["args"]["content"]
        with FakeHistory(history), LogCapture() as logs:
            out = await fire(scenario_cfg("warn"), params)
        c = out["args"]["content"]
        warns = logs.warnings()
        debugs = logs.debugs()
        ok = (
            c == raw
            and c == gold  # 与「完全不检查」逐字一致
            and "冲突" not in warns  # 不因新增逻辑冒出告警
            and "表外点名检查=跳过" in debugs  # 新检查明确跳过
            and "一致性检查=跳过" in debugs  # 既有对账也照旧跳过（1.0.19 行为）
        )
        check(
            "C71 没有表外点名 → 行为与 1.0.19 一致（内容不变、无新告警）",
            ok,
            f"content = {c!r}\n        gold = {gold!r}\n        WARNING = {warns!r}\n"
            f"        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C71 没有表外点名 → 行为与 1.0.19 一致（内容不变、无新告警）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C72 角色解析成功 → **替换**画面里的错误标签（1.0.21）──────────────
    # 用户反馈：1.0.20 的 fix 把画面里的 hanabi 删掉了，却没有补上「刻律德拉」的标签
    # → 用户侧看到的是「这个角色的提示词没有进去」。1.0.21 起先用已配置的模型解析出
    # 正式名 + 作品名 + NAI 标签，再拿它**替换**（而不是只删除）：
    #   `hanabi \(honkai: star rail\)` → `kleidra \(honkai: star rail\)`
    # 并在 natural 模式补上中文说明（（画面角色：“刻律德拉”，出自游戏《…》））。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        with (
            FakeResolve(KLEIDRA_PAYLOAD) as stub,
            FakeHistory([history_message("这身是「刻律德拉」？", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": KLEIDRA_SCENE},
                    "message": FakeMessage("嗯"),
                    "stream_id": "s-c72",
                },
            )
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "hanabi" not in c.lower()  # 错误标签没了
            and "Hanabi" not in c
            and "花火" not in c
            and TAG_KLEIDRA in c  # ★ 正确角色的标签进来了
            and "刻律德拉" in c  # natural 模式的中文说明
            and "画面角色" in c
            and "崩坏：星穹铁道" in c  # 作品名也在（追加模板里的 {game}）
            and "银白色长发的少女站在镜头前" in c  # 外观描述原文保留
            and "蓝白饰冠" in c
            and "长杖" in c
            and "角色解析" in records  # 解析日志
            and "替换"
            in records  # 「一致性检查=冲突已修正（fix）：把画面里的…替换为…」
            and "依据=角色解析(LLM)" in records
            and stub.calls == ["刻律德拉"]  # 只问一次模型
            and "建议加入角色表" in records  # 只打日志的「建议加入角色表」也在
        )
        check(
            "C72 解析成功 → 用解析出的标签替换画面里的错误标签",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C72 解析成功 → 用解析出的标签替换画面里的错误标签",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C73 解析返回 unknown=true → 退化为 1.0.20 的删除行为（1.0.21）──────
    # 模型不认识这个角色（或没把握）时必须**什么都不补**：不能拿别的角色顶上，
    # 也不能凭空造标签。行为与 1.0.20 逐字一致 —— 删掉画面里的错误标签，
    # 并且**不出现任何新标签**。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        with (
            FakeResolve({"unknown": True}) as stub,
            FakeHistory([history_message("这身是「刻律德拉」？", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": KLEIDRA_SCENE},
                    "message": FakeMessage("嗯"),
                    "stream_id": "s-c73",
                },
            )
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "hanabi" not in c.lower()  # 旧标签被删
            and "Hanabi" not in c
            and "花火" not in c
            and "Honkai" not in c
            and "kleidra" not in c.lower()  # ★ 没有补任何新标签
            and "刻律德拉" not in c  # 也没有中文说明（unknown → 不补）
            and "画面角色" not in c
            and "银白色长发的少女站在镜头前" in c  # 外观描述原文保留
            and "长杖" in c
            and "角色解析" in records
            and "unknown" in records  # 失败原因写清楚
            and "替换" not in records  # 没走替换分支
            and "一致性检查=冲突已修正（fix）：删除画面里的「花火」标签" in records
            and stub.calls == ["刻律德拉"]
        )
        check(
            "C73 解析 unknown=true → 退化为 1.0.20 的删除（不补新标签）",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C73 解析 unknown=true → 退化为 1.0.20 的删除（不补新标签）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C74 解析超时/异常 → 不崩、同样按 1.0.20 的行为处理（1.0.21）────────
    # 模型端挂起或网络故障时绝不能拦住生图：异常只记日志，内容按 1.0.20 处理
    # （fix 删除错误标签），且**日志里必须能看到失败原因**，便于用户排查。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        with (
            FakeResolve(error=TimeoutError("timed out")) as stub,
            FakeHistory([history_message("这身是「刻律德拉」？", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": KLEIDRA_SCENE},
                    "message": FakeMessage("嗯"),
                    "stream_id": "s-c74",
                },
            )
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "hanabi" not in c.lower()  # 不崩：按 1.0.20 的 fix 删掉错误标签
            and "花火" not in c
            and "kleidra" not in c.lower()
            and "刻律德拉" not in c
            and "银白色长发的少女站在镜头前" in c
            and "角色解析" in records
            and "TimeoutError" in records  # 失败原因（超时/异常）
            and "替换" not in records
            and stub.calls == ["刻律德拉"]
        )
        check(
            "C74 解析超时/异常 → 不崩、按 1.0.20 行为处理",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C74 解析超时/异常 → 不崩、按 1.0.20 行为处理",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C75 同一角色解析两次 → 第二次命中缓存，不再调用模型（1.0.21）────────
    # 同一句话里连续生两张图时不该重复问模型：解析结果进**进程内缓存**
    # （成功 24 小时），第二次的日志是 `角色解析(缓存)`，调用计数器必须停在 1。
    # 两次的内容也要求逐字一致 —— 缓存不能带来行为差异。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        history = [history_message("这身是「刻律德拉」？", 1.0)]
        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": KLEIDRA_SCENE},
            "message": FakeMessage("嗯"),
            "stream_id": "s-c75",
        }
        with FakeResolve(KLEIDRA_PAYLOAD) as stub, FakeHistory(history):
            with LogCapture() as first_logs:
                first = await fire(cfg, params)
            with LogCapture() as second_logs:
                second = await fire(cfg, params)
        c1 = first["args"]["content"]
        c2 = second["args"]["content"]
        first_text = first_logs.records_text()
        second_text = second_logs.records_text()
        ok = (
            len(stub.calls) == 1  # ★ 第二次不再调用模型
            and "角色解析(缓存)" in second_text  # 日志写明命中缓存
            and "角色解析(缓存)" not in first_text  # 第一次是模型给的
            and "来源=模型" in first_text
            and c1 == c2  # 缓存不改变结果
            and TAG_KLEIDRA in c2
        )
        check(
            "C75 同一角色解析两次 → 第二次命中缓存（不再调用模型）",
            ok,
            f"解析调用 = {stub.calls!r}\n        content = {c2!r}\n"
            f"        第一次日志（片段） = {first_text[-220:]!r}\n"
            f"        第二次日志（片段） = {second_text[-220:]!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C75 同一角色解析两次 → 第二次命中缓存（不再调用模型）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C76（防回归）角色在角色表内 → **完全不做解析**（1.0.21）────────────
    # 表内角色本来就有标准标签，没有任何理由去问模型：用户这次点过画面里那个角色
    # （没有冲突可替换），画面里也有角色标签（没有空缺要补入）→ **一次模型都不问**，
    # 行为与 1.0.20 完全一致（画面里的标签一字不改）。
    # 解析 stub 在这里的作用是**计数器**：只要插件真的调用了模型，这条就会失败。
    #
    # 日志断言说明（1.0.21 修正）：预判不满足时会留一条 DEBUG
    # `角色解析=跳过（<原因>）`（用户要能看出"为什么这次没解析"），所以不能再断言
    # 「日志里不出现 角色解析」；改成断言**没有任何真的解析动作**（既没有
    # `角色解析(LLM)` 的成败行、也没有 `角色解析(缓存)`），并额外要求那条跳过原因
    # 确实打了出来 —— 语义与「完全不做解析」一致，且更严格。
    try:
        cfg = make_config(roster=RS3)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "warn"
        raw = r"hanabi \(honkai: star rail\), 银白色长发的少女站在镜头前"
        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": raw},
            "message": FakeMessage("画花火吧"),
            "stream_id": "s-c76",
        }
        with (
            FakeResolve(KLEIDRA_PAYLOAD) as stub,
            FakeHistory([history_message("画花火吧，顺便看看这身", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            c == raw  # 画面里的表内标签一字不改
            and "kleidra" not in c.lower()
            and "刻律德拉" not in c
            and stub.calls == []  # ★ 表内角色完全不做解析
            and "角色解析(LLM)" not in records  # 没有解析成功 / 失败行
            and "角色解析(缓存)" not in records  # 也没有命中缓存
            and "角色解析=跳过" in records  # ★ 只留一条「为什么跳过」
            and "表外点名检查=正常" in logs.debugs()  # 走的是 1.0.20 那条判断
        )
        check(
            "C76 角色在角色表内 → 完全不做解析（调用次数 0）",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C76 角色在角色表内 → 完全不做解析（调用次数 0）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C77 皮肤表命中 → 零模型调用，皮肤关键词追加进提示词（1.0.22）────────
    # 用户需求原话：「我想看粉色妖精小姐」→ 出现**爱莉穿着这一套**的衣服。
    # 角色那一侧照旧（这里由上文「我们来聊爱莉希雅吧」的主题跟踪给出爱莉希雅），
    # 皮肤走 `[outfits] entries` 精确命中 → **一次模型都不问**。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=PINK_REQUEST)
        params["stream_id"] = "s-c77"
        with (
            FakeOutfit(OUTFIT_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "pink elf dress" in c  # ★ 表里的英文关键词进来了
            and "fairy wings" in c
            and "粉色妖精小姐" in c  # ★ 皮肤名也写进了说明（可核对）
            and "皮肤/时装" in c
            and "爱莉希雅的粉色妖精装" in c  # 表第 4 列的中文描述
            and "爱莉希雅" in c  # ★ 角色照旧补进来了
            and TAG_ELYSIA in c  # 角色标准标签也在（tag_completion）
            and stub.calls == []  # ★ 表命中：一次模型都没问
            and "皮肤解析(表)" in records
            and "皮肤解析(模型)" not in records
            and "皮肤解析(LLM)失败" not in records
        )
        check(
            "C77 皮肤表命中 → 零模型调用，皮肤关键词进提示词",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C77 皮肤表命中 → 零模型调用，皮肤关键词进提示词",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C78 表里没有 + auto_resolve → 模型解析成功并注入（1.0.22）──────────
    # 皮肤表是"零成本快路径"，表里没有时才问一次模型；成功后不仅要注入，
    # 还要打一条「建议加入皮肤表：…」（只打日志、绝不改配置文件），
    # 让用户能把这次的结果粘进配置，下次就零成本了。
    try:
        cfg = make_config(roster=RS3, outfits="", outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=PINK_REQUEST)
        params["stream_id"] = "s-c78"
        with (
            FakeOutfit(OUTFIT_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword in c for keyword in PINK_KEYWORDS)  # ★ 模型给的关键词进来了
            and "粉色妖精小姐" in c
            and "爱莉希雅" in c  # 角色照旧
            and stub.calls == ["粉色妖精小姐"]  # ★ 只问一次模型
            and "皮肤解析(模型)" in records
            and "角色=爱莉希雅" in records  # 归属角色线索（来自上文）
            and "建议加入皮肤表" in records  # ★ 只打日志的建议行
            and "pink elf dress, fairy wings" in records
        )
        check(
            "C78 表未命中 + auto_resolve → 模型解析成功并注入",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C78 表未命中 + auto_resolve → 模型解析成功并注入",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C79 解析返回 unknown=true → 什么都不注入，角色注入照旧（1.0.22）──────
    # 模型不认识这套皮肤（或没把握）时必须**什么都不加**：绝不瞎编关键词。
    # 内容与「关掉 [outfits]」时**逐字一致** —— 这条链路对角色那一侧零影响。
    try:
        cfg_on = make_config(roster=RS3, outfits="", outfits_enabled=True)
        cfg_on.general.history_cache_ttl = 0.0
        cfg_off = make_config(roster=RS3, outfits="", outfits_enabled=False)
        cfg_off.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=PINK_REQUEST)
        params["stream_id"] = "s-c79"
        history = [history_message("我们来聊爱莉希雅吧", 1.0)]
        with (
            FakeOutfit({"unknown": True}) as stub,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out_on = await fire(cfg_on, params)
        with FakeHistory(history):
            out_off = await fire(cfg_off, params)
        c = out_on["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword not in c for keyword in PINK_KEYWORDS)  # ★ 没有皮肤关键词
            and "粉色妖精小姐" not in c
            and "皮肤/时装" not in c
            and "爱莉希雅" in c  # ★ 角色注入照旧
            and TAG_ELYSIA in c
            and c == out_off["args"]["content"]  # ★ 与关掉 [outfits] 时逐字一致
            and stub.calls == ["粉色妖精小姐"]  # 问过一次（模型给了 unknown）
            and "皮肤解析(LLM)失败" in records
            and "unknown" in records  # 失败原因写清楚
            and "建议加入皮肤表" not in records  # 失败就没有建议行
        )
        check(
            "C79 解析 unknown=true → 不注入皮肤关键词、角色注入照旧",
            ok,
            f"content = {c!r}\n        关掉 [outfits] 的 content = "
            f"{out_off['args']['content']!r}\n        解析调用 = {stub.calls!r}\n"
            f"        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C79 解析 unknown=true → 不注入皮肤关键词、角色注入照旧",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C80（防误报）用户消息里没有皮肤意图 → 不触发皮肤逻辑（1.0.22）────────
    # 「画花火吧」里没有任何皮肤/时装的说法：不该去问模型，也不该留下
    # `皮肤解析` 这类日志（否则用户会以为插件"在乱猜皮肤"）；画面里已经有角色，
    # 内容照旧一字不改地放行。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        raw = "hanabi \\(honkai: star rail\\), 银白色长发的少女"
        params = {
            "signature": "nai_artist:action:share_photo",
            "action_name": "share_photo",
            "args": {"content": raw},
            "message": FakeMessage("画花火吧"),
            "stream_id": "s-c80",
        }
        with (
            FakeOutfit(OUTFIT_PAYLOAD) as stub,
            FakeHistory([history_message("画花火吧，顺便看看这身", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            stub.calls == []  # ★ 一次模型都不问
            and "皮肤解析" not in records  # ★ 日志里没有皮肤解析
            and "皮肤/时装" not in c
            and all(keyword not in c for keyword in PINK_KEYWORDS)
            and c == raw  # 画面里已经有角色 → 内容一字不改
        )
        check(
            "C80 用户消息里没有皮肤意图 → 不触发皮肤逻辑（0 次调用）",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C80 用户消息里没有皮肤意图 → 不触发皮肤逻辑（0 次调用）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C81 皮肤 + 角色同时命中 → 角色在前、皮肤在后（1.0.22）──────────────
    # 注入顺序是这条需求的关键：皮肤说明必须追加在**角色**之后，
    # 这样下游翻译模型先看到角色标签、再看到装扮关键词（衣服归属不会串人）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=PINK_REQUEST)
        params["stream_id"] = "s-c81"
        with (
            FakeOutfit(OUTFIT_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        # 三个位置都要能找到，且顺序为 角色中文名 < 角色标签 < 皮肤说明（皮肤名 → 关键词）
        ok = (
            "爱莉希雅" in c
            and TAG_ELYSIA in c
            and "pink elf dress" in c
            and c.index("爱莉希雅") < c.index(TAG_ELYSIA)
            and c.index(TAG_ELYSIA) < c.index("粉色妖精小姐")
            and c.index("粉色妖精小姐") < c.index("pink elf dress")
            and stub.calls == []
            and "皮肤注入" in records  # 「皮肤注入: …（搭配角色=爱莉希雅）」
            and "搭配角色=爱莉希雅" in records
            and "已追加皮肤/时装" in records
        )
        check(
            "C81 皮肤 + 角色同时命中 → 角色在前、皮肤在后",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C81 皮肤 + 角色同时命中 → 角色在前、皮肤在后",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C82 [outfits] enabled=false → 零开销、零调用、内容一致（1.0.22）──────
    # 关掉时必须真的"什么都不做"：不解析皮肤表、不读上文、不问模型，日志里连
    # `皮肤解析` 都没有（连"跳过"都不打）；内容与「开着这一维但不注入任何东西」
    # 时**逐字一致**（对照组 = 皮肤表为空 + auto_resolve=false：识别照跑、解析关掉）。
    try:
        cfg_off = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=False)
        cfg_off.general.history_cache_ttl = 0.0
        # 对照组：开着但不注入（皮肤表为空 + auto_resolve=false）
        cfg_idle = make_config(
            roster=RS3,
            outfits="",
            outfits_enabled=True,
            outfits_auto_resolve=False,
        )
        cfg_idle.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=PINK_REQUEST)
        params["stream_id"] = "s-c82"
        history = [history_message("我们来聊爱莉希雅吧", 1.0)]
        with (
            FakeOutfit(OUTFIT_PAYLOAD) as stub,
            FakeHistory(history),
            LogCapture() as logs_off,
        ):
            out_off = await fire(cfg_off, params)
        with FakeHistory(history):
            out_idle = await fire(cfg_idle, params)
        c = out_off["args"]["content"]
        records = logs_off.records_text()
        ok = (
            stub.calls == []  # ★ 0 次模型调用
            and "皮肤解析" not in records  # ★ 日志里没有皮肤解析
            and "皮肤/时装" not in c
            and all(keyword not in c for keyword in PINK_KEYWORDS)
            and c == out_idle["args"]["content"]  # ★ 内容与对照组逐字一致
            and "爱莉希雅" in c  # 角色注入不受影响
        )
        check(
            "C82 [outfits] enabled=false → 0 次调用、无皮肤日志、内容一致",
            ok,
            f"content = {c!r}\n        对照组的 content = "
            f"{out_idle['args']['content']!r}\n        解析调用 = {stub.calls!r}\n"
            f"        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C82 [outfits] enabled=false → 0 次调用、无皮肤日志、内容一致",
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
