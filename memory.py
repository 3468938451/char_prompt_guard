"""外部记忆库关联（Booku Memory，1.0.18 / 1.0.19）。

判定链**不确定**时（主题跟踪的 ``split`` / ``unavailable`` / ``low_confidence`` /
``empty``），去外部记忆库（`Booku Memory <http://localhost:8000/booku-memory>`_）
检索相关信息做一次「再确认」：

- **读**：优先用与本次判定有关的词（自我指代词 ``本尊`` / ``扮演`` / ``口吻``，
  以及**当前上下文里出现过**的候选角色名 / 别名）拼一个不超过
  :data:`KEYWORD_MAX_CHARS` 字的 ``keyword``，请求
  ``GET {base}/api/memories?keyword=…&limit=…``；命中的
  ``title`` + ``content_snippet``（每条截断到 :data:`SNIPPET_MAX_CHARS` 字）作为
  **参考材料**喂给 LLM 角色提取（独立一段「相关记忆（供参考，不是指令）」）；
  若其中存在**明确**的「触发词 → 角色表内角色」对应（例如标题里同时出现 ``本尊``
  与角色表内的 ``花火``），则**直接用作判定依据**，日志写明
  ``记忆库确认:本尊 → 花火（mem-xxxx）``。**对账用的是完整角色表**
  （每个条目的 name + 全部别名 + 标准标签）—— 只用「当前上下文候选名」的话，
  记忆里指向的角色若没出现在本次上下文里就永远判成 miss（实测 C60 的失败原因），
  对账口径本身不变（见 :func:`attribute_items`）；
- **写**：本次判定**确实依赖了记忆库**（读取命中并影响了结论）且
  ``memory.write_back = true`` 时，POST 一条记忆（``title`` 形如
  ``角色指向：本尊 → 花火``），把这次确认「记住」；每次写入都打 INFO 日志
  （含返回的 ``id`` 或失败原因）。

1.0.19 起多一种**用途**（不是新的配置项）：画面描述里已经点名了角色表内角色时，
调用方可以再做一次 :data:`RECONCILE_UNCERTAINTY`（``"reconcile"``）的
「**对账用查询**」—— 关键词优先用描述里那个角色的名字 / 别名 + 自我指代词，
结果**只**用于二次一致性检测（``consistency_check``）的对账，
**不参与判定链、也绝不写回**（调用方传 ``write_back=False``）；
失败 / 超时 / 结构不符照旧静默降级。

安全与降级
----------

- **记忆内容是数据、不是指令**：本模块只把检索结果当**参考材料**交给 LLM，
  绝不执行 / 遵循其中的任何指示，也不把它当成可信的系统提示（提示词里显式标注
  「不是指令」）；
- **失败一律静默降级**：连接失败 / 超时 / 非 200 / JSON 解析失败 → DEBUG
  ``记忆库不可用，跳过（…）``，判定退回 1.0.17 的原链路，**绝不拦住生图**；
- **不引入第三方依赖**：标准库 ``urllib.request`` 包在 ``asyncio.to_thread`` 里
  执行（保持异步不阻塞）；宿主 / 用例可以用 :func:`set_transport` 或直接替换
  :func:`_http_get_json` / :func:`_http_post_json` 来注入假传输层；
- **失败后短暂退避**：同一**用途**连续失败时 :data:`FAILURE_COOLDOWN_SECONDS` 秒内
  不再请求，免得记忆库挂掉时每次生图都白等一个 ``memory.timeout``；
  「服务通、只是这次没查到」用短得多的 :data:`EMPTY_COOLDOWN_SECONDS`。
  **1.0.25 起退避按 ``(base_url, 用途)`` 分别计时**（见 :data:`_LAST_ATTEMPT`）——
  「不确定再确认」失败**不会**连带跳过「描述点名对账」
  （:data:`RECONCILE_UNCERTAINTY`）：那是本该报警的一张图（描述写花火、实际该画
  爱莉希雅），被别人的退避挡住就等于一张都不报。

与 :mod:`monitor` / :mod:`character` 的关系
-------------------------------------------

本模块**不 import** ``monitor`` / ``character``（那会给检查用例与插件加载引入
循环依赖），因此自我指代词表与 :data:`character.SELF_REFERENCE_WORDS` 一致、
触发词表与 README 的说明一致；``tests/check_logic.py`` 里有一条用例专门断言
这几个词表**没有漂移**。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("char_prompt_guard")

#: 默认记忆库地址（``memory.base_url`` 的默认值，可配置）
DEFAULT_BASE_URL = "http://localhost:8000/booku-memory"

#: 只接受这两种协议
_ALLOWED_SCHEMES = ("http", "https")

#: ``keyword`` 的长度上限（字符）：再长对检索没有帮助，还容易被网关截断
KEYWORD_MAX_CHARS = 60

#: 单条 ``content_snippet`` 截断长度（字符）
SNIPPET_MAX_CHARS = 200

#: 检索条数上限（``memory.limit`` 超过它也按它处理）
LIMIT_MAX = 20

#: **请求失败**（超时 / 连不上 / 非 200 / JSON 坏）后的退避秒数：
#: 这段时间内**同一个用途**不再请求记忆库。
#:
#: 没有它的话，记忆库挂掉时**每次生图**都要白等一个 ``memory.timeout``
#: （默认 2 秒）—— 直接违背「绝不拦住生图」。
#:
#: 1.0.25 起窗口是**按用途分键**的（``(base_url, uncertainty)``，见
#: :data:`_LAST_ATTEMPT`）：「描述点名对账」不会被「不确定再确认」的失败连带跳过。
FAILURE_COOLDOWN_SECONDS = 60.0

#: **检索成功但没命中**（服务正常、只是这次没查到）后的退避秒数（短得多）。
#:
#: 与「连不上」分开：没命中往往只是这次的关键词不好，不该让记忆库**一分钟内**
#: 完全用不了（用户的下一句很可能就查得到）。
#:
#: .. note::
#:    1.0.25 之前这一档**从未生效过**：失败类型记在 :class:`MemoryClient`
#:    实例上（``last_failure_kind``），而客户端是每次 :func:`build_client`
#:    **新建**的实例 —— 退避判断处读到的永远是空串，于是「服务通但没查到」
#:    也被按 :data:`FAILURE_COOLDOWN_SECONDS` 退避 60 秒。现在类型记在模块级
#:    :data:`_LAST_KIND`（与 :data:`_LAST_ATTEMPT` **同一个分键**），退避判断处
#:    按 ``(client.base_url, 本次用途)`` 读。
EMPTY_COOLDOWN_SECONDS = 15.0

#: 触发词（与自我指代词一致的那几个，外加角色扮演场景常用的两个）。
#:
#: :data:`character.SELF_REFERENCE_WORDS` 是判定链真正认的词；``扮演`` / ``口吻``
#: 只在**检索关键词**里用（用户说「按她的口吻」时也值得去记忆库翻一翻），
#: 它们**不参与任何判定**，只影响检索。
SPEAKER_TRIGGER_WORDS: tuple[str, ...] = (
    "本尊",
    "你本人",
    "你自己",
    "真正的你",
    "真身",
    "脱壳",
    "摘壳",
    "卸壳",
    "本体",
    "扮演",
    "口吻",
)

#: 总是优先进入检索关键词的高信号词（即使没有命中上面整词表）
_PRIMARY_KEYWORDS: tuple[str, ...] = ("本尊", "扮演", "口吻")

#: 描述里取词时忽略的高频词（这些词去记忆库检索只会带回噪音）
_STOPWORDS: frozenset[str] = frozenset(
    {
        "一个",
        "一张",
        "一起",
        "画面",
        "镜头",
        "帮忙",
        "帮我",
        "现在",
        "这个",
        "那个",
        "什么",
        "怎么",
        "可以",
        "就是",
        "然后",
        "还是",
        "已经",
        "自己",
        "照片",
        "图片",
        "少女",
    }
)

#: 描述取词用的「2 字以上中文片段」切分（英文/数字同样收进来）
_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}|[\u4e00-\u9fff]{2,}")

#: 记忆条目里可选的候选字段名（真实接口给的是 ``title`` / ``content_snippet`` /
#: ``content``；这里多认几个名字，免得对方改字段名就整条读不到）
_TITLE_KEYS = ("title", "name", "summary")
_CONTENT_KEYS = ("content_snippet", "snippet", "content", "text", "body")

#: 写入记忆时的固定标签（供检索与人工核对）
CORE_TAG = "角色指向"
#: 1.0.22：写入「皮肤/时装」解析结果时用的固定标签（与 :data:`CORE_TAG` 并列，
#: 方便在记忆库里把两类结论分开检索）
OUTFIT_CORE_TAG = "皮肤指向"
#: 写入记忆时 ``status`` 的取值
WRITE_STATUS = "active"
#: 写入记忆内容里的来源标注
WRITE_SOURCE = "char_prompt_guard 自动确认"

#: 1.0.19：**对账用查询**的触发类型（``lookup(uncertainty=…)`` 的取值之一）。
#:
#: 与 ``split`` / ``unavailable`` / ``low_confidence`` / ``empty`` 不同，它不是
#: 「判定不确定」，而是「画面描述里已经点名了角色表内角色，需要与上下文 / 记忆对账」
#: （见 ``handler._memory_reconcile``）。这种查询**只用于二次一致性检测**，
#: 结果不参与判定链、**不写回**（``write_back=False``），失败照旧静默降级。
RECONCILE_UNCERTAINTY = "reconcile"


# ── 结果类型 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MemoryCharacter:
    """记忆库「确认」出来的角色（字段与 ``character.CharacterEntry`` 对齐）。

    刻意**不复用** ``CharacterEntry``：本模块不 import ``character``，
    免得给插件加载与检查用例引入循环依赖。调用方按字段构造自己的类型即可。
    """

    name: str = ""
    game: str = ""
    aliases: tuple[str, ...] = ()
    tag: str = ""

    def label(self) -> str:
        """可读名字（进日志）。"""
        return self.name or self.tag or "?"


@dataclass(frozen=True)
class MemoryItem:
    """一条命中的记忆（已截断）。"""

    id: str = ""
    title: str = ""
    content: str = ""
    bucket: str = ""
    memory_type: str = ""
    status: str = ""

    def label(self) -> str:
        """可读标题（没有标题时退回首行内容）。"""
        return self.title or self.content[:30] or "(无标题)"


@dataclass(frozen=True)
class MemoryAttribution:
    """一条记忆里「触发词 → 角色表内角色」的对应关系。"""

    trigger: str
    character: MemoryCharacter
    item: MemoryItem


@dataclass
class MemoryLookup:
    """一次「记忆库再确认」的完整结果。

    Attributes:
        status: ``disabled`` / ``skipped`` / ``unavailable`` / ``miss`` / ``hit``
        characters: 命中的角色（``hit`` 时非空；按角色表顺序，去重）
        trigger_word: 命中的触发词（如 ``本尊``）
        token: 命中的角色名 / 别名（如 ``花火``）
        items: 命中的记忆条目（已截断，最多 ``memory.limit`` 条）
        references: 给 LLM 的参考材料文本（``miss`` / 不可用时为空串）
        reason: 失败 / 跳过的具体原因（进 DEBUG 日志）
        write_error: 写回这一步的失败原因（成功或没写时为空串）
    """

    status: str = "skipped"
    characters: list[MemoryCharacter] = field(default_factory=list)
    trigger_word: str = ""
    token: str = ""
    items: list[MemoryItem] = field(default_factory=list)
    references: str = ""
    reason: str = ""
    write_error: str = ""

    @property
    def hit(self) -> bool:
        """是否命中「触发词 → 角色表内角色」的明确对应。"""
        return self.status == "hit" and bool(self.characters)

    def confirm_log(self) -> str:
        """日志文案：``记忆库确认:本尊 → 花火（mem-…）``。"""
        names = "、".join(character.label() for character in self.characters)
        where = self.items[0].id if self.items else ""
        return f"记忆库确认:{self.trigger_word} → {names}" + (
            f"（{where}）" if where else ""
        )


# ── 传输层（可注入，供用例替换） ───────────────────────────────────────


@dataclass
class MemoryTransport:
    """一次「读取 + 写入」用到的传输函数对（默认标准库实现，可替换）。"""

    get: Callable[..., Any] | None = None
    post: Callable[..., Any] | None = None


_transport = MemoryTransport()


def set_transport(
    getter: Callable[..., Any] | None = None,
    poster: Callable[..., Any] | None = None,
) -> None:
    """替换 / 清空传输层（用例用；``getter`` / ``poster`` 传 ``None`` 表示用默认实现）。

    默认实现就是本模块的 :func:`_http_get_json` / :func:`_http_post_json`；
    用例也可以直接 monkeypatch 那两个模块级函数 —— 客户端每次调用时都会
    **重新取一次模块属性**，两种注入方式都生效。
    """
    _transport.get = getter
    _transport.post = poster


def _urlopen(url: str, timeout: float) -> Any:
    """打开 URL（**显式不使用任何代理**）。

    记忆库通常是本机 / 局域网服务，而 ``urllib.request.urlopen`` 默认会读
    ``http_proxy`` / ``https_proxy`` 环境变量 —— 一旦机器上配了全局代理，
    请求 localhost 也会被塞给代理（实测表现是「莫名超时」而不是连接被拒）。
    这里用 ``ProxyHandler({})`` 显式关掉代理，并把超时透传给 ``open()``。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def _default_get(url: str, timeout: float) -> Any:
    """默认 GET：标准库 ``urllib.request``（同步，调用方包在 ``to_thread`` 里）。"""
    with _urlopen(url, timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _default_post(url: str, payload: dict[str, Any], timeout: float) -> Any:
    """默认 POST：JSON 体 + ``application/json`` 头。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with _urlopen(request, timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _http_get_json(url: str, timeout: float) -> Any:
    """GET JSON（走当前传输层）。**任何失败都抛异常，由调用方兜底。**"""
    getter = _transport.get or _default_get
    return getter(url, timeout)


def _http_post_json(url: str, payload: dict[str, Any], timeout: float) -> Any:
    """POST JSON（走当前传输层）。**任何失败都抛异常，由调用方兜底。**"""
    poster = _transport.post or _default_post
    return poster(url, payload, timeout)


async def _call(func: Callable[..., Any], *args: Any) -> Any:
    """把同步的传输函数放到线程里执行（不阻塞事件循环）。"""
    return await asyncio.to_thread(func, *args)


# ── 失败退避（模块级状态，避免"记忆库挂掉时每次生图都白等"） ──────────

#: ``(base_url, 用途)`` → 最近一次「没拿到结果」的时间戳
#:
#: 1.0.25 起**按用途分键**：早先只按 ``base_url`` 共享时，一次「不确定再确认」超时
#: 会让同一分钟内的「描述点名对账」也一起被跳过 —— 而那条对账是唯一能发现
#: "画面标签与描述不一致"的通道，等于静默关掉了它。
_LAST_ATTEMPT: dict[tuple[str, str], float] = {}

#: ``(base_url, 用途)`` → 上次「没拿到结果」的类型（``error`` / ``empty``）。
#: 必须是模块级状态：client 是每次用完就丢的，写在实例上会让退避判断永远读到空串，
#: 于是 ``EMPTY_COOLDOWN_SECONDS``（15s）这一档从来没生效过。
_LAST_KIND: dict[tuple[str, str], str] = {}


def reset_state() -> None:
    """清空「失败退避」状态（只给用例用）。"""
    _LAST_ATTEMPT.clear()
    _LAST_KIND.clear()


def _cooldown_key(base_url: str, uncertainty: str = "") -> tuple[str, str]:
    """退避键 = ``(base_url, 用途)``（用途为空串时退化为"通用"一档）。"""
    return (str(base_url or ""), str(uncertainty or ""))


def _cooldown_remaining(
    key: tuple[str, str], cooldown: float, now: float | None = None
) -> float:
    """距离上次失败是否还在退避窗口内；返回剩余秒数（0 表示可以请求）。"""
    stamp = _LAST_ATTEMPT.get(key)
    if stamp is None:
        return 0.0
    remaining = cooldown - ((now if now is not None else time.time()) - stamp)
    return max(0.0, remaining)


def _mark_attempt(key: tuple[str, str], kind: str = "error") -> None:
    """记一次「没拿到结果」的时间与类型（退避窗口起点）。"""
    _LAST_ATTEMPT[key] = time.time()
    _LAST_KIND[key] = str(kind or "error")


# ── 配置读取（任何字段都容错） ────────────────────────────────────────


def _as_bool(value: Any, default: bool = True) -> bool:
    """把配置值当布尔读（兼容被写成字符串的 ``"false"`` / ``"0"`` / ``"no"`` / ``"off"``）。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    """把配置值当浮点读；非数字 / 非正数时用默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_int(value: Any, default: int) -> int:
    """把配置值当整数读；非数字 / 非正数时用默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def memory_enabled(config: Any) -> bool:
    """``memory.enabled`` 是否为真（配置对象缺失 / 字段缺失时按**默认开**处理）。"""
    section = getattr(config, "memory", None)
    return _as_bool(getattr(section, "enabled", None))


def normalize_base_url(raw: Any) -> str:
    """把 ``memory.base_url`` 归一化成「无结尾斜杠」的地址；不合法时返回空串。

    - 只接受 ``http`` / ``https``（``file:`` / ``ftp:`` 之类一律拒绝）；
    - 结尾斜杠容错（``…/booku-memory/`` 与 ``…/booku-memory`` 等价）；
    - 空值回落到 :data:`DEFAULT_BASE_URL`。
    """
    text = str(raw or "").strip() or DEFAULT_BASE_URL
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.netloc:
        return ""
    return text.rstrip("/")


@dataclass
class MemoryClient:
    """一个记忆库客户端（由 :func:`build_client` 按配置构造）。"""

    base_url: str = DEFAULT_BASE_URL
    timeout: float = 2.0
    limit: int = 5
    write_back: bool = True
    bucket: str = "memory"
    memory_type: str = "knowledge"
    invalid_base_url: bool = False
    #: **本次**检索的「没拿到结果」类型：``error``（连不上/超时/非 200/JSON 坏）
    #: 或 ``empty``（服务正常但没命中）—— 两者用不同的退避窗口。
    #:
    #: .. note::
    #:    退避状态**不在这个实例上**：客户端每次 :func:`build_client` 都是**新建**
    #:    实例，写在这里的字段下一次调用就没了（1.0.25 修的正是这个坑 —— 退避判断
    #:    处曾经读它，于是永远读到空串）。读取失败时 :func:`lookup` 立刻把类型抄进
    #:    模块级的 :data:`_LAST_KIND`；这里保留它只为「本次调用」的观测与日志。
    last_failure_kind: str = ""

    # ── 检索 ──────────────────────────────────────────────────────────

    async def search(self, keyword: str) -> list[MemoryItem]:
        """按关键词检索记忆。

        Returns:
            命中的记忆条目（已截断，最多 :attr:`limit` 条）；
            **任何失败都返回空列表**，只打 DEBUG ``记忆库不可用，跳过（原因）``，
            并把 :attr:`last_failure_kind` 记成 ``error`` / ``empty``（**本次调用**
            的观测值；退避状态由 :func:`lookup` 按 ``(base_url, 用途)`` 记在模块级，
            见 :data:`_LAST_ATTEMPT` / :data:`_LAST_KIND`）。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            logger.debug("记忆库检索跳过：没有可用的关键词")
            return []
        query = urllib.parse.urlencode(
            {"keyword": keyword, "limit": max(1, min(int(self.limit), LIMIT_MAX))}
        )
        url = f"{self.base_url}/api/memories?{query}"
        try:
            payload = await _call(_http_get_json, url, float(self.timeout))
        except Exception as e:  # noqa: BLE001 —— 连接失败/超时/非 200/JSON 坏都走这里
            logger.debug(f"记忆库不可用，跳过（{_failure_reason(e)}）")
            # 退避由 lookup() 按 (base_url, 用途) 记录；这里只留下"失败类型"信号
            self.last_failure_kind = "error"
            return []
        items = parse_memory_items(payload)
        if not items:
            # 服务可能是好的，只是这次没命中：退避窗口比「连不上」短得多，
            # 别因为一句没查到就把记忆库锁死一分钟（用户的下一句很可能就查得到）
            logger.debug(f"记忆库检索无结果（关键词={keyword!r}）")
            # 同上：不在 client 侧记退避（client 用完即丢，记了也读不到）
            self.last_failure_kind = "empty"
        else:
            self.last_failure_kind = ""
            logger.debug(
                f"记忆库检索到 {len(items)} 条（关键词={keyword!r}）："
                f"{[item.label() for item in items]}"
            )
        return items[: max(1, min(int(self.limit), LIMIT_MAX))]

    # ── 写入 ──────────────────────────────────────────────────────────

    async def write(
        self,
        *,
        trigger_word: str,
        character: MemoryCharacter,
        game: str = "",
        tag: str = "",
        scenario: str = "",
        title: str = "",
        core_tag: str = CORE_TAG,
    ) -> str:
        """把一次「确认」写回记忆库（记住）。

        Args:
            title: 覆盖默认标题（``角色指向：<触发词> → <角色名>``）。1.0.22 起
                皮肤/时装解析写回时传 ``皮肤指向：<皮肤名> → <角色名>``
            core_tag: ``core_tags`` 的第一项，默认 :data:`CORE_TAG`（1.0.22 起皮肤
                写回传 :data:`OUTFIT_CORE_TAG`）。三元组仍**同时非空**
                （``core_tags`` / ``diffusion_tags`` / ``opposing_tags``）

        Returns:
            成功时返回记忆 ``id``（响应里没有 ``id`` 时返回 ``"?"``）；
            失败返回空串，并打一条 INFO 日志说明原因（**绝不影响生图**）。
        """
        memory_title = (title or "").strip() or write_title(
            trigger_word, character.label()
        )
        payload: dict[str, Any] = {
            "title": memory_title,
            "content": write_content(
                trigger_word=trigger_word,
                character=character,
                game=game,
                tag=tag,
                scenario=scenario,
            ),
            "bucket": self.bucket,
            "memory_type": self.memory_type,
            "status": WRITE_STATUS,
            "core_tags": [core_tag, trigger_word, character.label()],
            "diffusion_tags": ["角色指向", "photo", "标签", "角色表"],
            "opposing_tags": ["标签误用", "角色混淆", "张冠李戴"],
        }
        url = f"{self.base_url}/api/memories"
        try:
            response = await _call(_http_post_json, url, payload, float(self.timeout))
        except Exception as e:  # noqa: BLE001 —— 写失败只记日志
            reason = _failure_reason(e)
            logger.info(f"记忆库写入失败（{memory_title}）：{reason}")
            return ""
        memory_id = _response_id(response)
        logger.info(f"记忆库已记住：{memory_title}（id={memory_id or '未知'}）")
        return memory_id or "?"


def build_client(config: Any) -> MemoryClient:
    """按 ``[memory]`` 配置构造客户端；``base_url`` 不合法时标记后静默关闭。

    ``base_url`` 不以 ``http`` / ``https`` 开头时**完全不发请求**（连读取都不做），
    只打一条 WARNING 说明 —— 用户把地址写错时不该表现成"插件卡住"。
    """
    section = getattr(config, "memory", None)
    base_url = normalize_base_url(getattr(section, "base_url", None))
    invalid = not base_url
    if invalid:
        logger.warning(
            "memory.base_url 不是合法的 http/https 地址"
            f"（{getattr(section, 'base_url', None)!r}），本次不访问记忆库"
        )
        base_url = DEFAULT_BASE_URL
    return MemoryClient(
        base_url=base_url,
        timeout=_as_float(getattr(section, "timeout", None), 2.0),
        limit=_as_int(getattr(section, "limit", None), 5),
        write_back=_as_bool(getattr(section, "write_back", None)),
        bucket=str(getattr(section, "bucket", None) or "memory").strip() or "memory",
        memory_type=(
            str(getattr(section, "memory_type", None) or "knowledge").strip()
            or "knowledge"
        ),
        invalid_base_url=invalid,
    )


# ── 解析与文案 ────────────────────────────────────────────────────────


def _first_str(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    """从字典里按候选键名取第一个非空字符串。"""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _metadata_of(raw: dict[str, Any]) -> dict[str, Any]:
    """取 ``metadata``（不是字典时返回空字典）。"""
    metadata = raw.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def parse_memory_items(payload: Any) -> list[MemoryItem]:
    """把接口返回的 JSON 转成 :class:`MemoryItem` 列表（**结构不认识时返回空列表**）。

    只认 ``items`` 列表里的字典；``id`` / ``title`` / ``content_snippet`` 全部缺失
    的条目直接丢掉（没有可读内容的记忆对判定没有帮助）。

    结构对不上（顶层不是字典、或没有 ``items`` 列表）时额外打一条 WARNING ——
    否则表现是「记忆库明明有内容，插件却一直说没查到」，很难排查。
    """
    if not isinstance(payload, dict):
        logger.warning(
            "记忆库返回的结构不认识（顶层不是对象），本次跳过；"
            f"实际类型={type(payload).__name__}"
        )
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        logger.warning(
            "记忆库返回的结构里没有 items 列表，本次跳过；"
            f"顶层字段={sorted(payload)[:8]}（期望 {{'items': [...]}}）"
        )
        return []
    items: list[MemoryItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = _first_str(raw, _TITLE_KEYS)
        content = _first_str(raw, _CONTENT_KEYS)
        if not title and not content:
            continue
        metadata = _metadata_of(raw)
        items.append(
            MemoryItem(
                id=str(raw.get("id") or "").strip(),
                title=title,
                content=content[:SNIPPET_MAX_CHARS],
                bucket=str(metadata.get("bucket") or "").strip(),
                memory_type=str(metadata.get("memory_type") or "").strip(),
                status=str(metadata.get("status") or "").strip(),
            )
        )
    return items


def _failure_reason(exc: BaseException) -> str:
    """把异常翻译成可读原因（超时 / HTTP 状态码 / 其它）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"连接失败({exc.reason})"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "超时"
    if isinstance(exc, json.JSONDecodeError):
        return "JSON 解析失败"
    return f"{type(exc).__name__}: {exc}"


def _response_id(response: Any) -> str:
    """从写入响应里取记忆 ``id``（取不到返回空串）。"""
    if not isinstance(response, dict):
        return ""
    for key in ("id", "memory_id"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    item = response.get("item") or response.get("data")
    if isinstance(item, dict):
        return str(item.get("id") or "").strip()
    return ""


# ── 关键词构造 ────────────────────────────────────────────────────────


def _dedupe(words: list[str]) -> list[str]:
    """去空白、去重、保序。"""
    result: list[str] = []
    for word in words:
        text = str(word or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def speaker_keywords(text: str) -> list[str]:
    """文本里命中的触发词（长词优先，如 ``你自己`` 不会被 ``你自己`` 里的短词截胡）。"""
    if not text:
        return []
    return [word for word in SPEAKER_TRIGGER_WORDS if word in text]


def keyword_limit() -> int:
    """检索关键词的长度上限。"""
    return KEYWORD_MAX_CHARS


def _fit_joint(words: list[str], limit: int = KEYWORD_MAX_CHARS) -> str:
    """把词用空格连起来，总长不超过 ``limit``（放不下就不放）。"""
    picked: list[str] = []
    total = 0
    for word in _dedupe(words):
        extra = len(word) + (1 if picked else 0)
        if total + extra > limit:
            continue
        picked.append(word)
        total += extra
    return " ".join(picked)


def build_keyword(
    uncertainty: str = "",
    *,
    reason: str = "",
    description: str = "",
    roster_names: list[str] | None = None,
    signal_label: str = "",
    limit: int = KEYWORD_MAX_CHARS,
) -> str:
    """拼出检索 ``keyword``；取不到有效关键词时返回空串（**此时不请求**）。

    优先级（各段内部去重、总长不超过 ``limit``，默认 :data:`KEYWORD_MAX_CHARS`）：

    1. ``reason`` / ``description`` 里命中的**触发词**（``本尊`` / ``扮演`` / ``口吻`` …）
       —— 不确定时最可能有用的检索锚点；
    2. ``signal_label``（主题跟踪给出的信号描述，例如 ``拆单→本尊+爱莉希雅``）里
       出现的角色表内角色名 / 别名；
    3. ``roster_names``：**当前上下文里出现过的**候选角色名 / 别名（宁少勿多，
       超过长度上限就截断，不会把整张角色表塞进去）；
    4. ``description`` 里切出来的候选词（去停用词、按长度倒序取几个）。

    Args:
        uncertainty: ``split`` / ``unavailable`` / ``low_confidence`` / ``empty``
        reason: 不确定的具体原因（主题跟踪的 ``reason``）
        description: 本次的画面描述
        roster_names: **当前上下文里出现过的**候选名 / 别名
            （注意：**只用于构造关键词、帮助检索**；对账「触发词 → 角色表内角色」
            必须用完整角色表，见 :func:`attribute_items`）
        signal_label: 最近一次信号的可读描述
        limit: 长度上限
    """
    names = [str(name) for name in (roster_names or []) if str(name).strip()]
    primary = _dedupe(
        speaker_keywords(reason)
        + speaker_keywords(description)
        + list(_PRIMARY_KEYWORDS)
    )
    context = f"{signal_label} {reason}"
    related = [name for name in names if name in context]
    tokens = [
        token
        for token in _TOKEN_PATTERN.findall(description or "")
        if token not in _STOPWORDS
    ]
    tokens.sort(key=len, reverse=True)
    keyword = _fit_joint(primary[:2] + related + names[:4] + tokens[:4], limit)
    if keyword:
        return keyword
    # 兜底：连触发词都没有时，至少用角色表里的候选名去碰一碰
    return _fit_joint(names[:4], limit)


def _uncertainty_word(reason: str, fallback: str = "本尊") -> str:
    """从不确定原因里取一个触发词（取不到时用 ``fallback``）。"""
    words = _dedupe(speaker_keywords(reason))
    return words[0] if words else fallback


# ── 「触发词 → 角色表内角色」的对账 ────────────────────────────────────


def _contains_keyword(text: str, keyword: str) -> bool:
    """关键词是否命中文本。

    与 ``character._keyword_hits`` 同一口径：纯 ASCII 关键词（``hanabi`` /
    ``elysia``）用单词边界，免得 ``hanabi`` 在 ``hanabix`` 里也算命中；
    中文 / 日文关键词直接子串匹配。
    """
    if not text or not keyword:
        return False
    lowered = keyword.lower()
    if keyword.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None
    return lowered in text.lower()


def _roster_hits(text: str, roster_names: list[str]) -> list[str]:
    """文本里命中的角色表关键词（按传入顺序，去重）。"""
    if not text:
        return []
    hits: list[str] = []
    for name in roster_names:
        token = str(name or "").strip()
        if not token or token in hits:
            continue
        if _contains_keyword(text, token):
            hits.append(token)
    return hits


def attribute_items(
    items: list[MemoryItem],
    *,
    trigger_word: str,
    roster_names: list[str],
    name_lookup: Callable[[str], MemoryCharacter | None],
) -> list[MemoryAttribution]:
    """在记忆条目里找「触发词 → 角色表内角色」的**明确**对应。

    判定口径（宁少勿多）：同一条记忆的 ``title + content`` 里**同时**出现
    ``trigger_word`` 与某个角色表内关键词，才算一次明确对应；一条记忆里出现多个
    **角色**时**整条作废**（歧义 → 不猜），由调用方退回 1.0.17 的原链路。

    .. note::
       1.0.19 起「多个角色」按 ``name_lookup`` **解析出的角色**去重，而不是按命中的
       字符串个数：一个角色在角色表里同时写了 ``爱莉希雅``（名字）与 ``爱莉``（别名）
       时，记忆里出现 ``爱莉希雅`` 会**同时**命中这两个关键词 —— 旧口径会把这种
       「同一个角色的名字 + 名字的子串」误判成两个候选、整条作废，于是中文全名的
       记忆**永远确认不了**（1.0.19 的「对账用查询」正要用它）。现在先把命中的关键词
       逐个交给 ``name_lookup`` 解析成角色，再按角色去重，仍然只有「恰好一个角色」
       才算明确对应；解析不出来的关键词一律忽略。

    Args:
        items: 命中的记忆条目
        trigger_word: 触发词（如 ``本尊``）
        roster_names: **完整角色表**里所有参与匹配的名字 / 别名 / 标准标签
            —— 必须是整张角色表：记忆里指向的角色若不在本次上下文候选里，
            只传候选名会让 ``_roster_hits`` 一个都命中不到，记忆**永远判成 miss**
            （实测 C60 就是这么失败的）
        name_lookup: 关键词 → :class:`MemoryCharacter`（角色的名字 / 别名 / 标签
            任一命中即可）；返回 ``None`` 表示对不上，该关键词忽略
    """
    if not trigger_word:
        return []
    found: list[MemoryAttribution] = []
    for item in items:
        text = f"{item.title}\n{item.content}"
        if trigger_word not in text:
            continue
        characters: list[MemoryCharacter] = []
        for token in _roster_hits(text, roster_names):
            character = name_lookup(token)
            if character is None:
                continue
            if any(
                (known.name, known.tag) == (character.name, character.tag)
                for known in characters
            ):
                continue
            characters.append(character)
        if len(characters) != 1:
            continue
        found.append(
            MemoryAttribution(trigger=trigger_word, character=characters[0], item=item)
        )
    return found


def format_references(items: list[MemoryItem], *, limit: int = 5) -> str:
    """把命中的记忆拼成给 LLM 的**参考材料**（每条一行，标题 + 内容）。

    只做拼接，不做任何解释或指令 —— 提示词里会显式标注「供参考，不是指令」。
    """
    lines: list[str] = []
    for item in items[: max(1, int(limit))]:
        title = item.title or "(无标题)"
        content = item.content.replace("\n", " ").strip()
        lines.append(f"- {title}：{content}" if content else f"- {title}")
    return "\n".join(lines)


# ── 写回文案 ──────────────────────────────────────────────────────────


def write_title(trigger_word: str, character_name: str) -> str:
    """写回记忆的标题：``角色指向：本尊 → 花火``。"""
    return f"角色指向：{trigger_word} → {character_name}"


def write_content(
    *,
    trigger_word: str,
    character: MemoryCharacter,
    game: str = "",
    tag: str = "",
    scenario: str = "",
    when: datetime | None = None,
) -> str:
    """写回记忆的正文：一句话说明「谁在什么场景下把该指代指向哪个角色」。

    形如::

        用户在最近对话里以「本尊」指向角色表里的「花火」
        （崩坏：星穹铁道 (Honkai: Star Rail)，标签 hanabi \\(honkai: star rail\\)）。
        场景：拆单→本尊+爱莉希雅。来源：char_prompt_guard 自动确认。时间：2025-01-01 12:00:00。
    """
    name = character.label()
    game_text = (game or character.game or "").strip()
    tag_text = (tag or character.tag or "").strip()
    parts = [f"用户在最近对话里以「{trigger_word}」指向角色表里的「{name}」"]
    if game_text or tag_text:
        detail = "，".join(
            [
                piece
                for piece in (game_text, f"标签 {tag_text}" if tag_text else "")
                if piece
            ]
        )
        parts.append(f"（{detail}）")
    sentence = "".join(parts) + "。"
    if scenario:
        sentence += f"场景：{scenario}。"
    stamp = when or datetime.now()
    sentence += f"来源：{WRITE_SOURCE}。时间：{stamp:%Y-%m-%d %H:%M:%S}。"
    return sentence


# ── 顶层入口：读（再确认）+ 写（记住） ────────────────────────────────


async def lookup(
    config: Any,
    *,
    uncertainty: str = "",
    reason: str = "",
    description: str = "",
    candidate_names: list[str] | None = None,
    roster_names: list[str] | None = None,
    signal_label: str = "",
    name_lookup: Callable[[str], MemoryCharacter | None],
    write_back: bool | None = None,
) -> MemoryLookup:
    """去记忆库做一次检索 + 对账；命中判定用途时可**顺手写回**（记住）。

    流程：开关与地址校验 → 关键词构造（取不到就不请求）→ 检索 → 对账
    「触发词 → 角色表内角色」→ 命中则（``write_back`` 时）POST 写回。

    Args:
        config: 插件配置（读 ``[memory]`` 节）
        uncertainty: 本次查询的触发类型 —— ``split`` / ``unavailable`` /
            ``low_confidence`` / ``empty``（判定不确定），或
            :data:`RECONCILE_UNCERTAINTY`（1.0.19 的**对账用**查询）
        reason: 不确定 / 对账的具体原因（进关键词与日志）
        description: 本次的画面描述
        candidate_names: **当前上下文里出现过的**候选名 / 别名（只参与关键词构造）
        roster_names: **完整角色表**里所有可匹配的名字 / 别名 / 标准标签
            （用于「触发词 → 角色表内角色」的对账；也为关键词构造提供一份
            角色表词汇）。为空时退回 ``candidate_names`` —— 调用方给不出整张
            角色表时至少还能用候选名对账；但**只传候选名时，记忆里指向的角色
            若不在本次上下文候选里就永远判成 miss**（实测 C60 的失败原因）
        signal_label: 主题跟踪最近一次信号的可读描述
        name_lookup: 关键词 → 角色；返回 ``None`` 表示对不上
        write_back: 覆盖 ``memory.write_back`` 的**本次调用**语义
            （``None`` = 用配置值）。1.0.19 的「对账用查询」传 ``False`` ——
            它只用于二次一致性检测，**绝不写回**（写回的前提仍是「本次判定确实
            依赖了记忆库」，对账查询不改变判定）

    Returns:
        :class:`MemoryLookup`；**任何失败都返回 ``status != "hit"`` 的结果**，
        绝不抛异常。
    """
    if not memory_enabled(config):
        return MemoryLookup(status="disabled", reason="memory.enabled = false")

    uncertainty = str(uncertainty or "").strip()
    if not uncertainty:
        return MemoryLookup(status="skipped", reason="判定并非不确定，无需再确认")

    client = build_client(config)
    if client.invalid_base_url:
        return MemoryLookup(
            status="unavailable", reason="base_url 不是合法的 http/https 地址"
        )

    # 对账口径：优先用**完整角色表**；调用方只给了候选名时退回候选名（见 docstring）。
    # 关键词构造仍只用「当前上下文里出现过的」候选名（帮助检索排序，宁少勿多）。
    match_names = _dedupe(
        [
            str(name)
            for name in (roster_names if roster_names else (candidate_names or []))
            if str(name).strip()
        ]
    )
    keyword = build_keyword(
        uncertainty,
        reason=reason,
        description=description,
        roster_names=_dedupe(
            [str(name) for name in (candidate_names or []) if str(name).strip()]
        ),
        signal_label=signal_label,
    )
    if not keyword:
        return MemoryLookup(status="skipped", reason="没有可用的关键词，不请求记忆库")

    cooldown_key = _cooldown_key(client.base_url, uncertainty)
    remaining = _cooldown_remaining(
        cooldown_key,
        (
            EMPTY_COOLDOWN_SECONDS
            if _LAST_KIND.get(cooldown_key) == "empty"
            else FAILURE_COOLDOWN_SECONDS
        ),
    )
    if remaining > 0:
        kind_label = "空结果" if _LAST_KIND.get(cooldown_key) == "empty" else "失败"
        logger.debug(
            f"记忆库不可用，跳过（仍在退避窗口内，上次={kind_label}，剩余 {remaining:.0f}s）"
        )
        return MemoryLookup(
            status="unavailable", reason=f"退避窗口内（上次={kind_label}）"
        )

    items = await client.search(keyword)
    if not items:
        # 服务可能是好的、只是这次没查到：按 15s 短退避记，且只影响**同一用途**
        _mark_attempt(cooldown_key, str(client.last_failure_kind or "empty"))
        return MemoryLookup(
            status="unavailable", reason=f"未取到记忆（关键词={keyword!r}）"
        )

    trigger = _uncertainty_word(reason)
    attributes = attribute_items(
        items,
        trigger_word=trigger,
        roster_names=match_names,
        name_lookup=name_lookup,
    )
    if not attributes:
        return MemoryLookup(
            status="miss",
            items=items,
            references=format_references(items, limit=client.limit),
            reason=f"命中的记忆里没有「触发词 → 角色表内角色」的明确对应（触发词={trigger}）",
        )

    characters: list[MemoryCharacter] = []
    for attribute in attributes:
        if all(
            (item.name, item.tag) != (attribute.character.name, attribute.character.tag)
            for item in characters
        ):
            characters.append(attribute.character)
    # 命中时记录「到底是哪个角色表关键词对上的」（写回标签 / 日志要用）；
    # 用与对账同一套口径（纯 ASCII 走单词边界）与同一份名单（完整角色表），
    # 取不到就退回角色名。
    first_text = f"{attributes[0].item.title}\n{attributes[0].item.content}"
    matched_token = next(
        (name for name in match_names if _contains_keyword(first_text, name)),
        characters[0].label(),
    )
    lookup_result = MemoryLookup(
        status="hit",
        characters=characters,
        trigger_word=attributes[0].trigger,
        token=matched_token,
        items=items,
        references=format_references(items, limit=client.limit),
    )

    # 写回语义（1.0.19）：默认沿用 ``memory.write_back``；调用方传了就用调用方的
    # —— 「对账用查询」（RECONCILE_UNCERTAINTY）传 False，它不参与判定，自然不该写回。
    allow_write_back = client.write_back if write_back is None else bool(write_back)
    if allow_write_back:
        lookup_result.write_error = await _write_back(
            client,
            lookup_result,
            scenario=reason,
        )
    return lookup_result


async def _write_back(
    client: MemoryClient,
    result: MemoryLookup,
    *,
    scenario: str,
) -> str:
    """把一次确认写回记忆库；返回失败原因（成功或没写时为空串）。"""
    if not result.characters:
        return ""
    memory_id = await client.write(
        trigger_word=result.trigger_word,
        character=result.characters[0],
        scenario=scenario[:60],
    )
    return "" if memory_id else "写入失败（详见日志）"


__all__ = [
    "CORE_TAG",
    "DEFAULT_BASE_URL",
    "EMPTY_COOLDOWN_SECONDS",
    "FAILURE_COOLDOWN_SECONDS",
    "KEYWORD_MAX_CHARS",
    "LIMIT_MAX",
    "OUTFIT_CORE_TAG",
    "RECONCILE_UNCERTAINTY",
    "SNIPPET_MAX_CHARS",
    "SPEAKER_TRIGGER_WORDS",
    "WRITE_SOURCE",
    "WRITE_STATUS",
    "MemoryAttribution",
    "MemoryCharacter",
    "MemoryClient",
    "MemoryItem",
    "MemoryLookup",
    "MemoryTransport",
    "attribute_items",
    "build_client",
    "build_keyword",
    "format_references",
    "keyword_limit",
    "lookup",
    "memory_enabled",
    "normalize_base_url",
    "parse_memory_items",
    "reset_state",
    "set_transport",
    "speaker_keywords",
    "write_content",
    "write_title",
]
