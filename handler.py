"""char_prompt_guard 事件处理器。

订阅 ``before_action_call`` 事件，在生图 Action 真正执行之前检查提示词：

- 提示词里已经带有角色 → 放行；
- 没有角色 → 按「角色表快速匹配 → LLM 兜底提取」的顺序从最近对话上文中
  找出当前指代的角色，补入提示词；多角色同框时按配置补齐多个。
- 「这次该画谁」由**主题跟踪器**（:func:`monitor.track_subject`，1.0.14 起）给出：
  按时间正序扫最近 ``monitor.window`` 条消息，逐条演进一个「当前主题」
  （改口 / 多人 / 点名 / 本尊身份 / 代词回指 / 拆单），置信度 = 信号强度 × 时间衰减，
  低于 ``monitor.min_confidence`` 就不补。``monitor.enabled = false`` 时**完全退回
  1.0.13** 的「自我指代扫描（②.5）+ 用户点名/主角粘性（③）」链路（见
  :meth:`CharacterGuardHandler._collect_candidates` 里的 else 分支）。
- **拆单**（1.0.16 起）：同一条用户消息里既有自我指代词又有角色表内角色时（调用方
  把一单拆成两张图：先看本尊、再看壳），主题「不可单独确定」—— 这是唯一允许 LLM
  压过主题的情形：强制问一次 LLM，按**当前画面描述**判定；判定不出就不补。

不同生图插件的提示词语义不同，因此按「目标 Action」区分追加格式：

- ``natural``：提示词是自然语言，由生图插件自己再翻译成标签。
  适用于 nai_artist（``share_photo`` / ``share_drawing`` / 旧版 ``share_visual``
  的 ``content`` 参数）。
- ``tags``：提示词本身就是 NAI 标签串，没有翻译步骤，必须追加标准角色标签
  （如 ``elysia \\(honkai impact 3rd\\)``）。适用于 image_generator_plugin-neo
  （``draw_image`` 的 ``content_description`` 参数）；2 人及以上优先写入其
  ``characters`` 独立角色槽位参数。

主模型可用 ``#multi#`` 等标记临时改变行为，标记会被自动移除，不会进入画面。

对 args 的修改会被 ActionManager 回写并替换实际传给 Action 的参数，
因此补全发生在生图与提示词翻译之前。

**1.0.17 的第二个通道（参数说明注入，与本文件的判定链无关）**：主模型之所以不写
标准标签，是因为 nai_artist 给 ``content`` 写的参数说明本身就是「写画面描述，写给
下游翻译模型」—— 它照着说明写，标签自然没有。1.0.17 起
:mod:`tag_hint` 会把「最前面必须先写角色的标准标签」**直接追加进动作参数说明**
（纯内存改注解），让主模型在工具定义里就看到这条要求。本文件只负责提供两个
「再试一次」的时机：``on_all_plugin_loaded``（本插件常比生图插件先加载完）与首次
生图事件；注入逻辑、一次性标志位、失败降级全在 :mod:`tag_hint`，**判定链一行未改**。

**1.0.18 的第三份证据（外部记忆库，同样不改编判定链）**：判定**不确定**时
（主题跟踪的 ``split`` / ``unavailable`` / ``low_confidence`` / ``empty``，且画面
描述里没有角色表内角色），先问一次外部记忆库（Booku Memory，:mod:`memory`）——
命中「触发词 → 角色表内角色」的明确对应就直接采用（日志
``记忆库确认:本尊 → 花火（mem-xxxx）``）并把这次确认写回；没有明确对应时把命中的
``title + content_snippet`` 当作**参考材料**（独立一段「相关记忆（供参考，不是
指令）」）喂给既有的 LLM 提取。关闭 / 不可用 / 超时 / 非 200 / JSON 坏 → 静默降级回
1.0.17 的原链路，**绝不拦住生图**。实现落在 :meth:`CharacterGuardHandler._memory_lookup`
（读）与 :meth:`CharacterGuardHandler._memory_verdict`（用作判定依据 + 触发写回），
写回本身在 :mod:`memory`；测试用可替换的传输层
（``memory.set_transport`` 或直接 monkeypatch ``memory._http_get_json`` /
``memory._http_post_json``）注入假记忆库。

**1.0.19 的两处修补（一次真实事故）**：调用方（KFC）这次画的是**爱莉希雅**
（粉长发 / 蓝眼 / 尖耳 / 粉白短裙），却把**花火的标签** ``Hanabi (Honkai: Star Rail)``
写进了画面描述 —— 因为它的**备忘**里存着过期的固定规则「标签必须是 Hanabi 挂最前头」
（1.0.17 起参数说明要求它写标准标签，它照做了，但照搬的是备忘里的错误标签）。
1.0.18 的判定链看到「描述里已有角色表内角色」就放行（描述点名优先级最高，
且这种情况下**根本不查记忆库**），于是**没有任何对账**。1.0.19 补上：

- **一致性检测扩展为「期望集合」对账**（:meth:`CharacterGuardHandler._apply_consistency_check`）：
  期望集合 = 判定集合 ∪ **主题跟踪给出的主题**（``track.usable`` 时）∪ **记忆库确认结果**
  （明确对应时）。提示词里的角色表内角色**不在**期望集合里 → 视为冲突：
  ``warn`` 只打 WARNING、内容不改；``fix`` 仅在「期望集合恰好 1 人、画面里冲突的角色
  恰好 1 个」时把画面里那个角色的名字 / 别名 / 标签替换成期望角色的（复用
  :func:`character.replace_character_reference`），多角色 / 歧义 → 退回 warn；
- **描述已点名时也做一次「对账用」记忆库查询**
  （:meth:`CharacterGuardHandler._memory_reconcile`）：``consistency_check != "off"``
  且 ``memory.enabled`` 且角色表非空、且描述里出现了角色表内角色时，用**描述里那个
  角色的名字 / 别名 + 自我指代词**做关键词查一次（``limit`` 用配置值），结果
  **只**喂给上面那条对账，**不参与判定链、不替换描述点名的角色、不写回**；
  失败 / 超时 / 结构不符照旧静默降级。

**1.0.20 的第三条判断（又一次真实事故，同样不新增配置项）**：用户这次点名的是
**角色表之外**的角色（「刻律德拉」，不在角色表里），画面里却挂着**角色表内**的
花火标签 ``hanabi (honkai: star rail)``（来自调用方过期的备忘规则）。实测日志里
1.0.19 的三份证据**全都是空的** —— 记忆库 ``status=unavailable``、主题跟踪
``判定依据=无(不补)`` —— 于是期望集合为空、一致性检查**直接跳过**：

    记忆库对账查询（画面点名=花火）: status=unavailable
    提示词已包含角色，放行
    一致性检查=跳过（没有可对账的期望集合…）      ← ★ 该报警却没报

1.0.20 因此另加一条**独立**判断（复用 ``general.consistency_check`` 的
off/warn/fix，与期望集合那套互不影响）：

- **提取「用户点名的表外角色名」**（:meth:`CharacterGuardHandler._scan_user_naming`
  + :func:`character.extract_outside_names`）：焦点范围沿用自我指代扫描的做法
  （**触发消息 + 触发之前最近 N 条用户消息**，N = ``min(multi.recent_messages, 4)``），
  但**只取用户消息**、bot 的复述不算，也**不扫画面描述**（那是被检查的一侧）；
  引号模式（``「」 『』 《》 “” ""``）与句式模式（``披上X这层壳 / 扮演X / 画X /
  要X / 看看X / 换成X / 来张X``）各抓一遍，再按长度 / 纯标点 / 常见词黑名单 /
  「已经在角色表里」三条过滤 —— 细节见 ``character`` 模块的「表外点名」一节；
- **判断冲突**（:meth:`CharacterGuardHandler._apply_outside_naming_check`）：
  存在表外点名 ``X`` + 最终提示词里出现了角色表内角色 ``Y`` + **这批用户消息里
  没提过 ``Y``** → 冲突。``warn`` 只打 WARNING（格式里写清两侧名字与建议）、
  **内容一字不改**；``fix`` **删除**画面里 ``Y`` 的名字 / 别名 / 标准标签
  （我们没有表外角色的标准标签可替换，只能删）并清理删除后的
  ``,,`` / ``, ,`` / 行首孤立标点等残渣，**外观描述原文一字不动**，打 INFO；
  删不出可识别文本、或删完提示词就空了 → 退回 warn 行为；``off`` 完全不检查
  （连用户消息都不扫）。
- 与既有对账**两条独立、可以各自触发**：本条跑在 :meth:`CharacterGuardHandler._finalize`
  里、:meth:`CharacterGuardHandler._apply_consistency_check` **之后**，因此既有对账
  若已修正过内容，本条按**修正后**的文本再判一次；两条都命中时日志各有一条。

**1.0.21 的「角色解析（LLM）」**（用户反馈：只删不补等于"这个角色的提示词没有进去"）：
1.0.20 的 ``fix`` 把画面里那个过期的错误标签**删掉**，却没有补上用户真正点名的角色的
标签，用户侧看到的是"角色没进去"。1.0.21 因此新增 ``general.auto_resolve_characters``
（默认开）与 :meth:`CharacterGuardHandler._resolve_outside_character`：用**已配置的
模型**（同一套 ``llm_api``、同一个 ``translate_model`` / ``llm_timeout``，不带新依赖）
把表外角色解析成「正式名 + 作品名 + NAI 标签」，再走两个入口：

- **入口①替换**（:meth:`CharacterGuardHandler._apply_outside_naming_check`）：表外点名
  对账判定为冲突、``consistency_check="fix"`` 且解析成功 → 用解析出的标签（+ natural
  模式的中文说明）**替换**画面里的错误标签，日志
  ``一致性检查=冲突已修正（fix）：把画面里的「花火」替换为「刻律德拉」（替换 N 处），依据=角色解析(LLM):刻律德拉 → Kleidra…``；
- **入口②补入**（:meth:`CharacterGuardHandler._resolved_for_injection` +
  :meth:`CharacterGuardHandler._inject_resolved`，在 ``execute`` 里跑在判定链**之前**）：
  用户在最近消息里点名了表外角色、解析成功、而画面里**没有任何角色标签**（例如描述只有
  外观）→ 按 ``natural`` / ``tags`` 模式把解析结果补进提示词最前面（复用既有注入格式与
  ``tag_completion``），日志 ``已按角色解析补入（LLM）：<中文名> → <标签>``。

**1.0.21 修正（一次实测回归）**：入口①原本会被「期望集合对账」抢在前面 —— ``_finalize``
把解析结果并进判定集合，``fix`` 模式下对账先做了替换，于是日志里的依据写成
``判定集合=…``、natural 模式的中文说明也没补上（用例 C72 就是这么失败的）。
现在解析结果**不再并进判定集合**：对账只负责它自己的证据（判定 / 主题跟踪 / 记忆库），
解析结果一律由入口①处理 —— 依据里能看出「用的是角色解析」，中文说明与标准标签也一起补。

解析结果的**校验与降级**（:func:`character.parse_resolution_payload` /
:func:`character.validate_tag`）：``unknown=true`` / JSON 坏 / 标签不符合规范 /
``confidence`` 低于 0.5 → 一律视为**解析失败**，退化为 1.0.20 的行为（``warn`` 不变 /
``fix`` 只删除），日志 ``角色解析(LLM)失败：<原因>``；任何异常（超时 / 网络 / 解析）
只记 DEBUG/WARNING，**绝不拦住生图**。解析成功额外打一条 INFO
``建议加入角色表：<中文名> | <作品名> | <英文名小写>,<中文名> | <标签>``
（**只打日志，绝不自动改配置文件**）。

**缓存**（1.0.21 修正：**读写在调用方**，即
:meth:`CharacterGuardHandler._resolve_with_cache`）：同一角色名**不重复问模型** ——
成功缓存 24 小时（:data:`character.RESOLVE_CACHE_TTL_SECONDS`）、失败负缓存 10 分钟
（:data:`character.RESOLVE_FAILURE_TTL_SECONDS`）；命中缓存时日志为
``角色解析(缓存): …``。``character.resolve_character_via_llm`` 保持**纯解析**
（不读不写缓存、也不打成功日志），否则回归用例替换掉它的同时会把缓存一起替换掉
（C75：两次生图只允许问一次模型）。缓存的存储与清空入口在 :mod:`character`
（``resolve_cache_get`` / ``resolve_cache_put`` / :func:`character.reset_resolve_cache`），
「何时读、何时写、命中怎么打日志」全在 handler。
``memory.write_back`` 为真时，解析结果还会**写回记忆库**
（:meth:`CharacterGuardHandler._write_resolved_to_memory`，走
``memory.MemoryClient.write`` 这一条现有写回通道，三元组照旧同时非空）。

**1.0.22 的「皮肤 / 时装」这一维**（用户需求：「我想看看你的 xxxx（皮肤）」→
搜索并加进去，例如「我想看粉色妖精小姐」→ 爱莉穿着这一套的衣服）：

**角色那一侧一个字都没改**（描述点名 > 主题跟踪 > 记忆库 > LLM 仲裁 > 不补），
新增的只是「这次要穿哪一套」：

- **配置**：新增 ``[outfits]`` 节 —— ``enabled``（默认开，关掉=整条链路零开销）、
  ``entries``（皮肤表：``皮肤名 | 角色名(可空) | 英文关键词或 NAI 标签 | 中文描述(可空)``）、
  ``auto_resolve``（表里没有的皮肤名是否问模型）、``write_back``（是否写回记忆库）；
- **识别**（:meth:`CharacterGuardHandler._scan_outfit_names` +
  :func:`outfit.extract_outfit_names`）：只从**用户消息**里找（触发消息 + 触发前
  最近 ``min(multi.recent_messages, 4)`` 条用户消息，bot 消息跳过；
  **不扫画面描述**），句式模式（``我想看(看)?你的X`` / ``看看你的X`` / ``穿(上)?X`` /
  ``换上X`` / ``X这套|这一套`` / ``X(皮肤|时装|衣服|裙子|装扮|造型)`` / ``我想看X``）
  与引号模式（``「」 『』 “” ""``）各抓一遍，再过长度 / 虚词 / 黑名单 / 通用词 /
  「已经在角色表里」五道过滤；命中皮肤表的候选**优先**，多个候选时取
  **最新一条消息里的、最长**的一个；
- **解析**（:meth:`CharacterGuardHandler._resolve_outfit`）：**表优先 → 模型兜底** ——
  表命中零模型调用（直接用表里的关键词与中文描述）；表未命中且 ``auto_resolve`` 时
  用**已配置的模型**（:func:`outfit.resolve_outfit_via_llm`，与角色解析同一套
  ``llm_api`` / ``translate_model`` / ``llm_timeout``）解析成小写 ASCII 关键词，
  ``unknown`` / JSON 坏 / ``confidence < 0.5`` / 既无 tag 又无 keywords 一律判失败
  （**什么都不注入**，日志 ``皮肤解析(LLM)失败：<原因>``）；成功打 INFO
  ``皮肤解析(模型): …`` 与 ``建议加入皮肤表：…``（只打日志，绝不改配置文件）；
- **缓存**：进程内按「皮肤名 + 角色名」为键，成功 24 小时、失败负缓存 10 分钟，
  **读写在调用方**（:meth:`CharacterGuardHandler._resolve_outfit_with_cache`，
  与 1.0.21 的角色解析同一套分层，理由相同：解析函数是回归用例打桩的那一层）；
- **注入**（:meth:`CharacterGuardHandler._apply_outfit`，在 :meth:`_finalize` 的
  **最后一步**、两条一致性检查**之后**）：``natural`` 追加
  ``（皮肤/时装：“粉色妖精小姐”：pink elf dress, fairy wings）``、``tags`` 把关键词
  追加进同一个参数字符串；没有解析出角色时照样注入并打一条 DEBUG
  ``皮肤注入: …（无角色，仅注入皮肤关键词）``。放在最后一步是有意的：
  皮肤不是角色，不参与角色对账，也不会被角色修正逻辑（fix 替换 / 删除）碰到 ——
  「角色在前、皮肤在后」的次序自然成立。

**1.0.23 的三件事**（判定链仍然是「描述点名 > 主题跟踪 > 记忆库 > LLM 仲裁 > 不补」，
一行未改）：

- **皮肤名不再被当成表外角色名**（修一个真实风险）：用户说「画粉色妖精小姐这套」时，
  1.0.20 的 ``画X`` 句式可能把「粉色妖精小姐」当成**角色表外**的角色名，于是 1.0.21 的
  角色解析会多问一次模型；``consistency_check="fix"`` 时更会用解析结果去**替换 / 删除**
  画面里那个「用户这批消息没提过」的表内角色标签。现在皮肤识别**先跑**
  （:meth:`CharacterGuardHandler._scan_outfit_names`，1.0.22 起它本来就每次都跑），
  它认出来的皮肤 / 时装名（**句式 / 引号抓到的候选 + 命中皮肤表的正式名**，
  见 :class:`OutfitScope.names`）通过 ``exclude`` 传给
  :meth:`CharacterGuardHandler._scan_user_naming` →
  ``character.extract_outside_names(exclude=…)`` **豁免**；被豁免的候选会留一条
  DEBUG ``皮肤名豁免: …``。**注入位置与语义一个字没变**（皮肤仍追加在最后一步、
  角色在前皮肤在后），变的只有识别顺序 —— 表外点名扫描因此不再复用皮肤扫描备好的
  文本，两处各读一次上文（同一份进程内缓存，见 ``general.history_cache_ttl``）；
- **作品名去重**：``character._normalize_game_field`` 不再拼出
  ``… (Honkai: Star Rail) (Honkai: Star Rail)``（只影响展示 / 日志 / 注入文本，
  原始字段不动）；
- **``outfits.infer_unnamed``**（新增，**默认关**）：用户只说「流萤的那一套皮肤」而
  没点名具体皮肤时，开了就由模型推断这个角色最知名的一套并注入 ——
  :meth:`CharacterGuardHandler._infer_unnamed_outfit`（识别用
  :func:`outfit.find_unnamed_outfit_owner`，解析用 :func:`outfit.infer_outfit_via_llm`，
  缓存键 ``<角色名>|__infer__``），日志标注
  ``皮肤推断(模型): 流萤 → <皮肤名> → <关键词>（来源=模型推断，confidence=…）``；
  **默认关**时一次模型都不问、一条相关日志都不打，行为与 1.0.22 逐字一致；
  推断结果**不写回记忆库**（它是模型猜的，不该当成"用户确认过的事实"记住）。
"""

from __future__ import annotations

import importlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.message_api import (
    build_readable_messages_to_str,
    get_recent_messages,
)
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from . import memory as memory_api
from . import tag_hint
from .character import (
    BASIS_LATEST_MENTION,
    BASIS_USER_NAMED,
    CharacterEntry,
    NamingDecision,
    ResolvedCharacter,
    character_in_texts,
    content_contains_tag,
    delete_character_reference,
    entry_key,
    extract_characters_via_llm,
    extract_outside_names,
    find_characters_in_history,
    find_characters_in_text,
    find_roster_identity,
    find_self_reference_word,
    is_known_bot_message,
    iter_config_entries,
    merge_entries,
    message_text,
    normalize_prompt_text,
    order_messages_newest_first,
    parse_roster,
    replace_character_reference,
    resolve_cache_get,
    resolve_cache_put,
    resolve_character_via_llm,
    resolve_naming_decision,
    strip_quoted_blocks,
)
from .config import CharPromptGuardConfig
from .monitor import SubjectTrack, track_subject
from .outfit import (
    OUTFIT_SOURCE_TABLE,
    OutfitEntry,
    ResolvedOutfit,
    extract_outfit_names,
    find_outfit_entry,
    find_unnamed_outfit_owner,
    infer_outfit_via_llm,
    outfit_cache_get,
    outfit_cache_key,
    outfit_cache_put,
    outfit_infer_cache_key,
    parse_outfits,
    resolve_outfit_via_llm,
    write_outfit_to_memory,
)

logger = get_logger("char_prompt_guard")

APPEND_MODE_NATURAL = "natural"
APPEND_MODE_TAGS = "tags"

STRATEGY_CHARACTERS = "characters"
STRATEGY_APPEND_TAGS = "append_tags"

SCOPE_MULTI_ONLY = "multi_only"

# 主模型可用标记（会自动从提示词中移除，不进入画面）
MARKER_MULTI = "#multi#"
MARKER_SOLO = "#solo#"
MARKER_CHARS = "#chars#"
MARKER_TAGS = "#tags#"
MARKER_NOCHAR = "#nochar#"

_MARKER_PATTERN = re.compile(r"#(?:multi|solo|chars|tags|nochar)#", re.IGNORECASE)

# 标记优先级：同一类标记同时出现时按此取一个，避免"谁写在后面谁生效"的
# 不可预期行为。#nochar# 是一票否决；人数标记里 #solo# 更保守、压过 #multi#；
# 注入方式标记里 #chars#（独立角色槽位）比 #tags# 更具体。
_MARKER_PRIORITY: dict[str, int] = {
    MARKER_NOCHAR: 50,
    MARKER_SOLO: 40,
    MARKER_MULTI: 30,
    MARKER_CHARS: 20,
    MARKER_TAGS: 10,
}
_MARKER_STRATEGY: dict[str, str] = {
    MARKER_CHARS: STRATEGY_CHARACTERS,
    MARKER_TAGS: STRATEGY_APPEND_TAGS,
}
_MARKER_MULTI_GROUP: tuple[str, ...] = (MARKER_SOLO, MARKER_MULTI)
_MARKER_STRATEGY_GROUP: tuple[str, ...] = (MARKER_CHARS, MARKER_TAGS)

# agent 模式的聊天器（default_chatter / neo_default_chatter）把一个 Action 当
# **工具**调用（工具名形如 ``action-share_photo``），那条路径发的是
# ``before_tool_call`` / ``tool_name``，而不是 ``before_action_call`` /
# ``action_name``。只订阅前者的话，守卫在 agent 聊天器下永远收不到通知。
# 两个都订阅，两条路径都能覆盖（重复触发是幂等的：第二次会因"已含角色"放行）。
_TOOL_CALL_EVENT = getattr(EventType, "BEFORE_TOOL_CALL", None)
_SUBSCRIBE_EVENTS: list[EventType | str] = [EventType.BEFORE_ACTION_CALL]
if _TOOL_CALL_EVENT is not None:
    _SUBSCRIBE_EVENTS.append(_TOOL_CALL_EVENT)

# 「全部插件加载完成」（1.0.17）：只用来做一次「参数说明注入」的重试。
#
# 插件加载顺序由 ``loader.PluginLoader.resolve_load_order``（Kahn 拓扑 + 字典序）
# 决定，本插件的 ``dependencies`` 是空的，于是通常排在
# ``image_generator_plugin-neo`` / ``nai_artist`` **前面** —— 也就是说
# ``on_plugin_loaded`` 执行时，要改的那些动作**还没注册**。这个事件在所有插件
# 都加载完之后发布（``src/app/runtime/bot.py:809``），是重新注入的最佳时机；
# 核心若没有这个事件（老版本），自动退回「首次生图事件」再试一次。
_ALL_PLUGIN_LOADED_EVENT = getattr(EventType, "ON_ALL_PLUGIN_LOADED", None)
if _ALL_PLUGIN_LOADED_EVENT is not None:
    _SUBSCRIBE_EVENTS.append(_ALL_PLUGIN_LOADED_EVENT)


def _event_value(event: Any) -> str:
    """事件名归一化：``EventType`` 与裸字符串都能比（事件总线传的是裸字符串）。"""
    value = getattr(event, "value", None)
    return value if isinstance(value, str) else str(event)


#: 工具名相对 Action 名多出来的前缀（``action-share_photo`` → ``share_photo``）
_TOOL_NAME_PREFIX = "action-"
#: 工具组件自己的前缀（``tool-xxx`` → ``xxx``）
_COMPONENT_NAME_PREFIX = "tool-"

#: 自我指代最多回看「触发之前最近几条**用户**消息」（1.0.12）。
#:
#: 实际上限取 ``min(multi.recent_messages, 这个值)``。为什么不能只看触发消息：
#: 实测线上事故（12:14~12:15 同一会话）里，用户 12:14:39 说「那你再来图，要你自己～」，
#: 20 秒后（12:14:59）用一句「嗯哼？」触发了生图 —— 触发消息本身不含自我指代词，
#: 只看它就会按「主角粘性」沿用更早（12:12 引用块里）提到的「爱莉希雅」，把画面补错。
#: 上限取 4 而不是无限往回翻：再往前容易把别的话题里的「本尊」翻出来，
#: 反而压制掉正常的角色点名（``multi.recent_messages`` 默认 6，故默认回看 4 条）。
_SELF_REFERENCE_SCAN_LIMIT = 4

#: 「表外点名」扫描的焦点范围（1.0.20）：与自我指代扫描同一套做法 ——
#: 触发消息 + 触发之前最近 ``min(multi.recent_messages, 这个值)`` 条**用户**消息。
#:
#: 为什么沿用同一个上限：它守的是同一件事 —— 「用户这几句话里到底在说谁」。
#: 再往前翻容易把别的话题里的名字翻出来，反而制造误报；而这条判断的误报代价不低
#: （``fix`` 会删掉画面里正确的角色标签），所以宁少勿多。
_OUTSIDE_NAMING_SCAN_LIMIT = _SELF_REFERENCE_SCAN_LIMIT

#: ``fix`` 删除后至少要剩下多少个"非分隔符字符"，否则退回 warn（1.0.20）。
#: 提示词被删空、或只剩几个标点时，删掉的东西才是画面主体 —— 那就不是"清理错误
#: 标签"而是"清空画面"了，这种修改一律不做（只报警）。
_MIN_FIX_CONTENT_CHARS = 4


@dataclass(frozen=True)
class TargetSpec:
    """一个被接管的生图 Action。

    Attributes:
        plugin: 所属插件名；为空表示不限制插件
        action: 组件名（Action 的 name）
        prompt_arg: 承载提示词的参数名
        append_mode: 追加模式，``natural`` 或 ``tags``
        mode_arg: 需要按 ``general.apply_modes`` 过滤的参数名（如 nai_artist 的 mode）
        detect_args: 额外参与"是否已含角色"判断的参数名
        characters_arg: 独立角色槽位参数名（tags 模式多人时使用）
    """

    plugin: str
    action: str
    prompt_arg: str
    append_mode: str = APPEND_MODE_NATURAL
    mode_arg: str = ""
    detect_args: tuple[str, ...] = ()
    characters_arg: str = ""


@dataclass
class BotControl:
    """主模型通过标记给出的运行时指令。"""

    force_multi: bool | None = None
    force_strategy: str = ""
    nochar: bool = False
    markers_found: list[str] = field(default_factory=list)


@dataclass
class CandidateResult:
    """一次判定（「这次该补谁」）的完整结果。

    Attributes:
        candidates: 待补入的候选角色（已过滤掉提示词里已有的）
        decided: **判定集合** —— 本次判定认为画面里应该有的角色（不过滤、不截断），
            供二次一致性检测对账
        llm_multi: LLM 判定的「是否需要多人同框」
        mentioned: 上文中被提及的角色表内角色数（过滤前，供 multi_only 判断）
        naming: 确定性的「主题跟踪 / 用户点名·粘性」判定结果（1.0.14 起由主题跟踪给出，
            见 :meth:`monitor.SubjectTrack.naming`）
        basis: 判定依据的可读文案（进日志与 WARNING）
        memory: 本次「记忆库再确认」的结果（1.0.18；没查记忆库时是 ``None``）
        track: 本次主题跟踪的**完整结果**（1.0.19；``monitor.enabled = false`` 或
            跟踪器异常时为 ``None``）。二次一致性检测要用它构造「期望集合」
            （``track.usable`` 时把主题也算作期望），所以不能只留 ``naming``
    """

    candidates: list[CharacterEntry] = field(default_factory=list)
    decided: list[CharacterEntry] = field(default_factory=list)
    llm_multi: bool = False
    mentioned: int = 0
    naming: NamingDecision = field(default_factory=NamingDecision)
    basis: str = ""
    memory: memory_api.MemoryLookup | None = None
    track: SubjectTrack | None = None


_BUILTIN_TARGETS: tuple[TargetSpec, ...] = (
    # nai_artist 1.3.6+：旧的单个 share_visual 已拆成两个动作，都没有 mode 参数，
    # 动作名本身已区分「自拍 / 画作」，因此这里的模式过滤交给动作名，不再用 mode_arg。
    TargetSpec(
        plugin="nai_artist",
        action="share_photo",
        prompt_arg="content",
        append_mode=APPEND_MODE_NATURAL,
    ),
    TargetSpec(
        plugin="nai_artist",
        action="share_drawing",
        prompt_arg="content",
        append_mode=APPEND_MODE_NATURAL,
    ),
    # nai_artist 1.2.x 及更早：单动作 + mode 参数（drawing/photo），保留兼容
    TargetSpec(
        plugin="nai_artist",
        action="share_visual",
        prompt_arg="content",
        append_mode=APPEND_MODE_NATURAL,
        mode_arg="mode",
    ),
    # image_generator_plugin-neo：content_description 本身就是最终 NAI 标签串
    TargetSpec(
        plugin="image_generator_plugin-neo",
        action="draw_image",
        prompt_arg="content_description",
        append_mode=APPEND_MODE_TAGS,
        detect_args=("characters",),
        characters_arg="characters",
    ),
)


def parse_targets(raw_text: str) -> list[TargetSpec]:
    """解析目标 Action 表。

    每段格式：``插件名:action:动作名 | 提示词参数名 | 追加模式 | 模式参数 | 额外检测参数 | 角色槽位参数``。
    第 2 段必填，其余可省略。未配置时返回内置默认目标。
    """
    entries = iter_config_entries(raw_text)
    if not entries:
        return list(_BUILTIN_TARGETS)

    targets: list[TargetSpec] = []
    for entry in entries:
        parts = [part.strip() for part in entry.replace("｜", "|").split("|")]
        if not parts or not parts[0]:
            continue
        plugin, action = _split_target_signature(parts[0])
        if not action:
            logger.warning(f"目标 Action 配置缺少动作名，已忽略: {entry}")
            continue
        prompt_arg = parts[1] if len(parts) > 1 else ""
        if not prompt_arg:
            logger.warning(f"目标 Action 配置缺少提示词参数名，已忽略: {entry}")
            continue
        append_mode = (parts[2] if len(parts) > 2 else "").strip().lower()
        if append_mode not in {APPEND_MODE_NATURAL, APPEND_MODE_TAGS}:
            if append_mode:
                logger.warning(
                    f"未知追加模式 {append_mode!r}，按 {APPEND_MODE_NATURAL} 处理"
                )
            append_mode = APPEND_MODE_NATURAL
        mode_arg = parts[3] if len(parts) > 3 else ""
        detect_args: tuple[str, ...] = ()
        if len(parts) > 4:
            detect_args = tuple(
                name.strip()
                for name in parts[4].replace("，", ",").split(",")
                if name.strip()
            )
        characters_arg = parts[5] if len(parts) > 5 else ""
        targets.append(
            TargetSpec(
                plugin=plugin,
                action=action,
                prompt_arg=prompt_arg,
                append_mode=append_mode,
                mode_arg=mode_arg,
                detect_args=detect_args,
                characters_arg=characters_arg,
            )
        )
    return targets or list(_BUILTIN_TARGETS)


def _split_target_signature(signature: str) -> tuple[str, str]:
    """从 ``插件名:action:动作名`` 中拆出插件名与动作名。

    兼容三种形态：

    - ``插件名:action:动作名``：框架注册表给出的标准签名；
    - ``动作名``：只写了动作名的配置行；
    - ``插件名:action:``：发布方漏填动作名（例如只读 ``action_name`` 属性、
      而组件是用新版 ``name`` 声明的），此时动作名为空串，由调用方走参数名兜底。
    """
    raw = (signature or "").strip()
    if not raw:
        return "", ""
    parts = [part.strip() for part in raw.split(":")]
    if len(parts) >= 3:
        return parts[0], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


def _normalize_component_name(raw_name: str) -> str:
    """去掉工具名可能带的 ``action-`` / ``tool-`` 前缀。"""
    for prefix in (_TOOL_NAME_PREFIX, _COMPONENT_NAME_PREFIX):
        if raw_name.startswith(prefix):
            return raw_name[len(prefix) :]
    return raw_name


def resolve_stream_id(params: dict[str, Any]) -> str:
    """从事件参数里解析会话 ID（读上文必须靠它）。

    优先级：

    1. 发布方直接给的 ``params["stream_id"]``（本地补丁二版会带）；
    2. ``message.stream_id``（官方发布点只带 message）；
    3. ``message.chat_stream.stream_id`` / ``message.stream.stream_id``（个别构造方式）。

    agent 编排器自己发起工具调用时，``message`` 可能是 None，若发布方又没带
    ``stream_id``，这里只能返回空串 —— 此时读不到上文，插件会打 WARNING 说明，
    而不是静默地"提取不出角色"。
    """
    raw = str(params.get("stream_id") or "").strip()
    if raw:
        return raw
    message = params.get("message")
    if message is None:
        return ""
    for attr in ("stream_id", "chat_stream_id"):
        value = str(getattr(message, attr, "") or "").strip()
        if value:
            return value
    for holder in ("chat_stream", "stream"):
        value = str(
            getattr(getattr(message, holder, None), "stream_id", "") or ""
        ).strip()
        if value:
            return value
    return ""


def _enrich_from_roster(
    entry: CharacterEntry, roster: list[CharacterEntry]
) -> CharacterEntry:
    """用角色表补全 LLM 提取结果里缺失的字段（主要是游戏名与标准标签）。

    LLM 有时只给角色名，导致追加文案退化成"（画面角色：“爱莉希雅”）"——没有游戏名。
    角色表里若已有同一角色的条目，就把游戏名与标签借过来。

    注意不能直接用 ``entry_key`` 比较：它优先取标签，而 LLM 往往只给名字
    （键不同就匹配不上），所以这里按「标签 / 名字 / 别名」三个维度分别比对。
    """
    if not roster:
        return entry
    name = normalize_prompt_text(entry.name)
    tag = normalize_prompt_text(entry.tag)
    for known in roster:
        known_name = normalize_prompt_text(known.name)
        known_tag = normalize_prompt_text(known.tag)
        same = bool(tag and known_tag and tag == known_tag)
        if not same and name and known_name and name == known_name:
            same = True
        if not same and name:
            # 角色表里的别名与 LLM 给的名字对上（如 elysia ↔ 爱莉希雅）
            same = any(
                normalize_prompt_text(alias) == name for alias in known.aliases if alias
            )
        if not same and tag and known.name:
            same = tag == known_name
        if not same:
            continue
        return CharacterEntry(
            name=entry.name or known.name,
            game=known.game or entry.game,
            aliases=entry.aliases or known.aliases,
            tag=known.tag or entry.tag,
        )
    return entry


def _match_target_by_args(
    params: dict[str, Any], targets: list[TargetSpec], plugin: str
) -> TargetSpec | None:
    """动作名缺失时的兜底匹配：按「提示词参数名」唯一命中。

    典型场景是框架补丁只把旧的 ``action_name`` 属性写进事件，而组件是用新版
    ``name`` 声明的（如 image_generator_plugin-neo 的 ``draw_image``），于是
    ``action_name`` 与签名里的动作名都是空的。此时退一步：谁的提示词参数真的
    出现在 args 里，就认谁。

    多个目标共用同一个参数名是常态（nai_artist 1.3.6 的 ``share_photo`` /
    ``share_drawing`` 与旧版 ``share_visual`` 都用 ``content``），所以不能一见
    多个候选就放弃，按下面三步收窄，仍然无法区分时才宁可不处理：

    1. 带 ``mode_arg`` 的目标只有在 args 里真有这个参数时才可能是它（旧版
       nai_artist 的 ``share_visual`` 一定有 ``mode``）；
    2. 若仍剩多个，优先取"更具体"的那个（声明了 ``mode_arg`` 且 args 里存在）；
    3. 最后按「注入语义」去重：``prompt_arg`` / ``append_mode`` / ``mode_arg`` /
       ``characters_arg`` 完全一致的目标，补入行为没有区别，取配置里靠前的那个。
    """
    args = params.get("args")
    if not isinstance(args, dict):
        return None
    candidates = [
        spec
        for spec in targets
        if (not spec.plugin or not plugin or spec.plugin == plugin)
        and spec.prompt_arg in args
    ]
    # 第 1 步：声明的模式参数不存在 → 这个目标不可能是当前动作
    applicable = [
        spec for spec in candidates if not spec.mode_arg or spec.mode_arg in args
    ]
    if applicable:
        candidates = applicable
    # 第 2 步：优先"更具体"的目标（带 mode_arg 且 args 里确实有）
    specific = [spec for spec in candidates if spec.mode_arg and spec.mode_arg in args]
    if len(specific) == 1:
        candidates = specific
    if len(candidates) == 1:
        spec = candidates[0]
        logger.debug(
            f"事件未提供动作名，按提示词参数名 {spec.prompt_arg!r} 兜底匹配到 {spec.action}"
        )
        return spec
    # 第 3 步：注入语义完全等价的候选视为同一个，取靠前的
    if candidates:
        shapes = {
            (spec.prompt_arg, spec.append_mode, spec.mode_arg, spec.characters_arg)
            for spec in candidates
        }
        if len(shapes) == 1:
            spec = candidates[0]
            logger.debug(
                f"事件未提供动作名，参数名 {spec.prompt_arg!r} 命中多个语义等价目标 "
                f"{[item.action for item in candidates]}，取 {spec.action}"
            )
            return spec
    if len(candidates) > 1:
        logger.debug(
            f"事件未提供动作名，参数名匹配到多个目标 "
            f"{[spec.action for spec in candidates]}，无法唯一确定，跳过"
        )
    return None


def _match_target(
    params: dict[str, Any], targets: list[TargetSpec]
) -> TargetSpec | None:
    """判断当前事件命中了哪个目标 Action。

    组件若使用 ``name`` 声明动作名，事件的 ``action_name`` 可能为空，
    此时只能依赖 ``signature``，因此两者都要参与匹配。

    工具调用路径（agent 聊天器）给的是 ``tool_name``，且名字带 ``action-``
    前缀（如 ``action-share_visual``），这里一并归一化后再比对。
    """
    signature_plugin, signature_action = _split_target_signature(
        str(params.get("signature") or "")
    )
    signature_action = _normalize_component_name(signature_action)
    raw_name = _normalize_component_name(
        str(params.get("action_name") or params.get("tool_name") or "").strip()
    )
    action = signature_action or raw_name
    plugin = signature_plugin
    if not action:
        # 发布方漏填动作名：按参数名兜底，避免插件在 agent 路径下静默失效
        return _match_target_by_args(params, targets, plugin)
    for spec in targets:
        if spec.action != action:
            continue
        if spec.plugin and plugin and spec.plugin != plugin:
            continue
        if spec.plugin and not plugin:
            # 事件里没带插件名时只能按动作名匹配，可能撞上别插件的同名动作
            logger.debug(
                f"事件未提供插件名，按动作名匹配到 {spec.action}"
                f"（目标声明来自 {spec.plugin}）"
            )
        return spec
    return None


# 解析结果缓存：配置文本 → 解析结果。
# 角色表与目标 Action 表都是静态配置文本，而 before_action_call 每次 Action
# 调用都会触发，重复解析纯属浪费。以文本内容为键，配置一改文本就变、自动失效。
_PARSE_CACHE_LIMIT = 8
_targets_cache: dict[str, list[TargetSpec]] = {}
_roster_cache: dict[str, list[CharacterEntry]] = {}
#: 皮肤表（``[outfits] entries``）的解析缓存（1.0.22），与角色表同一套做法
_outfits_cache: dict[str, list[OutfitEntry]] = {}

# 历史消息缓存：stream_id → (写入时间, 消息列表)
_HISTORY_CACHE_LIMIT = 32
_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

# bot 自己的 sender_id 缓存：platform → (写入时间, bot_id 或 None)。
# 用来把「bot 的复述」和「用户的点名」分开：bot 说「好的，画爱莉希雅」不该
# 覆盖用户上一句刚改口的角色。解析不到时返回空集合，判定会退化成
# 「最近提及优先」（见 character.is_known_bot_message 的说明）。
_BOT_ID_CACHE_TTL = 60.0
_bot_id_cache: dict[str, tuple[float, str | None]] = {}

# 角色解析（LLM）结果的**调用方**缓存（1.0.21 修正）：**策略**在这里，**存储在**
# ``character``（``resolve_cache_get`` / ``resolve_cache_put`` / ``reset_resolve_cache``）。
#
# 为什么必须把「读缓存 / 写缓存」搬到调用方：解析函数是回归用例替换（打桩）的那一层
# ——``tests/check_logic.py`` 的 ``FakeResolve`` 同时替换 ``character`` 与 ``handler``
# 里的 ``resolve_character_via_llm``。缓存若写在被替换的那一层里，用例里第二次生图
# 照样会调模型（C75 断言第二次必须命中缓存、只问一次），等于没有缓存。
#
# 键走 ``character.normalize_prompt_text``（忽略大小写 / 反斜杠 / 下划线）；
# 成功缓存 24 小时、失败（unknown / 标签非法 / 超时 / 异常）负缓存 10 分钟，
# TTL 与上限常量、以及清空入口都在 ``character``（用例隔离只清一处）。


def _cached_parse(key_text: str, cache: dict[str, Any], factory: Any) -> Any:
    """按配置文本缓存解析结果。"""
    key = key_text or ""
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = factory(key)
    if len(cache) >= _PARSE_CACHE_LIMIT:
        cache.clear()
    cache[key] = result
    return result


def _outfit_entries(config: CharPromptGuardConfig) -> list[OutfitEntry]:
    """解析 ``outfits.entries`` 皮肤表（带配置文本缓存；1.0.23 起两处共用）。

    ``_scan_outfit_names``（识别，``execute`` 里先跑）与 ``_resolve_outfit``
    （解析）都要这张表：前者用它把「命中皮肤表的候选」升级成表里的**正式名**
    （表外点名豁免要用），后者用它查关键词。两处都走这里 → 同一份缓存，
    配置文本没变就不会重复解析。
    """
    section = getattr(config, "outfits", None)
    return _cached_parse(
        str(getattr(section, "entries", "") or ""), _outfits_cache, parse_outfits
    )


# ── 「本尊身份」解析（1.0.13）─────────────────────────────────────
#
# 自我指代（本尊 / 你本人 / 你自己 …）要的是「bot 本尊」，而插件看不到聊天器的
# 人设。1.0.12 的做法是一律不补；1.0.13 起给出两个**显式**来源：
#
#   ① ``general.true_self``（填角色表里的名字或别名）—— 填了就以它为准；
#   ② 框架人格配置的 ``personality.nickname``，其后 ``alias_names`` 逐个尝试。
#
# 命中角色表（名字 → 别名 → 标签）→ 正常注入该角色；没命中、或两个来源都取不到
# → 仍然**不补**（宁少勿多，绝不猜别的角色）。
#
# 明确不做的事：**不做外观识别**。不从「白发 / 粉眼 / 和服」这类画面关键词反推
# 角色 —— 误差不可控，角色表也保持现有的 4 列（名字 | 游戏名 | 别名 | 标签）。

#: 本尊身份来源：插件配置 ``general.true_self``
_TRUE_SELF_SOURCE_CONFIG = "配置"
#: 本尊身份来源：框架人格配置 ``personality.nickname`` / ``personality.alias_names``
_TRUE_SELF_SOURCE_PERSONALITY = "人格配置"

#: 读取 core 配置的模块路径（按顺序尝试）。
#:
#: ``src.core.config`` 是框架文档里的公开路径（``from src.core.config import
#: get_core_config``）；后者是它背后的实现模块，包 ``__init__`` 因为环境问题
#: 导入失败时仍有机会读到配置。
_CORE_CONFIG_MODULES: tuple[str, ...] = (
    "src.core.config",
    "src.core.config.core_config",
)


@dataclass(frozen=True)
class TrueSelfIdentity:
    """「本尊身份」解析结果（1.0.13）。

    Attributes:
        candidates: 候选名，按优先级排列（``general.true_self`` 只有 1 个；
            来自人格配置时是 ``nickname`` + 逐个 ``alias_names``），已去空白、去重
        source: 来源说明（:data:`_TRUE_SELF_SOURCE_CONFIG` /
            :data:`_TRUE_SELF_SOURCE_PERSONALITY`）；空串表示没取到任何身份名
    """

    candidates: list[str] = field(default_factory=list)
    source: str = ""


def _personality_section_from_plugin_api() -> Any | None:
    """尝试用框架的插件 API（``config_api``）读 core 的 ``personality`` 节。

    实测 ``src/app/plugin_system/api/config_api.py``（API_VERSION 1.0.0）**只提供
    插件配置**的加载与查询 —— ``load_config`` / ``reload_config`` / ``get_config`` /
    ``remove_config`` / ``get_loaded_plugins`` / ``initialize_all_configs``，
    **没有**读 core 配置（``config/core.toml``）的入口。所以这里只能试一下
    ``get_config("core")``：core 配置不经过 ConfigManager（它按插件名缓存），
    正常会返回 ``None``，此时由调用方退回 :func:`_personality_section_from_core_config`。

    整个函数用 ``importlib`` 动态导入 + try/except 包住：**任何失败都返回 None**，
    不抛异常，也不把 config_api 变成插件的硬依赖。
    """
    try:
        module = importlib.import_module("src.app.plugin_system.api.config_api")
    except Exception as e:  # noqa: BLE001 —— 拿不到就换下一条路径
        logger.debug(f"config_api 不可用，改读 core 配置对象: {e}")
        return None
    get_config = getattr(module, "get_config", None)
    if get_config is None:
        return None
    try:
        config = get_config("core")
    except Exception as e:  # noqa: BLE001 —— 同上，失败即换路径
        logger.debug(f"config_api.get_config('core') 调用失败: {e}")
        return None
    return getattr(config, "personality", None)


def _personality_section_from_core_config() -> Any | None:
    """直接读框架的 core 配置对象，取出 ``personality`` 节。

    走 ``src.core.config.get_core_config()``（core 配置未初始化时它抛
    ``RuntimeError``，被测/未启动运行时的环境就是这样）。同样用动态导入 +
    try/except 包住：**任何失败都返回 None**，只留一条 DEBUG 说明原因。
    """
    for module_name in _CORE_CONFIG_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception as e:  # noqa: BLE001 —— 该路径不可用就试下一个
            logger.debug(f"core 配置模块 {module_name} 不可用: {e}")
            continue
        get_core_config = getattr(module, "get_core_config", None)
        if get_core_config is None:
            continue
        try:
            config = get_core_config()
        except Exception as e:  # noqa: BLE001 —— 未初始化等情况：本次没有本尊身份
            logger.debug(f"读取 core 配置失败，本次按「未取到本尊身份」处理: {e}")
            return None
        section = getattr(config, "personality", None)
        if section is not None:
            return section
    return None


def _normalize_identity_names(raw: Any) -> list[str]:
    """把「本尊身份」候选名归一化成「去空白、去重、保序」的字符串列表。

    约定 :func:`_read_personality_identity` 返回 ``list[str]``；这里额外兼容裸字符串
    （以及元组/集合/None）：人格配置里 ``nickname`` 是字符串、``alias_names`` 是列表，
    不同核心版本或人工改写的配置文件都可能给成别的形状，**任何形状都不能让判定崩掉**。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        return []
    names: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in names:
            continue
        names.append(text)
    return names


def _read_personality_identity() -> list[str]:
    """读取框架人格配置里的「本尊身份」候选名（nickname → alias_names）。

    优先级与来源：

    1. 插件 API ``config_api``（见 :func:`_personality_section_from_plugin_api`，
       实测它没有 core 配置入口，正常取不到）；
    2. core 配置对象 ``personality.nickname``（框架默认值是 ``小狐狸``）；
    3. 同一个 ``personality`` 节的 ``alias_names``（逐个尝试）。

    ``nickname`` 为空、或只有 ``alias_names`` 时，仍然返回其中非空的那部分。

    **任何失败（模块不存在、配置未初始化、字段缺失、类型不对）都返回空列表**，
    绝不抛异常 —— 它只是「有没有本尊身份」的一个来源，失败时判定退回
    「不补 + 日志提示」，不影响生图。
    """
    for get_section in (
        _personality_section_from_plugin_api,
        _personality_section_from_core_config,
    ):
        try:
            section = get_section()
        except Exception as e:  # noqa: BLE001 —— 双保险：读取函数本身也不许抛
            logger.debug(f"读取人格配置失败: {e}")
            continue
        if section is None:
            continue
        names = _normalize_identity_names(getattr(section, "nickname", ""))
        for alias in _normalize_identity_names(getattr(section, "alias_names", None)):
            if alias not in names:
                names.append(alias)
        if names:
            logger.debug(f"人格配置提供本尊身份候选：{names}")
            return names
    return []


def resolve_true_self_identity(config: CharPromptGuardConfig) -> TrueSelfIdentity:
    """按优先级解析「本尊身份」候选名。

    优先级：``general.true_self``（非空）→ 框架人格配置 ``personality.nickname``
    → ``personality.alias_names``（逐个尝试）。

    本函数只负责「取名字」，**不判断名字在不在角色表里** —— 命中与否由
    :func:`character.find_roster_identity` 在调用处判断，日志里要能区分
    「命中并补入」与「不在角色表中，不补」两种情况。
    """
    configured = str(
        getattr(getattr(config, "general", None), "true_self", "") or ""
    ).strip()
    if configured:
        return TrueSelfIdentity(
            candidates=[configured], source=_TRUE_SELF_SOURCE_CONFIG
        )
    candidates = _normalize_identity_names(_read_personality_identity())
    if candidates:
        return TrueSelfIdentity(
            candidates=candidates, source=_TRUE_SELF_SOURCE_PERSONALITY
        )
    return TrueSelfIdentity()


# ── 记忆库关联（1.0.18）──────────────────────────────────────────────
#
# 判定链一行未改：记忆库只是「多一份证据」—— 只有判定**不确定**时才去读，
# 读到「触发词 → 角色表内角色」的明确对应就用作判定依据（并把这次确认写回），
# 否则静默降级回 1.0.17 的原链路（LLM 该问还是问，失败该不补还是不补）。

#: 判定链里「不确定」的四种情形（与主题跟踪的 status 同名）：
#: ``split`` 拆单 / ``unavailable`` 本尊身份取不到或代词无先行词 /
#: ``low_confidence`` 置信度不足 / ``empty`` 无信号（但仍可能要问 LLM）。
UNCERTAIN_SPLIT = "split"
UNCERTAIN_UNAVAILABLE = "unavailable"
UNCERTAIN_LOW_CONFIDENCE = "low_confidence"
UNCERTAIN_EMPTY = "empty"
UNCERTAIN_STATUSES: frozenset[str] = frozenset(
    {
        UNCERTAIN_SPLIT,
        UNCERTAIN_UNAVAILABLE,
        UNCERTAIN_LOW_CONFIDENCE,
        UNCERTAIN_EMPTY,
    }
)


def memory_uncertainty(track: SubjectTrack | None) -> str:
    """返回主题跟踪里「不确定」的类型；确定（``ok``）/ 没有跟踪结果时返回空串。

    只有这四种情形才去问记忆库；``ok``（主题可信）时**不查** —— 避免多一次网络
    请求，也避免记忆库里的旧结论压过用户刚说的话。
    """
    status = str(getattr(track, "status", "") or "")
    return status if status in UNCERTAIN_STATUSES else ""


def _memory_candidate_names(
    track: SubjectTrack | None,
    window: list[dict[str, Any]] | None,
    roster: list[CharacterEntry],
) -> list[str]:
    """本次上下文里的**候选名**（角色表里出现过的名字 / 别名）。

    只用「角色表里真的出现过」的词，宁少勿多：

    1. 主题跟踪刚提到的角色（``track.entries``，例如拆单里的「爱莉希雅」）；
    2. 最近若干条消息里被提及过的角色（:func:`character.find_characters_in_history`）。

    一个都没有时返回空列表 —— 那说明本次连候选都没有，记忆库也帮不上忙
    （``memory.build_keyword`` 会退回「触发词优先」的关键词，例如只有「本尊」那种）。

    .. note::
       **它只用于关键词构造**（``memory.build_keyword`` 的 ``roster_names`` 第 3 段：
       「这次上下文里出现的候选名」，帮助检索排序），**不再参与「触发词 → 角色」
       的对账** —— 对账必须用完整角色表，见 :func:`_roster_match_names`。
    """
    if not roster:
        return []
    entries: list[CharacterEntry] = list(getattr(track, "entries", None) or [])
    if window:
        entries = merge_entries(entries, find_characters_in_history(window, roster))
    names: list[str] = []
    for entry in entries:
        for keyword in entry.keywords():
            if keyword and keyword not in names:
                names.append(keyword)
    return names


def _conflicting_roster_entries(
    texts: list[str],
    roster: list[CharacterEntry],
    naming_scope: UserNamingScope,
    exclude: CharacterEntry | None = None,
) -> list[CharacterEntry]:
    """画面里「用户这次没点过」的角色表内角色（1.0.20 起的一致口径）。

    与 :meth:`CharacterGuardHandler._apply_outside_naming_check` 里的冲突判定
    **完全同一条规则**（1.0.21 抽成模块级函数，好让「该不该解析」这一步
    先按同一个口径预判，避免为一个根本用不上的解析去问模型）：

    - ``texts``：参与「已含角色」检测的文本（提示词 + ``detect_args``）；
    - ``roster``：角色表；
    - ``naming_scope``：用户最近几条用户消息 —— 里面**提过**的角色不算冲突
      （用户自己点的，画面里出现它是正常的）；
    - ``exclude``：解析出的角色（1.0.21）—— 刚替换进来/刚补入的那个不算冲突。
    """
    return [
        entry
        for entry in roster
        if character_in_texts(entry, texts)
        and not character_in_texts(entry, naming_scope.texts)
        and (exclude is None or entry_key(entry) != entry_key(exclude))
    ]


def _roster_match_names(roster: list[CharacterEntry]) -> list[str]:
    """**完整角色表**里所有可参与匹配的词：每个条目的 name + 全部别名 + 标准标签。

    去空白、去重（忽略大小写，与 ``character`` 的匹配口径一致）、保序（角色表顺序，
    条目内 name → 别名 → 标签）。空串一律不要。

    为什么对账必须用**整张**角色表，而不是「当前上下文里出现过的候选名」：
    记忆里存的对应关系（``角色指向：本尊 → 花火``）指向的角色**不一定出现在本次
    上下文里** —— 拆单场景里上文只提了「爱莉」，记忆里指向的却是「花火」。若只把
    候选名（爱莉希雅 / 爱莉 / elysia）交给 ``memory.attribute_items``，它的
    ``_roster_hits`` 在记忆文本里一个角色表关键词都找不到 → 返回 0 个命中 →
    整条记忆被判成 miss 并降级去问 LLM（**实测 C60 就是这么失败的**：日志里
    记忆库明明检索到「角色指向：本尊 → 花火」，status 却是 miss）。

    匹配口径本身不变（见 :func:`memory.attribute_items`）：同一条记忆里必须**同时**
    出现触发词、且**恰好一个**角色表内角色；出现多个**角色** → 整条作废（歧义不猜）。
    1.0.19 起「多个角色」按解析出的角色去重 —— 名字与它自己的别名子串（``爱莉希雅`` /
    ``爱莉``）只算一个，否则中文全名的记忆永远确认不了（详见 ``memory.attribute_items``）。
    """
    names: list[str] = []
    seen: set[str] = set()
    for entry in roster or []:
        for raw in (entry.name, *entry.aliases, entry.tag):
            text = str(raw or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(text)
    return names


def _memory_character_entry(character: memory_api.MemoryCharacter) -> CharacterEntry:
    """把记忆库确认出的角色转成判定链使用的 :class:`CharacterEntry`。

    两者字段一一对应，这里只做一次显式转换 —— 记忆库模块刻意不 import
    ``character``（避免循环依赖），所以转换落在调用方。
    """
    return CharacterEntry(
        name=character.name or character.tag,
        game=character.game,
        aliases=list(character.aliases),
        tag=character.tag,
    )


def _memory_name_lookup(roster: list[CharacterEntry]) -> Any:
    """造一个「关键词 → 角色」的查表函数（复用角色表自己的匹配规则）。

    走 :func:`character.find_roster_identity`（名字 → 别名 → 标签），因此
    ``hanabi \\(honkai: star rail\\)`` 与 ``hanabi (Honkai: star rail)`` 视为同一个。
    """

    def lookup(name: str) -> memory_api.MemoryCharacter | None:
        entry = find_roster_identity(name, roster)
        if entry is None:
            return None
        return memory_api.MemoryCharacter(
            name=entry.name,
            game=entry.game,
            aliases=tuple(entry.aliases),
            tag=entry.tag,
        )

    return lookup


@dataclass
class MemoryContext:
    """一次「记忆库再确认」的上下文（1.0.18，供调用方记录诊断信息）。

    Attributes:
        lookup: 记忆库检索 / 对账的结果
        mentioned: 主题跟踪窗口里被提及过的角色表内角色数（沿用 ``multi_only``
            的既有口径，只是给日志与 ``CandidateResult`` 用）
        reason: 触发这次再确认的具体原因（主题跟踪的 ``reason``）
    """

    lookup: memory_api.MemoryLookup
    mentioned: int = 0
    reason: str = ""


# ── 二次一致性检测的「期望集合」（1.0.19）────────────────────────────
#
# 1.0.10 起的一致性检测只把「判定集合」当基准：提示词里出现判定集合之外的角色表内
# 角色才告警。真实事故里判定集合恰好就是**描述里那个（错的）角色**（主题跟踪
# 不可用 → 判定集合为空；描述点名优先 → 直接放行），于是「画面 = 判定」永远自洽，
# 一次告警都不会有。1.0.19 把基准扩成**期望集合**：判定集合 + 主题跟踪给出的主题
# （``track.usable`` 时）+ 记忆库确认结果（明确对应时）——「这次该画谁」的每一份
# 证据都参与对账，画面里的角色只要落在期望集合之外就是冲突。


def _entry_label(entry: CharacterEntry) -> str:
    """角色在日志里的可读标签：``花火（hanabi \\(honkai: star rail\\)）``。

    没有标准标签时只写名字（或标签兜底），绝不编造游戏名。
    """
    name = entry.name or entry.tag or "?"
    tag = entry.resolved_tag()
    return f"{name}（{tag}）" if tag and tag != name else name


def _entry_names(entries: list[CharacterEntry]) -> str:
    """角色名列表：``花火、爱莉希雅``（没有名字时用标签兜底）。"""
    return "、".join(entry.name or entry.tag or "?" for entry in entries)


@dataclass(frozen=True)
class ExpectationSet:
    """一致性检测的对账**期望集合**（1.0.19）。

    Attributes:
        entries: 期望出现的角色（按来源顺序去重：判定集合 → 主题跟踪 → 记忆库确认）
        reasons: 每一份来源的可读说明（进 WARNING，用户据此核对「凭什么说该画她」）
    """

    entries: list[CharacterEntry] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def keys(self) -> set[str]:
        """期望集合的身份键（``entry_key``，与判定链同一口径）。"""
        return {entry_key(entry) for entry in self.entries}

    def names(self) -> str:
        """期望角色的可读列表。"""
        return _entry_names(self.entries)

    def reason_text(self) -> str:
        """来源说明：``判定集合=花火；主题跟踪=花火；记忆库确认:本尊 → 爱莉希雅（mem-1）``。"""
        return "；".join(self.reasons) or "无"

    def __bool__(self) -> bool:
        """非空（至少有一份证据）时为真。"""
        return bool(self.entries)


def build_expectation_set(
    *,
    decided: list[CharacterEntry] | None = None,
    track: SubjectTrack | None = None,
    memory: memory_api.MemoryLookup | None = None,
    roster: list[CharacterEntry] | None = None,
) -> ExpectationSet:
    """构造对账期望集合（1.0.19）。

    三份证据，顺序与「判定优先级」一致（描述点名的结果本来就在 ``decided`` 里）：

    1. **判定集合** ``decided`` —— 原样保留（1.0.10 起的老口径）；
    2. **主题跟踪** —— 只在 ``track.usable``（``status == "ok"`` 且主题非空）时算数：
       ``unavailable``（本尊身份取不到 / 代词无先行词）与 ``low_confidence``
       （置信度不足）都**不算**证据 —— 它们恰恰是「没有结论」，拿它们对账会把
       画面里正确的角色误判成冲突（1.0.19 特意划的边界）；
    3. **记忆库确认结果** —— 只在 ``lookup.hit``（「触发词 → 恰好一个角色表内角色」
       的明确对应）时算数；记忆条目里只是顺带提到某个角色（``miss``）不算，
       避免把模糊检索结果当成「该画她」的依据。

    记忆库里的角色名会先按「名字 → 别名 → 标签」在角色表里定位（:func:`find_roster_identity`），
    定位不到时退回记忆库自己给的字段 —— 与 :meth:`CharacterGuardHandler._memory_verdict`
    同一口径，保证日志里的名字与注入时用的名字一致。

    返回空集合（``entries`` 为空）表示**没有任何可对账的依据**，调用方应跳过检查：
    那种情况下提示词里的角色是描述自己写的，报警只会是噪音。
    """
    entries: list[CharacterEntry] = []
    reasons: list[str] = []

    def add(entry: CharacterEntry | None) -> None:
        if entry is None:
            return
        key = entry_key(entry)
        if any(entry_key(known) == key for known in entries):
            return
        entries.append(entry)

    known_decided = list(decided or [])
    for entry in known_decided:
        add(entry)
    if known_decided:
        reasons.append(f"判定集合={_entry_names(known_decided)}")

    if track is not None and track.usable and track.entries:
        for entry in track.entries:
            add(entry)
        reasons.append(f"主题跟踪={track.subject_names()}")

    if memory is not None and memory.hit and memory.characters:
        confirmed: list[CharacterEntry] = []
        for character in memory.characters:
            entry = find_roster_identity(character.name or character.tag, roster or [])
            if entry is None:
                entry = _memory_character_entry(character)
            add(entry)
            confirmed.append(entry)
        if confirmed:
            reasons.append(memory.confirm_log())

    return ExpectationSet(entries=entries, reasons=reasons)


# ── 「用户点名了角色表外角色」的冲突检测（1.0.20）──────────────────────
#
# 与上面那套「期望集合对账」**完全独立**：期望集合对账在「判定 / 主题跟踪 /
# 记忆库」三份证据都为空时直接跳过，而实测事故恰恰就是这种情况 —— 用户点名的
# 角色**不在角色表里**（「刻律德拉」），所以三份证据全空，画面里挂着的却是
# 角色表内的「花火」标签。这条判断不看那三份证据，只看：
#   ① 用户最近说过的话里有没有「角色表外」的名字（X）；
#   ② 最终提示词里有没有角色表内角色（Y）；
#   ③ 用户这批消息里**没有**提过 Y（用户这次没点它）。
# 三者同时成立才算冲突。


@dataclass(frozen=True)
class UserNamingScope:
    """一次「用户点名扫描」的结果（1.0.20）。

    Attributes:
        texts: 参与扫描的用户消息正文（**触发消息在前**，其后由新到旧）；
            冲突判断里的「用户这次提过 Y 吗」查的就是这一份
        scanned: 触发之前实际回看的用户消息条数（触发消息不算在内）
        names: 从这些消息里提取出的**表外**名字（已过滤：长度 / 纯标点 /
            常见词黑名单 / 角色表内的名字与别名），去重、保序
        sources: 每个表外名字**来自哪条消息**（与 ``names`` 一一对应，1.0.21 起）：
            解析角色时用它当「触发语」写回记忆库（「这身是？」比裸名字信息更多）
    """

    texts: list[str] = field(default_factory=list)
    scanned: int = 0
    names: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)

    def names_text(self) -> str:
        """表外名字的可读列表（进日志）：``刻律德拉、卡芙卡``。"""
        return "、".join(self.names) if self.names else "无"

    def label(self) -> str:
        """一行式诊断文案（DEBUG 日志用）。"""
        return (
            f"表外点名={self.names_text()} "
            f"（焦点范围=触发消息+触发前最近 {self.scanned} 条用户消息，"
            f"共 {len(self.texts)} 条用户消息）"
        )


# ── 皮肤/时装（1.0.22）────────────────────────────────────────────────
#
# 需求原话：「当说『我想看看你的 xxxx（皮肤）』，可以搜索并加进去」——
# **角色那一侧一个字都没改**（描述点名 > 主题跟踪 > LLM 仲裁 > 不补），
# 这里只是多出「皮肤/时装」这一维：
#
#   ① 识别（``_scan_outfit_names`` + ``outfit.extract_outfit_names``）：
#      只在**用户消息**里找（触发消息 + 触发前最近 N 条用户消息，bot 消息跳过；
#      与表外点名 / 自我指代同一个焦点范围），**不扫画面描述**；
#   ② 解析（``_resolve_outfit``）：皮肤表命中 → 零模型调用；表未命中且
#      ``outfits.auto_resolve`` → 问一次模型（``outfit.resolve_outfit_via_llm``，
#      与角色解析同一套 llm_api / translate_model / llm_timeout），失败**什么都不注入**；
#   ③ 注入（``_apply_outfit``，在 ``_finalize`` 的最后一步）：
#      按目标的 natural / tags 模式把关键词**追加**在角色之后。
#
# 为什么注入放在 ``_finalize`` 的最后（两条一致性检查**之后**）：皮肤关键词是
# 「多一层外观」，它不该参与角色对账（那两条检查只认角色表内的字符串），
# 也不该被任何角色修正逻辑顺手删掉 —— 追加在最后既保证「角色在前、皮肤在后」，
# 又保证「角色链路失败/修正时皮肤那条链路一个字都不受影响」。

#: natural 模式追加的皮肤说明模板（皮肤名 + 关键词必须都在里面；中文描述可选）
OUTFIT_NATURAL_TEMPLATE = "（皮肤/时装：“{name}”：{keywords}）"
OUTFIT_NATURAL_TEMPLATE_WITH_DESC = "（皮肤/时装：“{name}”：{keywords}；{description}）"


@dataclass(frozen=True)
class OutfitScope:
    """一次「皮肤/时装名扫描」的结果（1.0.22）。

    Attributes:
        texts: 参与扫描的用户消息正文（**触发消息在前**，其后由新到旧）；
            角色线索（给模型当提示、也是缓存键的一半）就是从这一份里取的
        name: 选中的皮肤名（空串表示这次没有皮肤意图）
        source: 这个皮肤名**来自哪条消息**（给模型当上下文线索，也让日志可核对）
        names: **本次识别到的全部**皮肤 / 时装名（1.0.23）—— 句式 / 引号抓到的候选
            加上它们命中皮肤表时的**正式名**，去重保序。它不进日志、不参与解析，
            只用来把「这些是皮肤，不是角色表外的角色名」告诉表外点名那条链路
            （见 :meth:`CharacterGuardHandler._scan_user_naming` 的 ``exclude``）
    """

    texts: list[str] = field(default_factory=list)
    name: str = ""
    source: str = ""
    names: list[str] = field(default_factory=list)

    def label(self) -> str:
        """一行式诊断文案（DEBUG 日志用）。"""
        if not self.name:
            return f"皮肤/时装=无（共扫描 {len(self.texts)} 条用户消息）"
        return (
            f"皮肤/时装={self.name}（来源={self.source[:30]!r}，"
            f"共扫描 {len(self.texts)} 条用户消息）"
        )


class CharacterGuardHandler(BaseEventHandler):
    """生图前角色提示词检测与补全处理器。"""

    name: str = "char_prompt_guard"
    description: str = (
        "在生图 Action 执行前检测角色提示词：已含角色则放行，"
        "否则根据上文补入角色名+游戏名（或 NAI 角色标签），支持多角色同框"
    )
    weight: int = 100
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = _SUBSCRIBE_EVENTS
    # LLM 提取可能较慢，禁用订阅者级超时保护
    timeout: float = 0.0

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 before_action_call / before_tool_call 事件。

        另外订阅了「全部插件加载完成」（1.0.17），只借用那个时机做一次参数说明注入
        重试：本插件 dependencies 为空，拓扑排序下常排在生图插件前面，加载时目标
        动作可能还没注册（详见 handler 模块顶部对注入时机的说明）。
        """
        config = self._config()
        if not config.general.enabled or not config.targets.enabled:
            return EventDecision.SUCCESS, params

        # 1.0.17：把「标签写在最前面」追加进生图动作的**参数说明**（纯内存改动）。
        # 插件加载时（on_plugin_loaded）已经试过一次；本插件 dependencies 为空，
        # 拓扑排序（Kahn + 名称字典序）下通常排在 nai_artist /
        # image_generator_plugin-neo **前面**，那一次多半"目标动作还没注册"。
        # 所以这里再给一次重试机会（先到者触发）：
        #   · on_all_plugin_loaded（全部插件加载完成，动作一定已注册）—— 正常就是它；
        #   · 首个事件 —— 兜底（核心不发上面那个事件时）。
        # 一次性标志位在 tag_hint 内部：跑过一次之后每次事件都是常数级空操作；
        # **与下面的判定链完全无关**，所以再加一层 try/except 兜底 ——
        # 这里任何失败都只记 DEBUG，绝不拦住生图。
        try:
            tag_hint.maybe_inject_on_event(_BUILTIN_TARGETS, config=config)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                f"注入参数说明失败（已忽略，不影响生图）: {type(e).__name__}: {e}"
            )

        if _event_value(event_name) == _event_value(_ALL_PLUGIN_LOADED_EVENT):
            # 生命周期事件：只借用这个时机做上面的注入重试。判定链只对生图事件
            # 有意义（这个事件的 params 里没有 args / signature），直接放行。
            return EventDecision.SUCCESS, params

        targets = _cached_parse(config.targets.list, _targets_cache, parse_targets)
        spec = _match_target(params, targets)
        if spec is None:
            # 未命中目标不是异常，但静默会让「插件装了却没反应」无从排查，
            # 所以这里留一条 DEBUG：把它和「命中了但放行」区分开。
            logger.debug(
                "未命中任何目标动作，放行"
                f"（签名={params.get('signature')!r}, "
                f"动作名={(params.get('action_name') or params.get('tool_name'))!r}, "
                f"已配置目标={[item.action for item in targets]}）"
            )
            return EventDecision.SUCCESS, params

        args = params.get("args")
        if not isinstance(args, dict):
            return EventDecision.SUCCESS, params

        raw_content = args.get(spec.prompt_arg)
        if not isinstance(raw_content, str) or not raw_content.strip():
            if spec.prompt_arg not in args:
                # 目标匹配上了，但配置里声明的参数名不在本次调用里 —— 多半是
                # [targets] 配置写错了（例如把 neo 的 content_description 填给了
                # nai_artist）。这里只告警不写回，避免给动作塞进未知参数导致报错。
                logger.warning(
                    f"目标 {spec.action} 声明的提示词参数 {spec.prompt_arg!r} 不在本次调用参数里"
                    f"（实际参数: {sorted(args)}），本次放行不补角色；请检查 [targets] 配置"
                )
            return EventDecision.SUCCESS, params

        if spec.mode_arg and not self._mode_allowed(args.get(spec.mode_arg), config):
            logger.debug(f"模式 {args.get(spec.mode_arg)} 不在生效范围，放行")
            return EventDecision.SUCCESS, params

        # 主模型运行时指令（标记会从提示词里移除）
        control, content = self._extract_control(raw_content, config)
        if control.nochar:
            if content != raw_content:
                args[spec.prompt_arg] = content
                params["args"] = args
            logger.info("主模型标记 #nochar#，本次不补角色")
            return EventDecision.SUCCESS, params

        roster = (
            _cached_parse(config.roster.characters, _roster_cache, parse_roster)
            if config.roster.enabled
            else []
        )
        # 1.0.22：皮肤/时装（「我想看你的粉色妖精小姐」）—— **与角色那一侧完全解耦**：
        # 只解析出「这次要穿哪一套」并带着走，注入统一放在 _finalize 的最后一步
        # （两条一致性检查之后），角色判定链一个字都不改。
        # [outfits] enabled=false 时本调用**立刻返回**（不读上文、不调模型、不打日志）。
        #
        # 1.0.23：**识别提前到「表外点名」扫描之前** —— 皮肤名要从表外点名链路里
        # 豁免（见 _scan_user_naming 的 exclude），所以「这次识别到了哪些皮肤名」
        # 必须先有结论。改的只是识别顺序：注入位置（_finalize 最后一步）与
        # 「角色在前、皮肤在后」的语义一个字都没变。
        outfit_scope = await self._scan_outfit_names(
            params=params, config=config, roster=roster
        )
        outfit = await self._resolve_outfit(
            params=params, config=config, roster=roster, scope=outfit_scope
        )
        # 1.0.20：「用户点名了角色表外角色」这条判断要看**用户最近说过什么**，
        # 所以先把焦点范围内的用户消息扫一遍（触发消息 + 触发前最近 N 条用户消息，
        # 只取用户消息）。consistency_check="off" 时**一次都不扫**（连上文都不读），
        # 与「off=完全不检查」的语义一致。结果只用于 _finalize 里那条冲突检测，
        # 不参与判定链 —— 扫描本身失败也只记 DEBUG、返回空结果。
        # 1.0.23：本次识别出的皮肤/时装名（outfit_scope.names）一并传进去**豁免** ——
        # 它们是皮肤，不是角色表外的角色名（用户说「画粉色妖精小姐这套」时，
        # 不该为「粉色妖精小姐」多问一次模型，更不该让 fix 去动画面里的角色标签）。
        user_naming = await self._scan_user_naming(
            params=params,
            config=config,
            roster=roster,
            exclude=outfit_scope.names,
        )
        texts = self._detection_texts(content, spec, args)
        # 1.0.21：把用户点名的表外角色**解析**一次（复用 llm_api，结果带缓存），
        # 只对**第一个**表外名字解析，避免一次生图打好几次模型。
        # 只有「两个入口之一真的用得上」时才问（见 _resolve_outside_character）：
        # 角色在角色表内的普通场景**一次模型都不问**。
        # 解析失败（unknown / 超时 / 标签非法 / 开关关掉）返回 None —— 一律退回
        # 1.0.20 的行为，绝不拦住生图。
        resolved = await self._resolve_outside_character(
            config=config,
            naming_scope=user_naming,
            texts=texts,
            roster=roster,
        )
        present_keys = {
            entry_key(entry) for entry in roster if character_in_texts(entry, texts)
        }

        # 1.0.21 入口②（**补入**）：用户点名了表外角色、解析成功，而画面里
        # **一个角色标签都没有**（典型：描述只写了外观）→ 按目标的 natural / tags
        # 模式把解析结果补进提示词最前面，然后走**既有注入路径**（不另写一套）。
        # 放在判定链**之前**是有意的：解析结果是模型给出的确定结论，比按粘性从
        # 上万里挑一个角色更可信；此时 present_keys 必为空，所以只要解析成功就补。
        # 画面里已经有角色标签时不在这里动 —— 那是「替换」那一条入口的事
        # （见 _apply_outside_naming_check）。
        injected = self._resolved_for_injection(
            content=content,
            spec=spec,
            args=args,
            roster=roster,
            user_naming=user_naming,
            resolved=resolved,
        )
        if injected is not None:
            self._inject_resolved(
                content=content,
                spec=spec,
                args=args,
                resolved=injected,
                config=config,
            )
            logger.info(
                f"已按角色解析补入（LLM）：{injected.name} → {injected.resolved_tag()}"
            )
            return await self._finalize(
                params=params,
                args=args,
                spec=spec,
                original=raw_content,
                final_content=args[spec.prompt_arg],
                decided=[injected],
                roster=roster,
                config=config,
                basis=f"判定依据=角色解析(LLM):{injected.name}",
                track=None,
                memory=None,
                naming_scope=user_naming,
                resolved=injected,
                outfit=outfit,
            )

        # 1.0.18：判定**不确定**且描述里没有角色表内角色时，先去外部记忆库
        # （Booku Memory）做一次「再确认」。命中「触发词 → 角色表内角色」的明确
        # 对应就直接用作判定依据（并写回记忆库），否则**静默降级**回 1.0.17 的
        # 原链路（记忆库模块自己不抛异常、失败只打 DEBUG）。
        #
        # 这里放在 _collect_candidates **之前**是有意的：命中时省掉一次 LLM 调用
        # （确定性记忆比模型猜测更可信），命中不了时 _collect_candidates 会照旧
        # 把命中片段的 title + snippet 作为参考材料喂给 LLM。
        description_characters = [
            entry for entry in roster if character_in_texts(entry, [content])
        ]
        description_has_character = bool(description_characters)
        memory_context = await self._memory_lookup(
            params=params,
            config=config,
            roster=roster,
            content=content,
            description_has_character=description_has_character,
        )
        memory_lookup = memory_context.lookup if memory_context is not None else None
        memory_verdict = self._memory_verdict(memory_context, roster)
        # 1.0.19：描述里**已经点名**角色表内角色时，另做一次「**对账用**」的记忆库
        # 查询（与上面那条「不确定才查」互斥：_memory_lookup 在描述点名时直接返回
        # None）。它的结果只喂给 _apply_consistency_check 的对账，
        # **不进判定链、不替换描述点名的角色、不写回**（write_back=False）。
        reconcile_lookup: memory_api.MemoryLookup | None = None
        if description_has_character:
            reconcile_lookup = await self._memory_reconcile(
                config=config,
                roster=roster,
                content=content,
                described=description_characters,
            )
        if memory_verdict is not None:
            # 记忆库给了明确结论 —— 直接走既有注入路径（不额外问 LLM）
            result = memory_verdict
        else:
            # 候选角色：用户点名/粘性 → LLM 仲裁（必要时），记忆库命中片段作为参考材料
            result = await self._collect_candidates(
                params,
                config,
                roster,
                content,
                texts,
                memory_lookup=memory_lookup,
            )
        candidates = result.candidates

        # 标签补全：描述里已经点名了角色表内的角色、但缺少该角色的标准标签时补标签。
        # 与"要不要新补一个角色"无关，所以放在最前面独立处理。
        # 这是实测逼出来的：描述里写着"花火"，下游 nai_artist 的翻译模型却把它翻成
        # ``hanabi (firefly) (honkai: star rail)``（括号未转义 + 串了另一个角色），
        # 补上转义正确的标准标签后，NAI 才能稳定锁定角色。
        completion_tags = self._completion_tags(content, roster, config)

        multi = self._resolve_multi(
            config,
            control,
            candidates,
            result.llm_multi,
            result.mentioned,
            result.naming.multi,
        )
        if not multi:
            # 单角色路径：沿用"命中任一角色即整体放行"
            if present_keys:
                final_content = (
                    self._complete(spec, content, completion_tags)
                    if completion_tags
                    else content
                )
                if not completion_tags:
                    logger.debug("提示词已包含角色，放行")
                return await self._finalize(
                    params=params,
                    args=args,
                    spec=spec,
                    original=raw_content,
                    final_content=final_content,
                    decided=result.decided,
                    roster=roster,
                    config=config,
                    basis=result.basis,
                    track=result.track,
                    memory=reconcile_lookup,
                    naming_scope=user_naming,
                    resolved=resolved,
                    outfit=outfit,
                )
            target_characters = candidates[:1]
        else:
            target_characters = [
                entry for entry in candidates if entry_key(entry) not in present_keys
            ][: max(1, int(config.multi.max_characters))]

        if not target_characters:
            final_content = (
                self._complete(spec, content, completion_tags)
                if completion_tags
                else content
            )
            if not completion_tags:
                logger.debug("未识别到需要补入的角色，放行")
            return await self._finalize(
                params=params,
                args=args,
                spec=spec,
                original=raw_content,
                final_content=final_content,
                decided=result.decided,
                roster=roster,
                config=config,
                basis=result.basis,
                track=result.track,
                memory=reconcile_lookup,
                naming_scope=user_naming,
                resolved=resolved,
                outfit=outfit,
            )

        new_content = self._apply(
            args=args,
            spec=spec,
            content=content,
            characters=target_characters,
            config=config,
            control=control,
        )
        if new_content is None:
            return await self._finalize(
                params=params,
                args=args,
                spec=spec,
                original=raw_content,
                final_content=content,
                decided=result.decided,
                roster=roster,
                config=config,
                basis=result.basis,
                track=result.track,
                memory=reconcile_lookup,
                naming_scope=user_naming,
                resolved=resolved,
                outfit=outfit,
            )

        names = ", ".join(entry.name or entry.tag or "?" for entry in target_characters)
        # 把"追加了什么"直接写进日志：用户最容易困惑的就是"到底补了谁、有没有游戏名"
        appended = new_content[len(content) :].strip()
        logger.info(
            f"已补入角色（{spec.action} / {spec.append_mode}"
            f"{' / 多人' if multi else ''}）: {names}"
            f"{f' → 追加 {appended!r}' if appended else ''}"
        )
        return await self._finalize(
            params=params,
            args=args,
            spec=spec,
            original=raw_content,
            final_content=new_content,
            decided=result.decided,
            roster=roster,
            config=config,
            basis=result.basis,
            track=result.track,
            memory=reconcile_lookup,
            naming_scope=user_naming,
            resolved=resolved,
            outfit=outfit,
        )

    # ── 记忆库再确认（1.0.18）─────────────────────────────────────

    async def _memory_lookup(
        self,
        *,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        content: str,
        description_has_character: bool,
    ) -> MemoryContext | None:
        """判定不确定时去记忆库做一次「再确认」；不需要查时返回 ``None``。

        触发条件（任一，见 :func:`memory_uncertainty`）：主题跟踪的 ``split`` /
        ``unavailable`` / ``low_confidence`` / ``empty``。另外还要求
        「画面描述里没有角色表内角色」—— 描述已经点名时判定并不不确定，
        没必要在这里多查一次（也避免记忆库的旧结论压过描述）。
        **1.0.19 起**这种情况改由 :meth:`_memory_reconcile` 做一次**对账用**查询
        （用途完全不同：只喂给二次一致性检测，不进判定链、不写回）。

        **本函数不抛异常**：记忆库的任何问题都只让 ``lookup.hit`` 为假，
        由调用方走 1.0.17 的原链路。
        """
        if not memory_api.memory_enabled(config) or not roster:
            return None
        if description_has_character:
            return None
        try:
            monitor = getattr(config, "monitor", None)
            # 与 _collect_candidates 里那段同一套开关判断：monitor 关掉（或配置对象
            # 形状不认识）时主题跟踪返回不了结论，这里同样不去打扰记忆库。
            if not bool(getattr(monitor, "enabled", False)):
                return None
            messages = await self._collect_context(params, config)
            window_size = max(1, int(getattr(monitor, "window", 15) or 15))
            window = messages[-window_size:]
            track = track_subject(
                messages,
                roster,
                window=window_size,
                min_confidence=float(getattr(monitor, "min_confidence", 0.5) or 0.5),
                half_life_minutes=float(
                    getattr(monitor, "half_life_minutes", 45.0) or 45.0
                ),
                multi_window_minutes=float(config.multi.multi_window_minutes),
                self_reference_enabled=(
                    bool(config.general.skip_on_self_reference)
                    and not description_has_character
                ),
                bot_ids=await self._resolve_bot_sender_ids(window),
                resolve_identity=lambda: resolve_true_self_identity(config),
            )
            uncertainty = memory_uncertainty(track)
            if not uncertainty:
                return None
            mentioned = len(find_characters_in_history(window, roster))
            # 对账用**完整角色表**（name + 别名 + 标准标签），不能用「当前上下文候选名」：
            # 记忆里指向的角色可能压根不在本次上下文里（实测 C60：上文只提「爱莉」，
            # 记忆里却是「本尊 → 花火」，只传候选名时 _roster_hits 命中 0 个 → 永远 miss）。
            # candidate_names（上下文候选名）只用于关键词构造，见 memory.build_keyword 第 3 段。
            roster_names = _roster_match_names(roster)
            result = await memory_api.lookup(
                config,
                uncertainty=uncertainty,
                reason=track.reason or track.basis,
                description=content,
                candidate_names=_memory_candidate_names(track, window, roster),
                roster_names=roster_names,
                signal_label=track.signal_label,
                name_lookup=_memory_name_lookup(roster),
            )
            logger.debug(
                f"记忆库再确认（不确定类型={uncertainty}）: status={result.status}"
                f"{f'，{result.reason}' if result.reason else ''}"
            )
            return MemoryContext(
                lookup=result, mentioned=mentioned, reason=track.reason or track.basis
            )
        except Exception as e:  # noqa: BLE001 —— 记忆库是「多一份证据」，不许拦住生图
            logger.debug(
                f"记忆库不可用，跳过（记忆库再确认失败: {type(e).__name__}: {e}）"
            )
            return None

    async def _memory_reconcile(
        self,
        *,
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        content: str,
        described: list[CharacterEntry],
    ) -> memory_api.MemoryLookup | None:
        """**对账用**的记忆库查询（1.0.19）：描述里已经点名角色时才能跑。

        背景（真实事故）：调用方把**过期备忘**里的固定标签（``Hanabi``）写进了画面
        描述，而这次真正要画的是爱莉希雅。1.0.18 的判定链一见「描述里有角色表内
        角色」就放行，连记忆库都不查，于是没有任何对账。所以这里补一次查询 ——
        关键词优先用**描述里那个角色的名字 / 别名**（``candidate_names``，帮助检索到
        「这次说的人」相关的记忆）加**自我指代词**（``memory.build_keyword`` 的
        ``_PRIMARY_KEYWORDS`` 一定会带上 ``本尊 / 扮演 / 口吻``；触发词取不到时
        对账用的触发词回落到 ``本尊``），``limit`` 用 ``memory.limit`` 配置值。

        触发条件（全部满足才查）：``memory.enabled``、角色表非空、
        ``general.consistency_check != "off"``、描述里确实有角色表内角色。
        ``off`` 时**一次请求都不发** —— 「完全不检查」应当包含不去查记忆库。

        结果**只**用于 :meth:`_apply_consistency_check` 的期望集合：

        - **不参与判定**（绝不替换 / 覆盖描述点名的角色，除非 ``fix`` 且满足
          「期望集合恰好 1 人 + 画面里冲突角色恰好 1 个」的唯一性条件）；
        - **不写回**（``write_back=False``）—— 写回语义仍是「本次判定确实依赖了
          记忆库」，对账查询不改变判定；
        - 失败 / 超时 / 非 200 / JSON 坏 / 结构不符 → 照旧静默降级（DEBUG
          ``记忆库不可用，跳过（…）``，沿用 1.0.18 的失败退避），**绝不拦住生图**。

        **本函数不抛异常**（与 :meth:`_memory_lookup` 一样，外层再包一层兜底）。
        """
        if not described or not roster:
            return None
        mode = (
            str(getattr(config.general, "consistency_check", "") or "").strip().lower()
        )
        if mode == "off":
            # consistency_check=off：完全不检查，含不去查记忆库（不发任何请求）
            return None
        if not memory_api.memory_enabled(config):
            return None
        try:
            # 关键词用的候选名：描述里那个角色的名字 / 别名（宁少勿多，不带标签 ——
            # 标签又长又含转义符，塞进 60 字的关键词预算里只会挤掉更有用的词）。
            candidate_names: list[str] = []
            for entry in described:
                for keyword in entry.keywords():
                    if keyword and keyword not in candidate_names:
                        candidate_names.append(keyword)
            described_names = _entry_names(described)
            reason = f"描述点名对账（画面里是：{described_names}）"
            result = await memory_api.lookup(
                config,
                uncertainty=memory_api.RECONCILE_UNCERTAINTY,
                reason=reason,
                description=content,
                # 关键词优先用描述里那个角色的名字 / 别名 + 自我指代词
                candidate_names=candidate_names,
                # 对账仍用**完整角色表**（与 _memory_lookup 同一口径）：
                # 记忆里指向的角色往往不在本次上下文候选里
                roster_names=_roster_match_names(roster),
                signal_label=reason,
                name_lookup=_memory_name_lookup(roster),
                # 对账查询绝不写回：它不参与判定，写回语义留给「不确定再确认」那条路径
                write_back=False,
            )
            logger.debug(
                f"记忆库对账查询（画面点名={described_names}）: status={result.status}"
                f"{f'，{result.reason}' if result.reason else ''}"
            )
            return result
        except Exception as e:  # noqa: BLE001 —— 对账只是「多一份证据」，不许拦住生图
            logger.debug(
                f"记忆库不可用，跳过（记忆库对账查询失败: {type(e).__name__}: {e}）"
            )
            return None

    @staticmethod
    def _memory_verdict(
        context: MemoryContext | None,
        roster: list[CharacterEntry],
    ) -> CandidateResult | None:
        """把「记忆库命中」转成判定结果；没有明确结论时返回 ``None``。

        命中时只采纳**角色表内**的角色（记忆库里的名字要对得上角色表才能带着
        名字 / 游戏名 / 标准标签一起注入），并打 INFO 日志
        ``记忆库确认:本尊 → 花火（mem-xxxx）``，判定依据写成
        ``判定依据=记忆库再确认:本尊 → 花火（mem-xxxx）`` —— 与其它判定依据一样，
        用户对着日志就能看出「这次为什么补了这个人」。
        """
        if context is None or not context.lookup.hit:
            return None
        lookup = context.lookup
        decided: list[CharacterEntry] = []
        for character in lookup.characters:
            entry = find_roster_identity(character.name or character.tag, roster)
            if entry is None:
                entry = _memory_character_entry(character)
            if any(entry_key(entry) == entry_key(known) for known in decided):
                continue
            decided.append(entry)
        if not decided:
            return None
        logger.info(f"{lookup.confirm_log()}（判定依据=记忆库再确认，来自记忆库）")
        naming = NamingDecision(
            kind="multi" if len(decided) > 1 else "single",
            characters=list(decided),
            basis=BASIS_LATEST_MENTION,
            note=f"记忆库再确认（{lookup.confirm_log()}）",
        )
        return CandidateResult(
            candidates=list(decided),
            decided=list(decided),
            llm_multi=len(decided) > 1,
            mentioned=context.mentioned,
            naming=naming,
            basis=f"判定依据=记忆库再确认:{lookup.confirm_log()}",
            memory=lookup,
        )

    # ── 配置与检测辅助 ────────────────────────────────────────────

    def _config(self) -> CharPromptGuardConfig:
        """获取插件配置，缺失时回退默认配置。"""
        config = self.plugin.config
        if isinstance(config, CharPromptGuardConfig):
            return config
        return CharPromptGuardConfig()

    @staticmethod
    def _extract_control(
        content: str, config: CharPromptGuardConfig
    ) -> tuple[BotControl, str]:
        """解析并移除主模型标记，返回指令与清理后的提示词。

        同一类标记同时出现时按 :data:`_MARKER_PRIORITY` 取一个并打 WARN，
        而不是"谁写在后面谁生效"。
        """
        control = BotControl()
        cleaned = content
        if config.multi.bot_control:
            found = _MARKER_PATTERN.findall(content)
            if found:
                markers = [marker.lower() for marker in found]
                control.markers_found = markers

                multi_markers = [m for m in markers if m in _MARKER_MULTI_GROUP]
                strategy_markers = [m for m in markers if m in _MARKER_STRATEGY_GROUP]
                if len(multi_markers) > 1 or len(strategy_markers) > 1:
                    logger.warning(f"检测到互相冲突的标记 {markers}，按固定优先级处理")

                if multi_markers:
                    winner = max(multi_markers, key=lambda m: _MARKER_PRIORITY[m])
                    control.force_multi = winner == MARKER_MULTI
                if strategy_markers:
                    winner = max(strategy_markers, key=lambda m: _MARKER_PRIORITY[m])
                    control.force_strategy = _MARKER_STRATEGY[winner]
                if MARKER_NOCHAR in markers:
                    control.nochar = True

                cleaned = _MARKER_PATTERN.sub(" ", content)
                cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
                if not cleaned:
                    # 标记误写导致内容为空时保留原文，避免把画面清空
                    cleaned = content
                    control.nochar = False
        return control, cleaned

    @staticmethod
    def _detection_texts(
        content: str, spec: TargetSpec, args: dict[str, Any]
    ) -> list[str]:
        """汇总用于"是否已含角色"判断的文本。"""
        texts = [content]
        for arg_name in spec.detect_args:
            extra = args.get(arg_name)
            if isinstance(extra, str) and extra.strip():
                texts.append(extra)
        return texts

    @staticmethod
    def _trigger_text(params: dict[str, Any]) -> str:
        """取出触发本次生图的那条消息正文（没有 message 时为空串）。

        ``_collect_context`` 与「自我指代」扫描都要用同一份文本，故抽成一处：
        两处若各取一次，很容易出现「收集上文时用了 A、判定时看了 B」的偏差。
        """
        message = params.get("message")
        if message is None:
            return ""
        return str(
            getattr(message, "processed_plain_text", "")
            or getattr(message, "content", "")
            or ""
        ).strip()

    async def _collect_candidates(
        self,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        content: str,
        texts: list[str],
        memory_lookup: memory_api.MemoryLookup | None = None,
    ) -> CandidateResult:
        """收集候选角色。

        判定优先级（1.0.10 起，1.0.14 起第二级由主题跟踪承担）：**描述点名 >
        主题跟踪（用户点名/粘性） > LLM 仲裁 > 不补**。

        - 描述点名：画面描述里已经写了角色表内角色 —— 由调用方直接放行/补标签，
          本函数只负责算出「判定集合」，供二次一致性检测对账；
        - **主题跟踪**（1.0.14 起，``monitor.enabled = true``）：把「这次该画谁」交给
          :func:`monitor.track_subject` —— 按时间正序扫最近 ``monitor.window`` 条消息
          维护一个「当前主题」（改口 / 多人 / 点名 / 本尊身份 / 代词回指 / 拆单），置信度 =
          信号强度 × 时间衰减，低于 ``monitor.min_confidence`` 就不补。它压过 LLM 的
          猜测；主题**不可用**（本尊身份不在角色表、取不到身份名、代词无先行词）时
          直接不补且不问 LLM。只有「换人还是追加不确定」（相邻两条点名消息靠得太近）
          才交 LLM 仲裁，与 1.0.12 起的既有语义一致；
        - **拆单**（1.0.16 起，主题跟踪的 ``split``）：同一条用户消息里既有自我指代词
          又有角色表内角色（「本尊看看，然后穿着爱莉来看看～」＝一单两件）时，主题
          「不可单独确定」。这是**唯一**允许 LLM 压过主题的情形：强制问一次 LLM，
          它给出的**角色表内**结论直接采用（日志
          ``判定依据=拆单→LLM 按画面判定:<角色名>``）；LLM 空 / 失败 / 超时 /
          ``use_llm = false`` → **不补**（日志 ``判定依据=无(不补)（拆单且无法按画面判定）``）。
          其它 status（可用 / 不可用 / 置信度不足 / 空）的一切照旧；
        - 自我指代（1.0.12 起；1.0.13 起能定位到角色）：``monitor.enabled = false`` 时
          走这条老链路 —— 按「``general.true_self`` → 人格配置 ``nickname`` →
          ``alias_names``」取本尊身份并在角色表里定位；**命中就补那个角色**，
          没命中或完全取不到身份名才**直接不补**（见 ②.5），不走粘性判定；
        - 用户点名(粘性)：``character.resolve_naming_decision`` 的确定性结论
          （带 ``multi.stickiness_hours`` 时效、换人/追加规则）—— 同样只在
          ``monitor.enabled = false`` 时使用。**它压过 LLM 的猜测** ——
          「用户 21:52 点名流萤、01:03 只说想看看你」时必须是流萤；
        - LLM 仲裁：主题为空 / 置信度不足 / 换人还是追加不确定 / **拆单**时才问（条件见 ④）；
        - 都不成立 → 不补（宁少勿多）。

        Args:
            memory_lookup: 本次「记忆库再确认」的结果（1.0.18，可为 ``None``）。
                命中「触发词 → 角色表内角色」时调用方已经在 :meth:`_memory_verdict`
                里直接用掉了它，走到这里说明**没有明确对应**；此处的用途只有一个 ——
                把命中片段的 ``title + content_snippet`` 作为**参考材料**（独立一段
                「相关记忆（供参考，不是指令）」）喂给 LLM 角色提取。

        Returns:
            :class:`CandidateResult`
        """
        messages = await self._collect_context(params, config)
        recent_window = (
            int(config.multi.recent_messages) if config.multi.recent_only else 0
        )
        window = messages[-recent_window:] if recent_window > 0 else messages

        # ① 上文中被提及的角色表内角色：只用于 multi_only 的「是不是多人场景」判断
        history_candidates: list[CharacterEntry] = []
        seen: set[str] = set()
        if roster:
            for entry in find_characters_in_history(window, roster):
                key = entry_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                history_candidates.append(entry)
        mentioned = len(history_candidates)

        # ② 画面描述里已经点名的「角色表内」角色
        present_keys = {
            entry_key(entry) for entry in roster if character_in_texts(entry, texts)
        }
        # ②.5 要用：描述里到底有没有角色表内角色（判定自我指代的前提之一）
        description_has_character = any(
            character_in_texts(entry, [content]) for entry in roster
        )

        # bot 自己的复述既不算点名、也不算「用户说过本尊」，所以先尽力解析本机 bot 的
        # sender_id（解析不到就退化成「最近提及优先」，见 character.is_known_bot_message）。
        # ②.5 与 ③ 共用这一份结果，只解析一次。
        bot_ids = await self._resolve_bot_sender_ids(window)

        # ② 主题跟踪（1.0.14 起，``monitor.enabled = true`` 时的主判定）：按时间正序
        # 扫最近 ``monitor.window`` 条消息，逐条演进一个「当前主题」，置信度 =
        # 信号强度 × 时间衰减。它取代了 1.0.13 里分散在两处的规则 ——「自我指代扫描
        # （②.5）」与「用户点名/主角粘性（③）」；规则、优先级与可读日志统一在
        # ``monitor.track_subject`` 一处（README「主题跟踪」一节有完整表格）。
        #
        # ``monitor.enabled = false`` 时走下面的 else 分支：1.0.13 的两条链路
        # 一字未改，可逐字节回归（用例 C53）。
        monitor = getattr(config, "monitor", None)
        monitor_on = bool(getattr(monitor, "enabled", False)) and bool(roster)
        track: SubjectTrack | None = None
        naming = NamingDecision()

        if monitor_on:
            try:
                track = track_subject(
                    messages,
                    roster,
                    window=int(getattr(monitor, "window", 15)),
                    min_confidence=float(getattr(monitor, "min_confidence", 0.5)),
                    half_life_minutes=float(
                        getattr(monitor, "half_life_minutes", 45.0)
                    ),
                    multi_window_minutes=float(config.multi.multi_window_minutes),
                    # 自我指代规则与 1.0.13 同一前提：画面描述里已经写明了角色表内角色时，
                    # 「本尊/你自己」不再改变主题（描述点名优先级最高）
                    self_reference_enabled=(
                        bool(config.general.skip_on_self_reference)
                        and not description_has_character
                    ),
                    bot_ids=bot_ids,
                    resolve_identity=lambda: resolve_true_self_identity(config),
                )
            except Exception as e:  # noqa: BLE001 —— 跟踪器自身出问题绝不能拦住生图
                logger.warning(
                    f"主题跟踪异常，本次退回 1.0.13 判定链路: {type(e).__name__}: {e}"
                )
                track = None
                monitor_on = False
            else:
                logger.debug(track.log_line())
                # 判定依据与主题轨迹一样重要：用户核对「为什么补了/没补」看的就是它
                # （「本尊身份」/「不在角色表」/「置信度不足」等原因都在里面）
                logger.debug(track.basis)
                naming = track.naming()
                if track.unavailable:
                    # 本尊身份不在角色表 / 取不到身份名 / 代词没有先行词：宁可什么都不补，
                    # 也绝不用粘性把更早的旧角色捡回来（1.0.12 起一直守的结论）。
                    # 这里**不问 LLM**：问它同样只能从上文里猜出那个旧角色。
                    # 1.0.18：调用方在问记忆库之前已经先试过「再确认」，能走到这里
                    # 说明记忆库也没给出明确对应 —— 状态与 1.0.17 完全一致。
                    return CandidateResult(
                        candidates=[],
                        decided=[],
                        llm_multi=False,
                        mentioned=mentioned,
                        naming=naming,
                        basis=track.basis,
                        memory=memory_lookup,
                        track=track,
                    )

        if not monitor_on:
            # ②.5 自我指代（1.0.12 起；1.0.13 起能定位到具体角色）：用户要的是
            # 「bot 本尊」，而「本尊是谁」只存在于调用方（聊天器）的内部设定里，
            # 插件看不到。此时按粘性沿用旧点名会补错人（实测：更早用户点名过
            # 「爱莉希雅」，插件就把它补进了「要你自己」的画面）。
            # 1.0.13 起先按「general.true_self → 人格配置 nickname → alias_names」
            # 取本尊身份：命中角色表就**正常注入那个角色**（走既有注入路径），
            # 没命中或完全取不到身份名才不补（也不问 LLM —— 问它同样只能从上文里
            # 猜出那个旧角色）。前提是**描述里没有角色表内角色**：描述里写明了角色
            # 就是另一回事。
            if (
                config.general.skip_on_self_reference
                and roster
                and not description_has_character
            ):
                source, hit, scanned = self._detect_self_reference(
                    window=window,
                    trigger_text=self._trigger_text(params),
                    content=content,
                    bot_ids=bot_ids,
                    config=config,
                )
                if hit:
                    logger.debug(
                        f"自我指代命中：来源={source} 词={hit!r}"
                        f"（焦点范围=画面描述+触发消息+触发前最近 {scanned} 条用户消息）"
                    )
                    return self._resolve_self_reference(
                        config=config,
                        roster=roster,
                        mentioned=mentioned,
                    )

            # ③ 用户点名 / 主角粘性（确定性、零成本）：1.0.13 的主依据。
            naming = resolve_naming_decision(
                window,
                roster,
                stickiness_hours=float(config.multi.stickiness_hours),
                multi_window_minutes=float(config.multi.multi_window_minutes),
                bot_ids=bot_ids,
            )
            logger.debug(naming.label())

        # ④ 决定要不要问 LLM（沿用 1.0.6 起的既有条件，另加「不确定」与「拆单」）：
        #   - llm_mode="always"：显式要求每次都问，可识别角色表**外**的角色；
        #   - 没有任何上文命中：保持原有 LLM 兜底行为；
        #   - description_first 且描述里没有角色表内角色：让读过描述的 LLM 参与；
        #   - 上文出现 ≥2 个角色表内角色：多人场景需要 LLM 仲裁一次；
        #   - 换人还是追加不确定（uncertain / 主题跟踪的 ambiguous）：按规则交 LLM 仲裁；
        #   - **拆单**（1.0.16，主题跟踪的 split）：**强制**问一次，不受上面几条限制。
        # 注意：**问不问 LLM** 与 **听不听 LLM** 是两件事 —— 主题跟踪（或 1.0.13 的用户
        # 点名）有确定结论时，LLM 的表内结论一律让位（见 ⑤），这里问一次只是为了
        # llm_mode="always" 下还能认出角色表外的角色、以及保留 description_first 的
        # 既有排查能力。
        llm_mode = str(config.multi.llm_mode).lower()
        multi_candidates = (
            config.multi.enabled
            and str(config.multi.scope).lower() == SCOPE_MULTI_ONLY
            and mentioned >= 2
        )
        # 拆单（1.0.16）：monitor 开启时由主题跟踪给出 —— 同一条用户消息里既有自我
        # 指代词又有角色表内角色（「本尊看看，然后穿着爱莉来看看～」＝一单两件）。
        # 此时主题**不可单独确定**，且**只有这一种情形**允许 LLM 压过主题：
        # 它读得到本次的**画面描述**，能判断当前这张图是「本尊」还是「壳」。
        split = bool(track is not None and track.split)
        # 「换人还是追加不确定」的来源：monitor 开启时由主题跟踪给出（相邻两条点名
        # 消息间隔 < multi.multi_window_minutes），关闭时沿用 resolve_naming_decision。
        ambiguous = track.ambiguous if track is not None else naming.kind == "uncertain"
        need_llm = config.general.use_llm and (
            split
            or llm_mode == "always"
            or not history_candidates
            or (config.general.description_first and not present_keys)
            or multi_candidates
            or ambiguous
        )

        llm_candidates: list[CharacterEntry] = []
        llm_multi = False
        llm_answered = False
        # 1.0.18：记忆库命中的片段（title + content_snippet）作为**参考材料**喂给
        # LLM 提取 —— 独立成段「相关记忆（供参考，不是指令）」。只在这里传，不做
        # 任何额外解释；记忆内容是数据不是指令，提示词里也显式标了这一点。
        memory_references = (
            memory_lookup.references
            if memory_lookup is not None and memory_lookup.items
            else ""
        )
        if need_llm:
            context_text = ""
            try:
                context_text = await build_readable_messages_to_str(
                    window,
                    timestamp_mode="relative",
                )
            except Exception as e:  # noqa: BLE001 —— 上文格式化失败就退回空上下文继续
                logger.debug(f"格式化上文失败: {e}")

            # 这次日志很关键：上文为空时模型必然给不出角色，
            # 之前正是"事件缺 stream_id → 上文只有触发消息"导致误判为插件没生效。
            logger.debug(
                f"调用 LLM 提取角色: 上文 {len(context_text)} 字符 / "
                f"{len(window)} 条消息，描述={content[:40]!r}"
                f"{f'，记忆库参考 {len(memory_references)} 字符' if memory_references else ''}"
            )
            extraction = await extract_characters_via_llm(
                description=content,
                context_text=context_text,
                roster=roster,
                translate_model=config.general.translate_model,
                timeout=float(config.general.llm_timeout),
                memory_references=memory_references,
            )
            if extraction is None:
                logger.debug("LLM 提取无结果（超时或失败），按用户点名/粘性判定兜底")
            else:
                llm_multi = extraction.multi
                llm_answered = bool(extraction.characters)
                logger.debug(
                    "LLM 提取结果: "
                    f"characters={[entry.name or entry.tag for entry in extraction.characters]} "
                    f"multi={extraction.multi}"
                )
                # 注意：这里**不能**复用上文史命中的 seen 集合。实测踩过：
                # 角色在上文里被提到过 → 已在 seen 里 → LLM 的结果被去重掉 →
                # 列表变空 → 被误判成"角色已在提示词里"，于是什么都不补。
                seen_llm: set[str] = set()
                for entry in extraction.characters:
                    if character_in_texts(entry, texts):
                        # 提示词里已经写了的，LLM 偶尔仍会列出来
                        continue
                    entry = _enrich_from_roster(entry, roster)
                    key = entry_key(entry)
                    if key in seen_llm:
                        continue
                    seen_llm.add(key)
                    llm_candidates.append(entry)

        # ④.5 拆单（1.0.16）：主题跟踪给出的结论是「不可单独确定」（同一条用户消息里
        # 既有自我指代词、又有角色表内角色），**只在这一种情形下**允许 LLM 压过主题 ——
        # 它读得到本次的**画面描述**，而主题跟踪只看对话上文，两者在拆单场景里权重相同
        # （这正是 1.0.15 的事故：「本尊看看，然后穿着爱莉来看看～」被整条判成
        # 「用户点名→爱莉希雅」，把 LLM 按画面正确认出的本尊=花火压掉了）。
        # 只采纳**角色表内**的结论：拆单时主题跟踪给不出任何可核对的候选人，角色表外的
        # 名字无从对账，宁可不补（与 `llm_mode="always"` 的「额外认人」是两回事）。
        # LLM 没结论（空 / 失败 / 超时 / `use_llm = false`）→ 什么都不补。
        if split:
            in_roster = [
                entry
                for entry in llm_candidates
                if any(entry_key(entry) == entry_key(known) for known in roster)
            ]
            if in_roster:
                candidates = list(in_roster)
                decided = list(in_roster)
                basis = (
                    "判定依据=拆单→LLM 按画面判定:"
                    f"{'、'.join(entry.name or entry.tag or '?' for entry in in_roster)}"
                )
            else:
                candidates = []
                decided = []
                basis = "判定依据=无(不补)（拆单且无法按画面判定）"
            logger.debug(basis)
            return CandidateResult(
                candidates=candidates[: max(1, int(config.multi.max_characters))],
                decided=decided,
                llm_multi=llm_multi,
                mentioned=mentioned,
                naming=naming,
                basis=basis,
                memory=memory_lookup,
                track=track,
            )

        # ⑤ 汇总。优先级：主题跟踪（1.0.14）/ 用户点名·粘性（1.0.13）有确定结论时以它
        # 为准 —— LLM 的表内猜测一律让位（这就是 1.0.10 起要修的核心问题：用户点名
        # 流萤，LLM 却猜成爱莉希雅）；只有 llm_mode="always" 这种显式开启的场景，才
        # 额外保留 LLM 认出来的**角色表外**角色（宁少勿多，不让模型的猜测混进画面）。
        # 唯一的例外是「换人还是追加不确定」：相邻两条点名消息靠得太近又没有任何
        # 追加/改口标记时，交 LLM 仲裁 —— 与 1.0.12 起「还是」判语境后的既有行为一致。
        if track is not None and track.usable:
            if track.ambiguous and llm_candidates:
                candidates = list(llm_candidates)
                decided = list(llm_candidates)
                basis = (
                    f"判定依据=LLM 仲裁:"
                    f"{[entry.name or entry.tag or '?' for entry in llm_candidates]}"
                    f" multi={str(llm_multi).lower()}"
                    f"（主题跟踪={track.subject_names()}，但换人/追加不确定）"
                )
                logger.debug(basis)
            else:
                candidates = [
                    entry
                    for entry in track.entries
                    if not character_in_texts(entry, texts)
                ]
                decided = list(track.entries)
                basis = track.basis
                if track.ambiguous:
                    basis = f"{basis}；LLM 无结论，按主题跟踪结果补入"
                outside = (
                    [
                        entry
                        for entry in llm_candidates
                        if not any(
                            entry_key(entry) == entry_key(known) for known in roster
                        )
                    ]
                    if llm_mode == "always"
                    else []
                )
                if outside:
                    logger.debug(
                        "LLM 额外识别出角色表外的角色 "
                        f"{[entry.name or entry.tag for entry in outside]}，一并补入"
                    )
                    candidates = merge_entries(candidates, outside)
        elif naming.kind in {"single", "multi"}:
            candidates = [
                entry
                for entry in naming.characters
                if not character_in_texts(entry, texts)
            ]
            decided = list(naming.characters)
            basis = naming.label()
            outside = (
                [
                    entry
                    for entry in llm_candidates
                    if not any(entry_key(entry) == entry_key(known) for known in roster)
                ]
                if llm_mode == "always"
                else []
            )
            if outside:
                logger.debug(
                    "LLM 额外识别出角色表外的角色 "
                    f"{[entry.name or entry.tag for entry in outside]}，一并补入"
                )
                candidates = merge_entries(candidates, outside)
        elif llm_candidates:
            candidates = list(llm_candidates)
            decided = list(llm_candidates)
            basis = (
                f"判定依据=LLM 仲裁:"
                f"{[entry.name or entry.tag or '?' for entry in llm_candidates]}"
                f" multi={str(llm_multi).lower()}"
            )
            logger.debug(basis)
        elif llm_answered:
            # LLM 答了，只是它给的角色都已经在提示词里 → 画面没问题，本次不补，
            # 绝不回退到上文里的旧角色
            logger.debug("LLM 给出的角色都已在提示词中，无需补入（不回退上文命中）")
            candidates = []
            decided = []
            basis = "判定依据=LLM 仲裁:[]（LLM 认为无需补入）"
            logger.debug(basis)
        elif naming.kind == "uncertain":
            # 不确定且 LLM 无结论/不可用 → 宁少勿多，只补最近那一个
            candidates = [
                entry
                for entry in naming.characters
                if not character_in_texts(entry, texts)
            ]
            decided = list(naming.characters)
            basis = f"{naming.label()}；LLM 无结论，宁少勿多只补最近那一个"
        else:
            # 无信号（没有点名、或点名已超出粘性时效）；monitor 开启时由主题跟踪给出
            # 具体原因（轨迹与判定依据在上面已经打过日志）：窗口内根本没有信号，
            # 还是信号太旧被 min_confidence 挡掉。
            candidates = []
            decided = []
            basis = track.basis if track is not None else naming.label()

        max_candidates = max(1, int(config.multi.max_characters))
        return CandidateResult(
            candidates=candidates[:max_candidates],
            decided=decided,
            llm_multi=llm_multi,
            mentioned=mentioned,
            naming=naming,
            basis=basis,
            memory=memory_lookup,
            track=track,
        )

    @staticmethod
    def _detect_self_reference(
        *,
        window: list[dict[str, Any]],
        trigger_text: str,
        content: str,
        bot_ids: set[str],
        config: CharPromptGuardConfig,
    ) -> tuple[str, str, int]:
        """在「焦点范围」里查找自我指代词（``本尊`` / ``你本人`` / ``你自己`` …）。

        .. note::
           1.0.14 起这条链路只在 ``monitor.enabled = false`` 时使用 —— 默认开启
           **主题跟踪**（``monitor.track_subject``）时，自我指代是状态机里的一条规则
           （主题 = 本尊身份），不再需要这里「按焦点范围找词」的启发式。保留它是为了
           「关掉 monitor 就完全退回 1.0.13」（回归用例 C53）。

        焦点范围（1.0.12）按顺序是：

        1. **画面描述**（``content``）；
        2. **触发本次生图的那条消息**；
        3. **触发之前最近 N 条用户消息**，N =
           ``min(multi.recent_messages, _SELF_REFERENCE_SCAN_LIMIT)``（默认 6 与 4
           取小 → 4 条），从新到旧数，bot 自己的消息跳过、不占名额。

        为什么要这么宽：实测事故（12:14~12:15 同一会话）里用户真正表达意图的是
        12:14:39 的「那你再来图，要你自己～」，而触发消息是 20 秒后的「嗯哼？」，
        画面描述里也没有角色名 —— 只看后两者根本看不到「要本尊」，插件就会按粘性
        把更早（12:12 引用块里）提到的「爱莉希雅」补进画面。N 不放大到整个窗口，
        是为了避免把别的话题里的「本尊」翻出来压制正常的角色点名。

        注意这里用 :func:`character.message_text` 取正文：引用块/媒体块（``[回复…]``）
        里的文字不算用户自己的表达，与点名扫描保持同一口径。

        Returns:
            ``(来源说明, 命中的自我指代词, 实际回看的用户消息条数)``；
            没有命中时前两项为空串。
        """
        sources: list[tuple[str, str]] = [("画面描述", content)]
        if trigger_text:
            sources.append(("触发消息", strip_quoted_blocks(trigger_text)))

        scan_limit = max(
            1, min(int(config.multi.recent_messages), _SELF_REFERENCE_SCAN_LIMIT)
        )
        # _collect_context 只在 trigger_text 非空时才把触发消息追加在末尾，
        # 所以「末尾那条是不是触发消息」可以直接由 trigger_text 判断。
        history_messages = window[:-1] if trigger_text else window
        scanned = 0
        # order_messages_newest_first：窗口理论上已是「旧 → 新」，但时间戳乱序时
        # 必须显式排一次，否则「最近 N 条用户消息」会数错（同 resolve_naming_decision 的口径）
        for message in order_messages_newest_first(history_messages):
            if scanned >= scan_limit:
                break
            if is_known_bot_message(message, bot_ids):
                continue
            scanned += 1
            sources.append((f"触发前第 {scanned} 条用户消息", message_text(message)))

        for source, text in sources:
            hit = find_self_reference_word(text)
            if hit:
                return source, hit, scanned
        return "", "", scanned

    @staticmethod
    def _resolve_self_reference(
        *,
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        mentioned: int,
    ) -> CandidateResult:
        """自我指代命中后的判定：本尊身份 → 角色表命中就补，否则不补（1.0.13）。

        .. note::
           与 :meth:`_detect_self_reference` 一样，1.0.14 起只在
           ``monitor.enabled = false`` 时使用；默认的主题跟踪路径里，同样的
           「本尊身份」定位逻辑在 ``monitor.track_subject`` 的自我指代分支，
           日志字段（``本尊身份`` / ``(来源=…)`` / ``角色表命中`` / ``不在角色表中``）
           与这里保持一致。

        流程（对应 README「本尊/自我指代怎么定」）：

        1. 按 ``general.true_self`` → 人格配置 ``personality.nickname`` →
           ``alias_names`` 取「本尊身份」候选名（见 :func:`resolve_true_self_identity`）；
        2. 候选名**逐个**去角色表按「名字 → 别名 → 标签」找同一个角色
           （见 :func:`character.find_roster_identity`）；
        3. **命中** → 返回该角色作为候选，走既有注入路径（natural 追加中文说明 +
           角色表标准标签；``tag_completion`` 逻辑照旧），日志：
           ``判定依据=自我指代→本尊身份'<名字>'(来源=配置/人格配置)→角色表命中:<角色名>``；
        4. **没命中** → 不补，日志写明「本尊身份不在角色表中，请加入角色表」；
        5. **完全取不到身份名** → 保持 1.0.12 的保守行为（不补 + 原日志），
           并补一句提示（填 ``general.true_self`` 或人格配置 nickname）。

        无论哪种结果都**不猜测、不做外观识别**：拿不准就不补。

        Returns:
            :class:`CandidateResult`；命中时 ``candidates`` / ``decided`` 都是那一个角色
        """
        identity = resolve_true_self_identity(config)
        hit_entry: CharacterEntry | None = None
        matched_name = ""
        # 候选名逐个试：nickname 不在角色表、别名在角色表时也要能命中
        for candidate in identity.candidates:
            hit_entry = find_roster_identity(candidate, roster)
            if hit_entry is not None:
                matched_name = candidate
                break

        if hit_entry is not None:
            basis = (
                f"判定依据=自我指代→本尊身份'{matched_name}'"
                f"(来源={identity.source})"
                f"→角色表命中:{hit_entry.name or hit_entry.tag or '?'}"
            )
            logger.debug(basis)
            naming = NamingDecision(
                kind="single",
                characters=[hit_entry],
                basis=BASIS_USER_NAMED,
                note=f"自我指代：本尊身份'{matched_name}'（来源={identity.source}）",
            )
            return CandidateResult(
                candidates=[hit_entry],
                decided=[hit_entry],
                llm_multi=False,
                mentioned=mentioned,
                naming=naming,
                basis=basis,
            )

        if identity.candidates:
            if len(identity.candidates) > 1:
                logger.debug(
                    f"本尊身份候选（来源={identity.source}）：{identity.candidates}，"
                    "都不在角色表中"
                )
            # 人格配置的 nickname 有框架默认值（core.toml 里默认是「小狐狸」），
            # 很容易落在角色表之外 —— 额外提示一条更省事的做法（判定依据那行不动）
            if identity.source == _TRUE_SELF_SOURCE_PERSONALITY:
                logger.debug(
                    f"提示：本尊身份来自人格配置（{'、'.join(identity.candidates)}）；"
                    "若与实际角色不符，可直接在 general.true_self 里填写角色表中的名字或别名覆盖"
                )
            # 「本尊是谁」有来源但角色表里没有 → 不补，并把该做什么写清楚
            naming = NamingDecision(
                kind="none",
                note=(
                    f"本尊身份='{identity.candidates[0]}' 不在角色表中，"
                    "无法补标准标签；请把该角色加入角色表"
                ),
            )
        else:
            # 1.0.12 的原日志一字不改，后面补一句怎么配置
            naming = NamingDecision(
                kind="none",
                note="检测到自我指代，本尊由调用方设定，插件不猜",
            )
        basis = naming.label()
        if not identity.candidates:
            basis = (
                f"{basis}"
                "（未取到本尊身份：请填写 general.true_self"
                " 或在人格配置里设置 nickname）"
            )
        logger.debug(basis)
        return CandidateResult(
            candidates=[],
            decided=[],
            llm_multi=False,
            mentioned=mentioned,
            naming=naming,
            basis=basis,
        )

    async def _resolve_bot_sender_ids(self, messages: list[dict[str, Any]]) -> set[str]:
        """尽力解析「本机 bot 自己的 sender_id」集合。

        框架自带的 history 字典（``message_api._rows_to_message_dicts``）只有
        ``sender_id`` / ``sender_name``，没有 ``is_bot`` / ``role`` 字段；框架
        自己是靠 ``adapter_api.get_bot_info_by_platform(platform)["bot_id"]``
        比对的，这里沿用同一口径。

        取不到时返回空集合：判定会退化成「最近提及优先」（把 bot 的复述也当
        点名），但绝不会因为认不出字段而让整条链路静默失效。用
        ``importlib`` 动态导入是为了不把 ``adapter_api`` 变成插件的硬依赖
        （manifest 的 ``api_version`` 里没有它，静态导入会被校验器挑出来）。
        """
        platforms = {str(message.get("platform") or "") for message in messages} - {""}
        if not platforms:
            return set()
        try:
            module = importlib.import_module("src.app.plugin_system.api.adapter_api")
        except Exception as e:  # noqa: BLE001 —— 拿不到 adapter 信息就退化成最近提及
            logger.debug(f"adapter_api 不可用，无法区分 bot 消息与用户消息: {e}")
            return set()
        get_bot_info = getattr(module, "get_bot_info_by_platform", None)
        if get_bot_info is None:
            return set()

        bot_ids: set[str] = set()
        now = time.time()
        for platform in platforms:
            cached = _bot_id_cache.get(platform)
            if cached is not None and (now - cached[0]) < _BOT_ID_CACHE_TTL:
                bot_id = cached[1]
            else:
                try:
                    info = await get_bot_info(platform)
                except Exception as e:  # noqa: BLE001 —— 单个平台失败不影响其它平台
                    logger.debug(f"解析平台 {platform} 的 bot 信息失败: {e}")
                    info = None
                bot_id = str((info or {}).get("bot_id") or "").strip() or None
                _bot_id_cache[platform] = (now, bot_id)
            if bot_id:
                bot_ids.add(bot_id)
        return bot_ids

    # ── 「用户点名了角色表外角色」的扫描与检测（1.0.20）──────────────

    async def _scan_user_naming(
        self,
        *,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        exclude: list[str] | None = None,
    ) -> UserNamingScope:
        """扫描「最近若干条用户消息」里用户点名的**表外**角色名（1.0.20）。

        焦点范围沿用 1.0.12 起自我指代扫描（:meth:`_detect_self_reference`）的做法：
        **触发消息** + **触发之前最近 N 条用户消息**（N = ``min(multi.recent_messages,
        _OUTSIDE_NAMING_SCAN_LIMIT)``，默认 ``min(6, 4) = 4``；bot 自己的消息跳过、
        不占名额）。区别只有两点：

        - **只取用户消息**，且**不扫画面描述** —— 画面描述是"画面那一侧"，
          正是这条判断要检查的对象，不能拿它给自己作证；
        - 取正文一律走 :func:`character.message_text` / :func:`strip_quoted_blocks`，
          引用块（``[回复…]``）与媒体块里的文字**不算**用户自己的点名。

        提取规则（引号 / 句式）与过滤（长度 / 纯标点 / 常见词黑名单 / 角色表内）
        全部在 :func:`character.extract_outside_names`，本方法只负责"扫哪些消息"。

        **1.0.23 的豁免**：``exclude`` 是**本次皮肤识别认定的皮肤 / 时装名**
        （:meth:`_scan_outfit_names` 的 :attr:`OutfitScope.names`）。它们不该再被
        当成「角色表外的角色名」—— 用户说「画粉色妖精小姐这套」时，「粉色妖精小姐」
        是**一套皮肤**，不是角色；被误当成角色时不仅会多问一次模型（1.0.21 的角色
        解析），``fix`` 还会去替换 / 删除画面里正确角色的标签。豁免口径与「表内」判定
        一致（完全相同或互为子串，忽略大小写 / 空白），被豁免的候选会记一条 DEBUG
        ``皮肤名豁免: …``，方便核对"是不是把真的表外角色名也豁免掉了"。
        没有皮肤名（``exclude`` 为空）时行为与 1.0.22 逐字一致。

        ``consistency_check = "off"``、角色表为空时**直接返回空结果**（连上文都不读）；
        任何异常同样只记 DEBUG 并返回空结果 —— 这条判断是「多一条检查」，
        绝不允许影响判定链或拦住生图。

        .. note::
           本方法自己读一次上文（``_collect_context``）。这是一次**独立**的读取，
           但它走的是同一份进程内缓存（``general.history_cache_ttl``，默认 5 秒）：
           缓存热时只是列表拷贝，缓存冷时它顺手把缓存填上，后面的
           ``_memory_lookup`` / ``_collect_candidates`` 直接命中 —— 也就是说
           **同一次生图事件最多只多一次库读**，且只在 ``history_cache_ttl = 0``
           （用户显式关掉缓存）时才会真的多读一次。
           1.0.23 起皮肤识别（:meth:`_scan_outfit_names`）也自己读一次上文，
           它跑在本方法**之前**、用的是同一份缓存，因此总量不变。
        """
        mode = (
            str(getattr(config.general, "consistency_check", "") or "").strip().lower()
        )
        if mode == "off" or not roster:
            return UserNamingScope()
        try:
            messages = await self._collect_context(params, config)
            trigger_text = self._trigger_text(params)
            # _collect_context 只在 trigger_text 非空时才把触发消息追加在末尾
            history = messages[:-1] if trigger_text else messages
            bot_ids = await self._resolve_bot_sender_ids(messages)

            texts: list[str] = []
            if trigger_text:
                texts.append(strip_quoted_blocks(trigger_text))
            scan_limit = max(
                1,
                min(int(config.multi.recent_messages), _OUTSIDE_NAMING_SCAN_LIMIT),
            )
            scanned = 0
            for message in order_messages_newest_first(history):
                if scanned >= scan_limit:
                    break
                if is_known_bot_message(message, bot_ids):
                    continue
                scanned += 1
                texts.append(message_text(message))

            names: list[str] = []
            sources: dict[str, str] = {}
            exempted: list[str] = []
            for text in texts:
                found = extract_outside_names(
                    text,
                    roster,
                    exclude=exclude or (),
                    dropped=exempted,
                )
                for name in found:
                    if name not in names:
                        names.append(name)
                        # 记住它来自哪句话（1.0.21：解析成功后写回记忆库时的触发语）
                        sources[name] = text
            if exempted:
                # 去重保序：同一套皮肤可能在好几条消息里被抓到
                unique = list(dict.fromkeys(exempted))
                logger.debug(
                    "皮肤名豁免: "
                    + "、".join(unique)
                    + "（已由皮肤识别认定为皮肤/时装名，不算表外角色名）"
                )
            scope = UserNamingScope(
                texts=texts, scanned=scanned, names=names, sources=sources
            )
            if names:
                logger.debug(f"表外点名扫描: {scope.label()}")
            return scope
        except Exception as e:  # noqa: BLE001 —— 多一条检查，失败即静默退回
            logger.debug(f"表外点名扫描失败，本次跳过（{type(e).__name__}: {e}）")
            return UserNamingScope()

    # ── 皮肤/时装：识别与解析（1.0.22）─────────────────────────────

    async def _scan_outfit_names(
        self,
        *,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        entries: list[OutfitEntry] | None = None,
        texts: list[str] | None = None,
    ) -> OutfitScope:
        """扫描「最近若干条**用户消息**」里用户点名的皮肤/时装名（1.0.22）。

        焦点范围与自我指代 / 表外点名**完全一致**：**触发消息** + **触发之前最近
        N 条用户消息**（``N = min(multi.recent_messages, 4)``；bot 自己的消息跳过、
        不占名额），且**不扫画面描述** —— 画面描述是被改写的那一侧。

        ``texts`` 已经由调用方备好时直接复用，**不重复读上文**；没备好
        （1.0.23 起 ``execute`` 就是直接调本方法，不再拿表外点名那份结果）
        才自己读一次 —— 走的是同一份进程内缓存（``general.history_cache_ttl``）。

        多个候选时取**最新一条消息里的、最长的**一个（``texts`` 是「触发消息在前、
        其后由新到旧」，所以第一个有候选的消息就是最新的那条）。

        1.0.23：**返回的 :class:`OutfitScope` 里同时带上本次识别到的全部皮肤名**
        （``names``，含命中皮肤表时的**正式名**）—— 表外点名那条链路要用它做豁免
        （见 :meth:`_scan_user_naming`）。因此这里会**扫完**焦点范围内的每一条消息，
        而不是像 1.0.22 那样找到第一个候选就返回（选中的 ``name`` 与 ``source``
        仍是「最新一条消息里最长的那个」，语义没变）。

        ``[outfits] enabled = false`` 或任何异常 → 返回空结果：这条链路是「多一层
        外观」，绝不允许影响角色判定或拦住生图。
        """
        if not bool(getattr(getattr(config, "outfits", None), "enabled", False)):
            # 整条链路零开销：不读上文、不调模型、不打日志
            return OutfitScope()
        if entries is None:
            entries = _outfit_entries(config)
        try:
            focus = [text for text in (texts or []) if text]
            if not focus:
                messages = await self._collect_context(params, config)
                trigger_text = self._trigger_text(params)
                history = messages[:-1] if trigger_text else messages
                bot_ids = await self._resolve_bot_sender_ids(messages)
                if trigger_text:
                    focus.append(strip_quoted_blocks(trigger_text))
                scan_limit = max(
                    1,
                    min(int(config.multi.recent_messages), _OUTSIDE_NAMING_SCAN_LIMIT),
                )
                scanned = 0
                for message in order_messages_newest_first(history):
                    if scanned >= scan_limit:
                        break
                    if is_known_bot_message(message, bot_ids):
                        continue
                    scanned += 1
                    focus.append(message_text(message))
            name = ""
            source = ""
            names: list[str] = []
            for text in focus:
                hits = extract_outfit_names(text, entries, roster)
                if not hits:
                    continue
                if not name:
                    # 多个候选时取**最新一条消息里的、最长**的一个
                    name = max(hits, key=len)
                    source = text
                for hit in hits:
                    if hit not in names:
                        names.append(hit)
                    # 命中皮肤表的候选：把表里的**正式名**也记进来（1.0.23 的表外点名
                    # 豁免要用它 —— 用户写「画粉色妖精」时抓到的候选与表里的名字并不相等）
                    matched = find_outfit_entry(hit, entries)
                    if matched is not None and matched.name not in names:
                        names.append(matched.name)
            if not name:
                logger.debug("皮肤/时装=跳过（用户最近的几条消息里没有皮肤/时装的说法）")
                return OutfitScope(texts=focus, names=names)
            scope = OutfitScope(texts=focus, name=name, source=source, names=names)
            logger.debug(f"皮肤/时装扫描: {scope.label()}")
            return scope
        except Exception as e:  # noqa: BLE001 —— 多一层外观，失败即静默退回
            logger.debug(f"皮肤/时装扫描失败，本次跳过（{type(e).__name__}: {e}）")
            return OutfitScope()

    async def _resolve_outfit(
        self,
        *,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        scope: OutfitScope | None = None,
    ) -> ResolvedOutfit | None:
        """识别并解析本次的皮肤/时装（1.0.22）；没有或失败时返回 ``None``。

        流程（**表优先 → 模型兜底**）：

        1. ``outfits.enabled = false`` → **立刻返回**（不解析皮肤表、不读上文、不调模型）；
        2. 解析皮肤表（``outfits.entries``，带配置文本缓存）；``scope`` 由调用方
           （``execute``）先跑 :meth:`_scan_outfit_names` 备好时直接复用，
           没给（本方法被单独调用）才自己扫一次；
        3. 命中皮肤表 → 直接用表里的关键词 / 中文描述，**零模型调用**；表里的那一行
           没写关键词时按「表未命中」处理（继续走第 4 步，用户少填一列也能用）；
        4. 表未命中且 ``outfits.auto_resolve`` → 问一次模型（:meth:`_resolve_outfit_with_cache`，
           与角色解析同一套缓存策略）；``auto_resolve = false`` → 只留一条 DEBUG
           ``皮肤解析=跳过（…不在皮肤表里…）``；
        5. **1.0.23**：这次**一个皮肤名都没有**（``scope.name`` 为空）时，若
           ``outfits.infer_unnamed`` 为真 → 交给 :meth:`_infer_unnamed_outfit`
           去认「<角色>的那一套皮肤」并让模型推断（默认关，关掉时那一步立刻返回）。

        **本函数不抛异常**：任何失败都只记日志并返回 ``None``，由调用方什么都不注入。
        """
        section = getattr(config, "outfits", None)
        if not bool(getattr(section, "enabled", False)):
            return None
        entries = _outfit_entries(config)
        if scope is None:
            scope = await self._scan_outfit_names(
                params=params, config=config, roster=roster, entries=entries
            )
        if not scope.name:
            # 用户没点名具体的皮肤名 —— 1.0.23：只有显式开了 infer_unnamed 才继续，
            # 默认关时这里与 1.0.22 完全一致（返回 None，什么都不注入）
            return await self._infer_unnamed_outfit(
                config=config, roster=roster, texts=scope.texts
            )
        character = self._outfit_character_hint(scope, entries, roster)
        entry = find_outfit_entry(scope.name, entries)
        if entry is not None and entry.keywords:
            outfit = ResolvedOutfit(
                name=entry.name,
                character=entry.character or character,
                keywords=list(entry.keywords),
                description=entry.description,
                source=OUTFIT_SOURCE_TABLE,
                confidence=1.0,
                raw_name=scope.name,
            )
            logger.debug(outfit.log_line())
            return outfit
        if entry is not None:
            logger.debug(f"皮肤表条目「{entry.name}」没有填英文关键词，按表未命中处理")
        if not bool(getattr(section, "auto_resolve", True)):
            logger.debug(
                f"皮肤解析=跳过（{scope.name} 不在皮肤表里，"
                "且 outfits.auto_resolve=false）"
            )
            return None
        return await self._resolve_outfit_with_cache(
            config=config,
            name=scope.name,
            character=character,
            context_text=scope.source,
        )

    async def _infer_unnamed_outfit(
        self,
        *,
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        texts: list[str],
    ) -> ResolvedOutfit | None:
        """用户只说「<角色>的那一套皮肤」时让模型推断一套（1.0.23，**默认关**）。

        开关是 ``outfits.infer_unnamed``（默认 ``false``）：**关掉时本方法立刻返回**
        —— 不扫消息、不调模型、不打任何日志，行为与 1.0.22 逐字一致（0 次模型调用）。

        开启时：

        1. 先确定是哪个角色 —— 用 :func:`outfit.find_unnamed_outfit_owner` 在焦点范围内
           的用户消息里找「<角色名>的那一套皮肤 / 时装」（角色名必须是**角色表内**角色），
           ``texts`` 是「触发消息在前、其后由新到旧」，所以取到的是**最近一条**消息里
           提到的那个；找不到就什么都不做（只留一条 DEBUG）；
        2. 交给 :meth:`_resolve_outfit_with_cache` 的 ``inferred=True`` 分支：
           :func:`outfit.infer_outfit_via_llm`（与皮肤解析**同一套** ``llm_api`` /
           ``translate_model`` / ``llm_timeout``）给出该角色最知名的一套，
           校验口径与 1.0.22 完全相同（只收小写 ASCII 关键词；``unknown`` / JSON 坏 /
           ``confidence < 0.5`` / 无有效关键词 → **什么都不注入**）；
        3. 缓存与皮肤解析**同一套**，键为 ``<角色名>|__infer__``
           （:func:`outfit.outfit_infer_cache_key`）；
        4. 成功打 INFO ``皮肤推断(模型): 流萤 → <皮肤名> → <关键词>（来源=模型推断，confidence=…）``
           与 ``建议加入皮肤表：…``；**推断结果不写回记忆库**（模型猜的东西不该被当成
           "用户确认过的事实"记住）。

        **本函数不抛异常**：任何失败都只记日志并返回 ``None``，调用方据此什么都不注入。
        """
        if not bool(getattr(getattr(config, "outfits", None), "infer_unnamed", False)):
            # 默认关：一次模型都不问、一条日志都不打（与 1.0.22 逐字一致）
            return None
        if not roster:
            return None
        owner = ""
        source = ""
        for text in texts:
            owner = find_unnamed_outfit_owner(text, roster)
            if owner:
                source = text
                break
        if not owner:
            logger.debug(
                "皮肤推断=跳过（用户最近的几条消息里没有"
                "「<角色名>的那一套皮肤/时装」这种说法）"
            )
            return None
        logger.debug(
            f"皮肤推断: 候选角色={owner}（用户没点名具体是哪一套，"
            f"来源={source[:30]!r}）"
        )
        return await self._resolve_outfit_with_cache(
            config=config,
            name=owner,
            character=owner,
            context_text=source,
            inferred=True,
        )

    @staticmethod
    def _outfit_character_hint(
        scope: OutfitScope, entries: list[OutfitEntry], roster: list[CharacterEntry]
    ) -> str:
        """皮肤归属角色的**线索**（进模型提示词、日志与缓存键）；取不到返回空串。

        优先级：皮肤表第 2 列（用户明确写了归属）→ 焦点范围内用户消息里最近提到的
        角色表内角色（``scope.texts`` 是「触发消息在前、其后由新到旧」）。

        它**只是线索**：不影响注入内容，也不会被写进画面 —— 角色照旧由既有判定链决定
        （见 :meth:`_apply_outfit` 的 ``decided``）。
        """
        entry = find_outfit_entry(scope.name, entries)
        if entry is not None and entry.character:
            return entry.character
        for text in scope.texts:
            hits = find_characters_in_text(text, roster)
            if hits:
                return hits[0].name
        return ""

    async def _resolve_outfit_with_cache(
        self,
        *,
        config: CharPromptGuardConfig,
        name: str,
        character: str = "",
        context_text: str = "",
        inferred: bool = False,
    ) -> ResolvedOutfit | None:
        """解析一个皮肤名；**命中缓存就不再问模型**（1.0.22 的调用方缓存）。

        与 1.0.21 的角色解析（:meth:`_resolve_with_cache`）**完全同一套做法**：
        缓存**在这里读、在这里写**（存储与 TTL 常量在 :mod:`outfit`）——
        解析函数 :func:`outfit.resolve_outfit_via_llm` 是回归用例替换（打桩）的那一层，
        缓存写在它内部时用例里第二次生图照样会调模型。

        - 缓存键 = **皮肤名 + 角色名**（同一套「婚纱」落在不同角色身上是不同外观）；
        - 命中（成功 24 小时 / 失败 10 分钟）→ 日志 ``皮肤解析(缓存): …``，
          **一次模型调用都不发**，也不再写回记忆库；
        - 未命中 → 问一次模型，把结果（成功或失败）写进缓存：
          成功打 INFO ``皮肤解析(模型): …`` + ``建议加入皮肤表：…``
          并按 ``outfits.write_back`` 写回记忆库；失败打 DEBUG
          ``皮肤解析(LLM)失败：<原因>``（异常则 WARNING）。

        **1.0.23 的 ``inferred=True``**（用户只说「<角色>的那一套皮肤」、没点名具体皮肤）：
        此时 ``name`` 是**角色名**，缓存键换成 ``<角色名>|__infer__``
        （:func:`outfit.outfit_infer_cache_key`），解析函数换成
        :func:`outfit.infer_outfit_via_llm`，日志前缀变成 ``皮肤推断(模型/缓存): …``；
        其余（TTL、``建议加入皮肤表``、失败即什么都不注入）完全一致。
        **推断结果不写回记忆库** —— 它是模型猜的，不该被当成"用户确认过的事实"记住
        （1.0.22 那条写回只服务于「用户点名 → 解析出结果」这条可信路径）。

        **本函数不抛异常**（异常一律转成失败结果并写负缓存）；调用方拿到 ``None``
        或 ``ok`` 为假的结果时**什么都不注入**。
        """
        owner = (character or name).strip()
        cache_key = (
            outfit_infer_cache_key(owner)
            if inferred
            else outfit_cache_key(name, character)
        )
        label = "角色" if inferred else "皮肤"
        cached = outfit_cache_get(name, character, key=cache_key)
        if cached is not None:
            logger.info(cached.log_line())
            if cached.ok:
                self._log_outfit_suggestion(cached)
                return cached
            return None

        try:
            if inferred:
                result = await infer_outfit_via_llm(
                    owner,
                    translate_model=str(config.general.translate_model or ""),
                    timeout=float(config.general.llm_timeout or 0.0),
                    context_text=context_text,
                )
            else:
                result = await resolve_outfit_via_llm(
                    name,
                    character=character,
                    translate_model=str(config.general.translate_model or ""),
                    timeout=float(config.general.llm_timeout or 0.0),
                    context_text=context_text,
                )
        except Exception as e:  # noqa: BLE001 —— 解析是「多一层外观」，不许拦住生图
            logger.warning(
                f"{'皮肤推断' if inferred else '皮肤解析'}(LLM)失败："
                f"{type(e).__name__}: {e}（{label}={name!r}）"
            )
            # 异常也要进负缓存：同一套皮肤不必为每一张图都再等一次超时
            outfit_cache_put(
                name,
                ResolvedOutfit(
                    name=name,
                    character=character,
                    reason=f"调用失败({type(e).__name__}: {e})",
                    raw_name=name,
                    inferred=inferred,
                ),
                character=character,
                key=cache_key,
            )
            return None

        outfit_cache_put(name, result, character=character, key=cache_key)
        if not result.ok:
            logger.debug(f"{result.log_line()}（{label}={name!r}）")
            return None
        logger.info(result.log_line())
        self._log_outfit_suggestion(result)
        if inferred:
            logger.debug(
                f"皮肤推断结果不写回记忆库（模型推断、用户没点名）：{result.name}"
            )
            return result
        if bool(getattr(getattr(config, "outfits", None), "write_back", True)):
            await write_outfit_to_memory(config, result)
        return result

    @staticmethod
    def _log_outfit_suggestion(outfit: ResolvedOutfit) -> None:
        """打一条「建议加入皮肤表：…」的 INFO（**只打日志，绝不自动改配置文件**）。

        形如 ``建议加入皮肤表：粉色妖精小姐 | 爱莉希雅 | pink elf dress, fairy wings | <中文描述>``
        —— 用户可以直接粘贴进 ``outfits.entries``。
        """
        line = outfit.table_line()
        if line:
            logger.info(f"建议加入皮肤表：{line}")

    # ── 角色解析（LLM，1.0.21）────────────────────────────────────

    async def _resolve_outside_character(
        self,
        *,
        config: CharPromptGuardConfig,
        naming_scope: UserNamingScope | None,
        texts: list[str] | None = None,
        roster: list[CharacterEntry] | None = None,
    ) -> CharacterEntry | None:
        """把「用户点名的表外角色」解析成标准标签（1.0.21）；失败返回 ``None``。

        入口①（替换）/ 入口②（补入）共用这一次解析 —— 只在需要时调用。

        开关与前提（**全部满足才问**，任何一条不满足都在调用模型**之前** return，
        并留一条 DEBUG ``角色解析=跳过（<原因>）``）：

        - ``general.auto_resolve_characters``（默认开）；
        - ``general.consistency_check != "off"`` —— 「完全不检查」自然也不该问模型；
        - 焦点范围内**确实有**表外点名（:class:`UserNamingScope.names`）；
        - **至少有一个入口真的需要它**（``texts`` / ``roster`` 给出来时才检查）：
          **画面里没有角色表内角色** → 只可能是入口②（**补入**，例如描述只写了外观）；
          **画面里有角色表内角色** → 只可能是入口①（**替换**），此时要求存在
          「用户这次没点过的表内角色」才算冲突。两个条件都不成立（画面里是用户
          这次点过的表内角色）时，这次解析**不可能被用上**，于是**一次模型都不问**
          —— 角色在角色表内的普通场景（C76）走的正是这条，零成本；
        - 只对**第一个**表外名字解析（一次生图最多一次模型调用）。

        解析走 :meth:`_resolve_with_cache`（同一套 ``llm_api``、同一个
        ``translate_model`` / ``llm_timeout``；**调用方缓存**命中时不再问模型）；
        成功后按 ``memory.write_back`` 把这次解析**写回记忆库**（沿用现有写回通道，
        含 ``core_tags`` / ``diffusion_tags`` / ``opposing_tags`` 三元组）。

        **本函数不抛异常**：任何失败都只记 DEBUG/WARNING 并返回 ``None``，
        由调用方逐字退回 1.0.20 的行为。
        """
        if not bool(getattr(config.general, "auto_resolve_characters", True)):
            logger.debug("角色解析=跳过（general.auto_resolve_characters=false）")
            return None
        mode = (
            str(getattr(config.general, "consistency_check", "") or "").strip().lower()
        )
        if mode == "off":
            logger.debug("角色解析=跳过（general.consistency_check=off，本次不检查）")
            return None
        if naming_scope is None or not naming_scope.names:
            logger.debug("角色解析=跳过（用户最近的几条消息里没有角色表外的点名）")
            return None
        if texts is not None or roster is not None:
            # 「有没有入口真的需要它」：见 docstring —— 两个入口都不成立时一次都不问。
            # 注意这里不能拿「这个名字在不在用户消息里」当条件：它本来就来自用户消息
            # （extract_outside_names 的输入就是那几条），那样写等于永远不解析。
            present = [
                entry
                for entry in (roster or [])
                if character_in_texts(entry, texts or [])
            ]
            if present and not _conflicting_roster_entries(
                texts or [], roster or [], naming_scope
            ):
                # 画面里是**用户这次点过的**表内角色 → 不是冲突，也不需要补入
                logger.debug(
                    "角色解析=跳过（画面里的角色表内角色用户这次点过："
                    "没有冲突要修正、也没有空缺要补入）"
                )
                return None
        name = naming_scope.names[0]
        result = await self._resolve_with_cache(
            config=config,
            name=name,
            context_text=naming_scope.sources.get(name, ""),
        )
        if result is None or not result.ok or result.entry is None:
            return None
        await self._write_resolved_to_memory(
            config=config,
            resolved=result,
            trigger=self._resolve_trigger(name, naming_scope),
        )
        return result.entry

    async def _resolve_with_cache(
        self,
        *,
        config: CharPromptGuardConfig,
        name: str,
        context_text: str = "",
    ) -> ResolvedCharacter | None:
        """解析一个表外角色；**命中缓存就不再问模型**（1.0.21 的调用方缓存）。

        缓存**在这里读、在这里写**（存储与 TTL 常量在 :mod:`character`）：
        解析函数 :func:`character.resolve_character_via_llm` 是回归用例替换（打桩）
        的那一层，缓存写在它内部时，用例里第二次生图照样会调模型（C75 断言只问一次）。

        - 命中（成功 24 小时 / 失败 10 分钟）→ 日志 ``角色解析(缓存): …``，
          **一次模型调用都不发**；
        - 未命中 → 问一次模型，把结果（成功或失败）写进缓存：
          成功打 INFO ``角色解析(模型): …`` + ``建议加入角色表：…``，
          失败打 DEBUG ``角色解析(LLM)失败：<原因>``（异常则 WARNING）。

        **本函数不抛异常**（异常一律转成失败结果并写负缓存）；调用方拿到
        ``ok`` 为假的结果时退回 1.0.20 的行为。

        Returns:
            :class:`character.ResolvedCharacter`；连结果都拿不到（异常）时返回 ``None``
        """
        cached = resolve_cache_get(name)
        if cached is not None:
            logger.info(cached.log_line())
            self._log_roster_suggestion(cached)
            return cached

        try:
            result = await resolve_character_via_llm(
                name,
                translate_model=str(config.general.translate_model or ""),
                timeout=float(config.general.llm_timeout or 0.0),
                context_text=context_text,
            )
        except Exception as e:  # noqa: BLE001 —— 解析是「多一条路」，不许拦住生图
            logger.warning(
                f"角色解析(LLM)失败：{type(e).__name__}: {e}（角色={name!r}）"
            )
            # 异常也要进负缓存：同一个角色不必为每一张图都再等一次超时
            resolve_cache_put(
                name,
                ResolvedCharacter(
                    reason=f"调用失败({type(e).__name__}: {e})", raw_name=name
                ),
            )
            return None

        resolve_cache_put(name, result)
        if not result.ok or result.entry is None:
            logger.debug(
                f"角色解析(LLM)失败：{result.reason or '未知原因'}（角色={name!r}）"
            )
            return None
        logger.info(result.log_line())
        self._log_roster_suggestion(result)
        return result

    @staticmethod
    def _log_roster_suggestion(resolved: ResolvedCharacter) -> None:
        """打一条「建议加入角色表：…」的 INFO（**只打日志，绝不自动改配置文件**）。

        形如 ``建议加入角色表：刻律德拉 | 崩坏：星穹铁道 (Honkai: Star Rail) |
        kleidra,刻律德拉 | kleidra \\(honkai: star rail\\)`` —— 用户可以直接
        粘贴进 ``roster.characters``。
        """
        line = resolved.roster_line()
        if line:
            logger.info(f"建议加入角色表：{line}")

    @staticmethod
    def _resolve_trigger(name: str, naming_scope: UserNamingScope) -> str:
        """解析写回记忆库时用的「触发词」：优先取该名字来自的那条用户消息。

        用户的原话（「这身是？」「刻律德拉那身」）比裸名字信息更多，检索时也更容易
        命中；取不到或过长时退回 ``角色解析``。
        """
        source = str(naming_scope.sources.get(name) or "").strip()
        if source and len(source) <= 20:
            return source
        return "角色解析"

    @staticmethod
    async def _write_resolved_to_memory(
        *,
        config: CharPromptGuardConfig,
        resolved: ResolvedCharacter,
        trigger: str,
    ) -> None:
        """把一次成功的角色解析写回记忆库（1.0.21）；失败只记日志。

        走 :meth:`memory.MemoryClient.write`（**现有写回通道**）而不是
        ``memory.lookup``：解析结果不依赖关键词检索，不需要（也不该）被
        「检索失败退避窗口」挡住 —— 那条退避是给读取用的。

        写回内容里的三元组照旧由 :meth:`memory.MemoryClient.write` 组装
        （1.0.20 已修：``core_tags`` / ``diffusion_tags`` / ``opposing_tags``
        **同时非空**，不要退回只发 ``core_tags``）。

        ``memory.write_back = false`` 时**一条也不写**（用户在配置里明确关掉了写回）；
        记忆库不可用时 :meth:`memory.MemoryClient.write` 自己吞掉异常并记 INFO。
        """
        if resolved.entry is None:
            return
        if not memory_api.memory_enabled(config):
            return
        section = getattr(config, "memory", None)
        if getattr(section, "write_back", True) is False:
            return
        try:
            client = memory_api.build_client(config)
            if client.invalid_base_url:
                return
            memory_id = await client.write(
                trigger_word=trigger,
                character=memory_api.MemoryCharacter(
                    name=resolved.entry.name,
                    game=resolved.entry.game,
                    aliases=tuple(resolved.entry.aliases),
                    tag=resolved.entry.resolved_tag(),
                ),
                scenario=f"角色解析(LLM)：{resolved.raw_name or resolved.entry.name}",
            )
            if memory_id:
                logger.debug(
                    f"角色解析结果已写回记忆库：{resolved.entry.name}（id={memory_id}）"
                )
        except Exception as e:  # noqa: BLE001 —— 写回失败只记日志，绝不影响生图
            logger.debug(
                f"角色解析结果写回记忆库失败（已忽略）: {type(e).__name__}: {e}"
            )

    def _resolved_for_injection(
        self,
        *,
        content: str,
        spec: TargetSpec,
        args: dict[str, Any],
        roster: list[CharacterEntry],
        user_naming: UserNamingScope | None,
        resolved: CharacterEntry | None,
    ) -> CharacterEntry | None:
        """判断是否该走 1.0.21 的入口②（**补入**）；返回要补入的角色或 ``None``。

        条件（全部满足）：

        1. 解析成功（``resolved`` 非空、且带标准标签）；
        2. 画面里**一个角色标签都没有** —— 用「**角色表内**角色的名字 / 别名 /
           标准标签在提示词里是否出现」判断（检测文本 = 提示词 + ``detect_args``，
           与既有的「已含角色」检测同一口径）。注意：**画面里那个表外角色的中文名
           不算** —— 描述里写了「刻律德拉」而没写任何标准标签时，仍然属于"没有标签"，
           正是本入口要处理的场景；
        3. 用户最近说过的话里**没有**这个解析结果（已经写过就没必要再补）。

        画面里已经有角色标签时不在这里动：那种情况属于**替换**（入口①，
        见 :meth:`_apply_outside_naming_check`），两条入口不会互相打架。
        """
        if resolved is None or not resolved.resolved_tag():
            return None
        if user_naming is not None and character_in_texts(resolved, user_naming.texts):
            return None
        texts = self._detection_texts(content, spec, args)
        if any(character_in_texts(entry, texts) for entry in roster):
            return None
        return resolved

    def _inject_resolved(
        self,
        *,
        content: str,
        spec: TargetSpec,
        args: dict[str, Any],
        resolved: CharacterEntry,
        config: CharPromptGuardConfig,
    ) -> None:
        """把解析结果按目标模式**补进**提示词（1.0.21 入口②）。

        只是 :meth:`_resolved_presentation` 的「写回 args」外壳 —— 与既有注入共用
        同一批模板 / 同一个 ``tag_completion`` 开关，**不新造一套格式**。
        """
        args[spec.prompt_arg] = self._resolved_presentation(
            content=content, spec=spec, args=args, resolved=resolved, config=config
        )

    def _resolved_presentation(
        self,
        *,
        content: str,
        spec: TargetSpec,
        args: dict[str, Any],
        resolved: CharacterEntry,
        config: CharPromptGuardConfig,
    ) -> str:
        """把解析结果的表现层合并进 ``content`` 并返回（1.0.21 入口①/②共用）。

        - ``natural``：复用既有的追加模板
          （``（画面角色：“<中文名>”，出自游戏《<作品名>》）``），并在
          ``tag_completion`` 开启时把转义标准标签**写进提示词最前面** ——
          实测下游翻译模型会把中文角色名翻坏，标签才是最稳的那一层；
        - ``tags``：直接复用既有的标签追加逻辑（``_apply``）。

        入口②（补入）与入口①（替换）都走这里：替换之后画面里只有模型给的英文标签，
        natural 模式若不补中文说明，用户看到的仍旧是"角色没进去"（实测 C72）。
        """
        if spec.append_mode == APPEND_MODE_TAGS:
            merged = self._apply(
                args=args,
                spec=spec,
                content=content,
                characters=[resolved],
                config=config,
                control=BotControl(),
            )
            return content if merged is None else merged
        clause = self._build_resolved_append_text(resolved, config)
        tag = resolved.resolved_tag()
        pieces = [content.rstrip()]
        if clause:
            pieces.append(clause)
        merged = "\n".join(piece for piece in pieces if piece)
        if (
            tag
            and config.general.tag_completion
            and not content_contains_tag(merged, tag)
        ):
            merged = f"{tag}, {merged}".strip().rstrip(",").rstrip()
        return merged

    def _build_resolved_append_text(
        self, resolved: CharacterEntry, config: CharPromptGuardConfig
    ) -> str:
        """:meth:`_build_append_text` 的**不会抛异常**版本（1.0.21）。

        解析出来的名字／作品名是**模型给的自由文本**：万一里面带 ``{`` ``}``，
        ``str.format`` 会抛 ``ValueError`` 甚至 ``KeyError``。这里兜住并退回
        「（画面角色：“<名字>”）」，绝不让一次格式化失败拦住生图。
        """
        try:
            return self._build_append_text(resolved, config)
        except Exception as e:  # noqa: BLE001 —— 模板填充失败就退回最简写法
            logger.debug(f"追加模板填充失败，退回最简写法: {type(e).__name__}: {e}")
            return f"（画面角色：“{resolved.name}”）"

    @staticmethod
    def _resolved_origin(entry: CharacterEntry) -> str:
        """解析结果的来源说明（进替换日志，1.0.21）。

        形如 ``刻律德拉 → Kleidra，作品《崩坏：星穹铁道 (Honkai: Star Rail)》``：
        英文名优先取别名里的 ASCII 写法（模型给的 ``name_en``），没有别名时退回
        标准标签；作品名用解析出的 ``game``。两份信息都取不到时只写名字 ——
        **绝不编造**（日志与画面用同一份数据，用户对着日志就能核对画面）。
        """
        english = next(
            (
                alias.strip()
                for alias in entry.aliases
                if alias.strip() and alias.strip().isascii()
            ),
            "",
        )
        origin = f"{entry.name} → {english or entry.resolved_tag()}"
        game = entry.game.strip()
        return f"{origin}，作品《{game}》" if game else origin

    async def _apply_outside_naming_check(
        self,
        *,
        content: str,
        spec: TargetSpec,
        args: dict[str, Any],
        roster: list[CharacterEntry],
        config: CharPromptGuardConfig,
        naming_scope: UserNamingScope | None = None,
        resolved: CharacterEntry | None = None,
    ) -> str:
        """「用户点名了角色表外角色」的冲突检测（1.0.20；1.0.21 起会**替换**）。

        与 :meth:`_apply_consistency_check`（期望集合对账）**完全独立、可各自触发**：

        - 期望集合对账问的是「画面里的角色是不是**这次该画的那个**」（依据 = 判定集合 /
          主题跟踪 / 记忆库）；
        - 本条问的是「画面里怎么挂着**用户这次没点过**的角色表内角色，而用户明明
          点了一个**不在角色表里**的角色」（依据 = 用户最近几条消息本身）。

        实测事故里前者三份证据全空 → 直接跳过；后者恰恰能看到问题，所以必须独立成条。

        判断（三者同时成立才算冲突）：

        1. 焦点范围内的用户消息里存在**表外点名** ``X``（:meth:`_scan_user_naming`）；
        2. **最终提示词**里出现了角色表内角色 ``Y``；
        3. 这批用户消息里**没有提过** ``Y``（用户这次没点它）。

        任一不成立 → 不判冲突，只记 DEBUG（保持 1.0.19 行为：用户点过画面的角色、
        或压根没有表外点名时，一个字都不动）。没有表外点名时，画面里的表内角色
        **都在用户自己说过的话里**的话仍报「正常」（画面与用户说法一致），
        否则才是真正的「跳过」（没有可对账的另一侧）。

        **1.0.21 起的 ``fix`` 分两种结局**：``general.auto_resolve_characters`` 开着
        且解析成功（``resolved`` 非空）时，把画面里的 ``Y`` **替换**成解析出的标签
        （而不是只删除），并按 :meth:`_resolved_presentation` 补上 natural 模式的
        中文说明与标准标签 —— 用户反馈的正是「删掉之后没有补上正确角色的标签」；
        解析失败（``unknown`` / 超时 / 标签非法）时逐字退回 1.0.20 的删除行为。

        Args:
            content: **已经是最终文本**（既有对账若修正过，这里是修正后的内容 ——
                本方法在 :meth:`_finalize` 里跑在 :meth:`_apply_consistency_check` 之后）
            resolved: 本次**预解析**出的表外角色（1.0.21；调用方只对第一个表外点名
                解析一次，``None`` 表示没解析或解析失败）
        """
        mode = (
            str(getattr(config.general, "consistency_check", "") or "").strip().lower()
        )
        if mode == "off" or not roster:
            return content
        if mode not in {"warn", "fix"}:
            # 非法取值在上面那条检查里已经记过 WARNING，这里静默按 warn 处理
            mode = "warn"
        texts = self._detection_texts(content, spec, args)
        if naming_scope is None or not naming_scope.names:
            present = [entry for entry in roster if character_in_texts(entry, texts)]
            named = {
                entry_key(entry)
                for entry in roster
                if naming_scope is not None
                and character_in_texts(entry, naming_scope.texts)
            }
            if present and all(entry_key(entry) in named for entry in present):
                # 没有表外点名，但画面里的表内角色**都是用户自己点过的**：
                # 画面与用户说法一致 —— 报「正常」，别让用户以为这条检查没跑
                logger.debug(
                    "表外点名检查=正常（画面里的角色表内角色都在用户最近说过的话里"
                    f"出现过：{_entry_names(present)}；表外点名=无）"
                )
            else:
                logger.debug(
                    "表外点名检查=跳过（用户最近的几条消息里没有角色表外的点名）"
                )
            return content

        present = [entry for entry in roster if character_in_texts(entry, texts)]
        # 冲突 = 画面里有、但用户这批消息里**没点过**的角色表内角色。
        # 1.0.21：刚由解析结果替换进来的那个角色不算冲突（它就是用户点名的角色）。
        # 规则与 _conflicting_roster_entries 完全一致（那边是"该不该解析"的预判）。
        conflicts = _conflicting_roster_entries(
            texts, roster, naming_scope, exclude=resolved
        )
        if not conflicts:
            logger.debug(
                "表外点名检查=正常（画面里的角色表内角色都在用户最近说过的话里"
                f"出现过：{_entry_names(present) or '无'}；表外点名="
                f"{naming_scope.names_text()}）"
            )
            return content

        outside = naming_scope.names_text()
        detail = (
            f"用户点名「{outside}」（不在角色表中），"
            f"但画面标签={'、'.join(_entry_label(entry) for entry in conflicts)}"
        )
        suggestion = "；建议：不要用备忘里的旧标签，只写画面描述"

        if mode != "fix":
            # warn（默认）：只报不改 —— 内容必须一字不变
            logger.warning(f"一致性检查=冲突（warn 不改内容）：{detail}{suggestion}")
            return content

        # fix ①：1.0.21 起优先**替换**（解析成功时）。
        # 为什么替换比删除好：用户要的就是「这个角色的提示词进去」—— 画面里那个过期的
        # 旧标签删掉却不补正确标签，用户侧看到的是「角色没进去」（实测反馈）。
        # 替换范围仍然**只用已知字符串**：错误角色的名字 / 别名 / 标准标签 →
        # 一个由模型解析、且已被规范校验过的标准标签（不猜、不编）。
        if resolved is not None and resolved.resolved_tag():
            replaced = content
            hits = 0
            for entry in conflicts:
                replaced, count = replace_character_reference(replaced, entry, resolved)
                hits += count
            if hits > 0 and not any(
                character_in_texts(entry, self._detection_texts(replaced, spec, args))
                for entry in conflicts
            ):
                # 依据必须体现**来源**：这次替换用的不是判定集合，而是角色解析(LLM)
                # 的结果（用户对着日志才能看出「凭什么把花火换成刻律德拉」）
                logger.info(
                    f"一致性检查=冲突已修正（fix）：把画面里的「{_entry_names(conflicts)}」"
                    f"替换为「{resolved.name or resolved.tag}」（替换 {hits} 处），"
                    f"依据=角色解析(LLM):{self._resolved_origin(resolved)}"
                )
                # natural 模式补中文说明 + 标准标签（tags 模式补标签）：
                # 否则画面里只剩模型给的英文标签，用户侧仍旧是"角色没进去"
                return self._resolved_presentation(
                    content=replaced,
                    spec=spec,
                    args=args,
                    resolved=resolved,
                    config=config,
                )

        # fix ②：解析失败（unknown / 超时 / 标签非法）或替换不成功时，
        # 逐字退回 1.0.20 的**删除**行为 —— 宁可不补，也不要留着错的标签。
        # 为什么是删除而不是拿别的角色顶上：插件手上没有表外角色的标准标签，
        # 拿另一个角色顶上只会错得更彻底。
        cleaned = content
        removed = 0
        for entry in conflicts:
            cleaned, hits = delete_character_reference(cleaned, entry)
            removed += hits
        remaining = sum(
            1 for char in cleaned if not char.isspace() and char not in ",，、;；"
        )
        if removed <= 0 or remaining < _MIN_FIX_CONTENT_CHARS:
            # 删不出可识别文本（角色表里的字面写法都没出现），或删完提示词就空了 ——
            # 那说明这段提示词的主体就是那个标签，删掉等于清空画面，一律不做
            logger.warning(
                f"一致性检查=冲突但无法安全删除（删除处数={removed}，剩余有效字符={remaining}），"
                f"退回 warn 行为、不改内容：{detail}{suggestion}"
            )
            return content

        logger.info(
            f"一致性检查=冲突已修正（fix）：删除画面里的「{_entry_names(conflicts)}」标签"
            f"（用户点名的是角色表外角色「{outside}」）"
        )
        return cleaned

    # ── 收尾：二次一致性检测 → 按需写回 ────────────────────────────

    async def _finalize(
        self,
        *,
        params: dict[str, Any],
        args: dict[str, Any],
        spec: TargetSpec,
        original: str,
        final_content: str,
        decided: list[CharacterEntry],
        roster: list[CharacterEntry],
        config: CharPromptGuardConfig,
        basis: str,
        track: SubjectTrack | None = None,
        memory: memory_api.MemoryLookup | None = None,
        naming_scope: UserNamingScope | None = None,
        resolved: CharacterEntry | None = None,
        outfit: ResolvedOutfit | None = None,
    ) -> tuple[EventDecision, dict[str, Any]]:
        """统一的收尾：跑二次一致性检测 → 追加皮肤 → 内容有变化才回写 args。

        ``track``（本次主题跟踪的完整结果）与 ``memory``（1.0.19 描述点名时的
        **对账用**记忆库查询结果）都只是给一致性检测提供「期望集合」的证据，
        不参与其它任何判断。

        **两条检查互相独立、按顺序各跑一次**（1.0.20）：

        1. :meth:`_apply_consistency_check`：与「期望集合」（判定集合 + 主题跟踪主题
           + 记忆库确认结果）对账；
        2. :meth:`_apply_outside_naming_check`：与「用户最近几条**用户消息**」对账
           （``naming_scope``）—— 用户点名了**角色表外**的角色、画面里却是用户
           这次没点过的角色表内角色时报警 / 替换 / 删除。

        第 2 条跑在第 1 条**之后**：第 1 条若已修正过内容，第 2 条按**修正后**的文本
        再判一次；两条都命中时日志各留一条，互不覆盖。

        **1.0.21**：``resolved`` 是本次预解析出的表外角色（见
        :meth:`_resolve_outside_character`），只交给第 2 条做**替换** + 补中文说明。

        **1.0.22**：``outfit`` 是本次预解析出的皮肤/时装（见 :meth:`_resolve_outfit`），
        由 :meth:`_apply_outfit` 追加在**最后一步** —— 两条一致性检查都跑完之后。
        这样安排有两个理由：① 皮肤关键词不是角色，不该参与角色对账（那两条检查
        只认角色表内的字符串）；② 任何角色修正（fix 替换 / 删除）都不会碰到它 ——
        「角色在前、皮肤在后」的次序也就自然成立。

        **1.0.23**：``outfit`` 也可能是「模型推断」出来的那一套
        （``outfits.infer_unnamed`` 开启、用户只说「<角色>的那一套皮肤」时，
        见 :meth:`_infer_unnamed_outfit`）—— 注入路径与 1.0.22 **完全相同**，
        这里一个字都不需要改。

        .. note::
           1.0.21 修正（实测回归）：解析结果**不再并进第 1 条的判定集合**。
           并进去时 ``fix`` 模式的对账会抢在入口①之前做替换，日志依据被写成
           ``判定集合=…``、natural 模式的中文说明也没补上（用例 C72 就是这么失败的）。
           对账只负责它自己的证据（判定集合 / 主题跟踪 / 记忆库）；解析结果的替换、
           中文说明与标准标签统一由入口①（:meth:`_apply_outside_naming_check`）处理，
           依据里就能看出「用的是角色解析(LLM)」。也不会因此误报：解析出的角色是
           角色表**之外**的，不可能被对账的 ``extras``（只遍历角色表）算成冲突。
        """
        checked = self._apply_consistency_check(
            content=final_content,
            spec=spec,
            args=args,
            roster=roster,
            decided=list(decided),
            config=config,
            basis=basis,
            track=track,
            memory=memory,
        )
        checked = await self._apply_outside_naming_check(
            content=checked,
            spec=spec,
            args=args,
            roster=roster,
            config=config,
            naming_scope=naming_scope,
            resolved=resolved,
        )
        checked = self._apply_outfit(
            content=checked,
            spec=spec,
            outfit=outfit,
            decided=list(decided),
        )
        if checked != original:
            args[spec.prompt_arg] = checked
            params["args"] = args
        return EventDecision.SUCCESS, params

    def _apply_consistency_check(
        self,
        *,
        content: str,
        spec: TargetSpec,
        args: dict[str, Any],
        roster: list[CharacterEntry],
        decided: list[CharacterEntry],
        config: CharPromptGuardConfig,
        basis: str,
        track: SubjectTrack | None = None,
        memory: memory_api.MemoryLookup | None = None,
    ) -> str:
        """二次一致性检测：对账「最终提示词里的角色表内角色」vs「**期望集合**」。

        - ``off``：不检查（也**不去查记忆库** —— 见 :meth:`_memory_reconcile`）；
        - ``warn``（默认）：提示词里出现期望集合之外的**角色表内**角色时打一条
          WARNING（同时写出画面那一侧与上下文 / 记忆那一侧的角色名与依据），内容不改；
        - ``fix``：仅当冲突是「画面里的角色 vs 期望角色」且双方都能唯一定位
          （期望集合恰好 1 人、冲突角色恰好 1 个）时，把画面里那个错误角色的
          名字/别名/标签替换成期望角色的（**替换范围仅限角色表里已知的字符串**），
          替换后按需补期望角色的标准标签；出现歧义或多角色无法定位 → 自动退回
          warn 行为并记 WARNING。

        **1.0.19 起**对账基准从「判定集合」扩成 :class:`ExpectationSet`
        （判定集合 + 主题跟踪主题 + 记忆库确认结果，见 :func:`build_expectation_set`）。
        原因：真实事故里判定集合恰好就是描述里那个**错的**角色（或干脆为空），
        「画面 = 判定」永远自洽，一次告警都没有。期望集合任意一份证据成立即可报冲突；
        **期望集合为空**（谁都没结论）时仍然跳过 —— 那种情况下提示词里的角色是描述
        自己写的，没有依据可对账，报警只会是噪音。

        .. note::
           1.0.20 起另有一条**独立**的检查 :meth:`_apply_outside_naming_check`
           （「用户点名了角色表外角色 vs 画面里的表内角色」），它不看期望集合，
           因此本方法「跳过」时它照样会跑 —— 实测事故正是「期望集合为空」那种情况。
           两条可以各自触发，日志各留一条。
        """
        mode = str(config.general.consistency_check or "").strip().lower()
        if mode == "off" or not roster:
            return content
        if mode not in {"warn", "fix"}:
            logger.warning(
                f"general.consistency_check={mode!r} 不是合法取值（off/warn/fix），按 warn 处理"
            )
            mode = "warn"

        expected = build_expectation_set(
            decided=decided, track=track, memory=memory, roster=roster
        )
        if not expected:
            logger.debug(
                "一致性检查=跳过（没有可对账的期望集合：本次判定为空，"
                "主题跟踪与记忆库也没有明确指向）"
            )
            return content

        texts = self._detection_texts(content, spec, args)
        expected_keys = expected.keys()
        extras = [
            entry
            for entry in roster
            if entry_key(entry) not in expected_keys
            and character_in_texts(entry, texts)
        ]
        if not extras:
            logger.debug(
                f"一致性检查=正常（提示词里的角色表内角色都在期望集合内：{expected.names()}）"
            )
            return content

        # 两侧角色名与依据都要写清楚：用户核对「到底谁跟谁冲突、凭什么」全靠这一行
        detail = (
            f"画面标签={'、'.join(_entry_label(entry) for entry in extras)}，"
            f"但上下文/记忆指向={expected.names()}（依据={expected.reason_text()}）"
        )
        suffix = f"；{basis}" if basis else ""

        if mode != "fix":
            logger.warning(f"一致性检查=冲突（warn 不改内容）：{detail}{suffix}")
            return content

        if len(expected.entries) != 1 or len(extras) != 1:
            logger.warning(
                "一致性检查=冲突但无法唯一定位"
                f"（期望集合 {len(expected.entries)} 个 / 冲突角色 {len(extras)} 个），"
                f"退回 warn 行为、不改内容：{detail}{suffix}"
            )
            return content

        wrong, right = extras[0], expected.entries[0]
        replaced, hits = replace_character_reference(content, wrong, right)
        if hits <= 0:
            logger.warning(
                f"一致性检查=冲突但未能定位可替换文本（角色表里没有「{wrong.name}」的字面写法），"
                f"退回 warn 行为、不改内容：{detail}{suffix}"
            )
            return content

        logger.warning(
            "一致性检查=冲突已修正（fix）：把画面里的"
            f"「{wrong.name or wrong.tag}」替换为「{right.name or right.tag}」"
            f"（替换 {hits} 处），依据={expected.reason_text()}{suffix}"
        )
        # 替换后按需补正确角色的标准标签（沿用 tag_completion 逻辑）
        tags = self._completion_tags(replaced, [right], config)
        if tags:
            replaced = self._complete(spec, replaced, tags)
        return replaced

    @staticmethod
    def _resolve_multi(
        config: CharPromptGuardConfig,
        control: BotControl,
        candidates: list[CharacterEntry],
        llm_multi: bool,
        mentioned: int = 0,
        naming_multi: bool = False,
    ) -> bool:
        """决定本次是否按"多人同框"处理。

        优先级：主模型标记 > ``multi.decide``（bot/latest/always_multi） >
        ``multi.decision``（旧字段 off/always/bot）> 用户点名的多人语义 /
        LLM 判定 / 上文人数兜底。
        """
        if not config.multi.enabled:
            return False
        if control.force_multi is not None:
            return control.force_multi

        # 新字段 decide：latest=总是只补最近点名的那个；always_multi=总是多人
        decide = str(getattr(config.multi, "decide", "") or "").lower()
        if decide == "latest":
            return False
        if decide == "always_multi":
            return True

        # 旧字段 decision：保留兼容
        decision = str(config.multi.decision).lower()
        if decision == "off":
            return False
        if decision == "always":
            return True

        # "bot"：用户点名的多人语义 / LLM 判定 / 候选数量兜底
        if naming_multi:
            # 用户在同一条消息里点了 ≥2 个角色，或用了「还有/一起」这类追加词
            return True
        if llm_multi:
            return True
        if str(config.multi.scope).lower() == SCOPE_MULTI_ONLY:
            # 用「上文提到的角色数」判断，而不是「还缺几个」：
            # 上文提了 A+B、提示词已经写了 A 时，也要进多人路径把 B 补上。
            return mentioned >= 2
        return True

    @staticmethod
    def _mode_allowed(mode: Any, config: CharPromptGuardConfig) -> bool:
        """判断当前生图模式是否在生效范围内。"""
        allowed_raw = str(config.general.apply_modes or "")
        allowed = {
            part.strip().lower()
            for part in re.split(r"[,，]", allowed_raw)
            if part.strip()
        }
        if not allowed:
            return False
        mode_str = str(mode or "").strip().lower()
        if not mode_str:
            return True
        return mode_str in allowed

    async def _collect_context(
        self, params: dict[str, Any], config: CharPromptGuardConfig
    ) -> list[dict[str, Any]]:
        """收集最近上文消息（含触发本次生图的消息）。

        历史消息按 stream 做短 TTL 缓存（``general.history_cache_ttl``）：
        主模型一次发起多个生图时不必重复查库。**触发消息永远实时追加**，
        不吃缓存。
        """
        stream_id = resolve_stream_id(params)
        # 触发消息正文只从 params 取一处（_trigger_text），避免与自我指代扫描取到不同文本
        trigger_text = self._trigger_text(params)

        messages: list[dict[str, Any]] = []
        if not stream_id:
            # 读不到上文时把话说清楚：否则表现为"插件没反应"，极难排查
            logger.warning(
                "事件里没有会话 ID（既无 stream_id 也无可用的 message），"
                "本次只能依据触发消息本身判断角色；"
                "若框架补丁是旧版请更新（二版起会带 stream_id）"
            )
        if stream_id:
            ttl = float(config.general.history_cache_ttl)
            cached = _history_cache.get(stream_id) if ttl > 0 else None
            if cached is not None and (time.time() - cached[0]) < ttl:
                messages = list(cached[1])
            else:
                try:
                    # 取回窗口放宽到至少 24h / 200 条：history_limit 配得过小时，
                    # "几小时前明确点名的角色"会被整段截掉，导致判定只能靠猜
                    # （实测：history_limit=10 时把 3 小时前的"流萤"截没了）。
                    # 真正参与判定的条数仍由 multi.recent_messages 控制。
                    _wide_hours = max(float(config.general.history_hours), 24.0)
                    _wide_limit = max(int(config.general.history_limit), 200)
                    messages = await get_recent_messages(
                        stream_id,
                        hours=_wide_hours,
                        limit=_wide_limit,
                    )
                    logger.debug(
                        f"上文取回 {len(messages)} 条"
                        f"（窗口 {_wide_hours:g}h / 上限 {_wide_limit} 条）"
                    )
                except Exception as e:  # noqa: BLE001 —— 读不到历史就当没有上文，不影响生图
                    logger.debug(f"读取上文消息失败: {e}")
                if ttl > 0:
                    if len(_history_cache) >= _HISTORY_CACHE_LIMIT:
                        _history_cache.clear()
                    _history_cache[stream_id] = (time.time(), list(messages))

        if trigger_text:
            messages.append(
                {
                    "time": time.time(),
                    "sender_name": "用户",
                    # 触发消息一定是「进来的人话」，显式标成用户消息：判定依据才会
                    # 记成「用户点名」而不是退一步的「最近提及」
                    "is_bot": False,
                    "role": "user",
                    "processed_plain_text": trigger_text,
                }
            )
        logger.debug(
            f"上文收集: stream_id={stream_id[:8] or '<无>'} "
            f"消息数={len(messages)} 触发消息={trigger_text[:30]!r}"
        )
        return messages

    # ── 注入 ──────────────────────────────────────────────────────

    @staticmethod
    def _completion_tags(
        content: str, roster: list[CharacterEntry], config: CharPromptGuardConfig
    ) -> list[str]:
        """返回需要"补全"的标准标签。

        条件：角色表提供了标签、该角色（名字/别名/标签）已出现在画面描述里、
        但描述里**没有**那个转义正确的标准标签。这类情况说明下游翻译不可靠，
        补标签比补中文名更管用。
        """
        if not config.general.tag_completion or not roster:
            return []
        tags: list[str] = []
        for entry in roster:
            tag = entry.resolved_tag()
            if not tag:
                continue
            if not character_in_texts(entry, [content]):
                continue
            if content_contains_tag(content, tag):
                continue
            tags.append(tag)
        return tags

    @staticmethod
    def _complete(spec: TargetSpec, content: str, tags: list[str]) -> str:
        """把标准标签补到提示词末尾（保留原有描述），返回新内容。"""
        appended = ", ".join(tags)
        stripped = content.rstrip().rstrip(",").rstrip()
        logger.info(f"已补标准标签（{spec.action}）: {appended}")
        return f"{stripped}, {appended}" if stripped else appended

    def _apply(
        self,
        *,
        args: dict[str, Any],
        spec: TargetSpec,
        content: str,
        characters: list[CharacterEntry],
        config: CharPromptGuardConfig,
        control: BotControl,
    ) -> str | None:
        """按目标模式注入角色，返回新的提示词；无需修改时返回 None。"""
        if spec.append_mode == APPEND_MODE_TAGS:
            strategy = control.force_strategy or str(config.multi.strategy_neo).lower()
            use_slot = bool(spec.characters_arg) and (
                strategy == STRATEGY_CHARACTERS
                and len(characters) >= 2
                or control.force_strategy == STRATEGY_CHARACTERS
            )
            if use_slot and self._fill_characters_arg(args, spec, characters, config):
                return content
            return self._append_tags(content, characters)

        # natural 模式：追加中文说明，交由生图插件自己的翻译模型处理
        max_natural = max(1, int(config.multi.natural_max))
        entries = characters
        if len(entries) > max_natural:
            logger.warning(
                f"{spec.plugin or spec.action} 不支持多角色槽位，"
                f"本次只补前 {max_natural} 个角色"
            )
            entries = entries[:max_natural]
        clauses = [self._build_append_text(entry, config) for entry in entries]
        clauses = [clause for clause in clauses if clause]
        if not clauses:
            return None
        merged = f"{content.strip()}\n{''.join(clauses)}"
        # 角色表提供了标准标签时，顺带把转义标签也写上：实测下游翻译模型会把中文名
        # 翻坏（把「花火」翻成 hanabi (firefly) (honkai: star rail)），有标签才保险。
        if config.general.tag_completion:
            tags = [
                entry.resolved_tag()
                for entry in entries
                if entry.resolved_tag()
                and not content_contains_tag(merged, entry.resolved_tag())
            ]
            if tags:
                merged = f"{merged.rstrip().rstrip(',')}, {', '.join(tags)}"
        return merged

    @staticmethod
    def _append_tags(content: str, characters: list[CharacterEntry]) -> str | None:
        """把角色标签追加到标签串末尾。"""
        tags: list[str] = []
        for entry in characters:
            tag = entry.resolved_tag()
            if not tag:
                logger.info(
                    f"角色 {entry.name or '?'} 未提供角色标签，跳过注入"
                    "（请在角色表第 4 列填写标准标签，如 elysia \\(honkai impact 3rd\\)）"
                )
                continue
            if content_contains_tag(content, tag):
                continue
            tags.append(tag)
        if not tags:
            return None
        stripped = content.rstrip().rstrip(",").rstrip()
        joined = ", ".join(tags)
        return f"{stripped}, {joined}" if stripped else joined

    def _fill_characters_arg(
        self,
        args: dict[str, Any],
        spec: TargetSpec,
        characters: list[CharacterEntry],
        config: CharPromptGuardConfig,
    ) -> bool:
        """把角色写入独立角色槽位参数；成功返回 True。"""
        existing = self._parse_existing_characters(args.get(spec.characters_arg))
        if existing is None:
            logger.warning(
                f"{spec.characters_arg} 不是合法 JSON，退回追加标签方式，不做覆盖"
            )
            return False

        existing_keys = {
            normalize_prompt_text(str(item.get("prompt") or "")) for item in existing
        }
        added = 0
        for entry in characters:
            tag = entry.resolved_tag()
            if not tag:
                logger.info(
                    f"角色 {entry.name or '?'} 未提供角色标签，跳过角色槽位注入"
                )
                continue
            key = normalize_prompt_text(tag)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            existing.append({"prompt": tag})
            added += 1

        if added == 0:
            return False

        if str(config.multi.coord_plan).lower() == "spread" and len(existing) > 1:
            total = len(existing)
            for index, item in enumerate(existing):
                if item.get("x") is None:
                    item["x"] = round((index + 1) / (total + 1), 3)
                if item.get("y") is None:
                    item["y"] = 0.5

        args[spec.characters_arg] = json.dumps(existing, ensure_ascii=False)
        return True

    @staticmethod
    def _parse_existing_characters(raw: Any) -> list[dict[str, Any]] | None:
        """解析已存在的角色槽位参数；非法时返回 None。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return []
        if isinstance(raw, list):
            data: Any = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return None
        else:
            return None

        if not isinstance(data, list):
            return None
        result: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                result.append(dict(item))
        return result

    @staticmethod
    def _build_append_text(
        character: CharacterEntry, config: CharPromptGuardConfig
    ) -> str:
        """根据配置模板生成追加的角色说明文本。"""
        game = character.game.strip()
        template = (
            config.general.append_format
            if game
            else config.general.append_format_no_game
        )
        try:
            return template.format(name=character.name.strip(), game=game)
        except (KeyError, ValueError):
            return template

    # ── 皮肤/时装的注入（1.0.22）───────────────────────────────────

    @staticmethod
    def _outfit_clause(outfit: ResolvedOutfit) -> str:
        """natural 模式的皮肤说明：``（皮肤/时装：“粉色妖精小姐”：pink elf dress, fairy wings）``。

        皮肤名与关键词**必须在里面**（用户对着日志和画面就能核对"到底加没加进去"）；
        中文描述有就一并带上（下游翻译模型还能据此补细节），没有就只写关键词。
        """
        keywords = outfit.keywords_text()
        if outfit.description:
            return OUTFIT_NATURAL_TEMPLATE_WITH_DESC.format(
                name=outfit.name, keywords=keywords, description=outfit.description
            )
        return OUTFIT_NATURAL_TEMPLATE.format(name=outfit.name, keywords=keywords)

    def _apply_outfit(
        self,
        *,
        content: str,
        spec: TargetSpec,
        outfit: ResolvedOutfit | None,
        decided: list[CharacterEntry],
    ) -> str:
        """把皮肤/时装**追加**在角色之后（1.0.22）；没有可注入内容时原样返回。

        这是 1.0.22 新增的**唯一**改动点：角色那一侧一个字都没改，皮肤只是
        「多追加一层」。规则：

        - ``natural``（nai_artist）：另起一行追加
          ``（皮肤/时装：“<皮肤名>”：<关键词>）``，位置在角色中文说明之后；
        - ``tags``（image_generator_plugin-neo）：把解析出的关键词追加进**同一个**
          参数字符串（``content_description``）末尾（``, pink elf dress, fairy wings``），
          与角色标签同处一个槽位；已经在提示词里的关键词不重复追加；
        - **没有解析出角色**（``decided`` 为空）时照样注入关键词，只打一条 DEBUG
          说明「无角色，仅注入皮肤关键词」—— 用户说「穿这套」时外观描述里通常
          已经写了角色（此时角色在画面里、只是判定链没给结论）；
        - 注入失败 / 解析失败 / 开关关掉时 ``outfit`` 为 ``None``，本方法原样返回，
          **绝不影响角色注入，也绝不拦住生图**。
        """
        if outfit is None or not outfit.ok:
            return content
        owner = next((entry.name for entry in decided if entry.name), "")
        if owner:
            logger.debug(f"皮肤注入: {outfit.name}（搭配角色={owner}）")
        else:
            logger.debug(f"皮肤注入: {outfit.name}（无角色，仅注入皮肤关键词）")

        if spec.append_mode == APPEND_MODE_TAGS:
            missing = [
                keyword
                for keyword in outfit.keywords
                if not content_contains_tag(content, keyword)
            ]
            if not missing:
                logger.debug(f"皮肤关键词已在提示词里，不重复追加: {outfit.name}")
                return content
            joined = ", ".join(missing)
            stripped = content.rstrip().rstrip(",").rstrip()
            merged = f"{stripped}, {joined}" if stripped else joined
            logger.info(
                f"已追加皮肤/时装（{spec.action} / tags）: {outfit.name} → {joined}"
            )
            return merged

        clause = self._outfit_clause(outfit)
        if clause in content:
            return content
        merged = f"{content.rstrip()}\n{clause}" if content.strip() else clause
        logger.info(
            f"已追加皮肤/时装（{spec.action} / natural）: "
            f"{outfit.name} → {outfit.keywords_text()}"
        )
        return merged
