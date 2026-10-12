"""角色识别与提取：角色表解析、文本匹配、LLM 提取。"""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ROLE, LLMPayload, TaskType, Text
from src.kernel.llm import ModelSet

logger = get_logger("char_prompt_guard")


@dataclass
class CharacterEntry:
    """角色表条目。"""

    name: str
    game: str = ""
    aliases: list[str] = field(default_factory=list)
    tag: str = ""

    def keywords(self) -> list[str]:
        """返回用于匹配的关键词列表（角色名 + 别名，去重）。"""
        seen: set[str] = set()
        keywords: list[str] = []
        for keyword in [self.name, *self.aliases]:
            keyword = keyword.strip()
            if not keyword:
                continue
            key = keyword.lower()
            if key in seen:
                continue
            seen.add(key)
            keywords.append(keyword)
        return keywords

    def resolved_tag(self) -> str:
        """返回可写入 NAI 提示词的角色标签。

        只使用角色表显式声明的标签：中文作品名机翻成英文极易产出错误标签，
        因此宁缺毋滥，未声明时返回空字符串（调用方应跳过注入）。
        """
        return self.tag.strip()


def iter_config_entries(raw_text: str) -> list[str]:
    """将「列表型」配置文本切分为条目，兼容多种书写方式。

    角色表、目标 Action 表等配置项共用此逻辑，支持：

    - 多行：每行一个条目；
    - 单行分号分隔：``甲 | 乙 ; 丙 | 丁``；
    - 字面量 ``\\n``（例如从 JSON 复制粘贴进来）。

    注释规则：

    - **整行**以 ``#`` 开头：整行忽略（行内的分号不参与切分，
      因此注释里可以自由书写分隔符说明）；
    - **行尾**注释：``空白 + #`` 之后的内容会被丢弃，例如
      ``爱莉希雅 | 崩坏三 | elysia  # 主力角色``。只有在 ``#``
      前面有空白时才当注释，避免误伤取值里自带的 ``#``。
    """
    normalized = (
        (raw_text or "").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r", "")
    )
    entries: list[str] = []
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 行尾注释：只在「空白 + #」处切分，注释一直吃到行尾
        line = re.split(r"\s+#", line, maxsplit=1)[0]
        for segment in re.split(r"[;；]", line):
            if segment.strip():
                entries.append(segment)
    return entries


def parse_roster(raw_text: str) -> list[CharacterEntry]:
    """解析角色表文本。

    每个条目一个角色：``角色名 | 游戏名 | 别名1,别名2 | 角色标签``。
    游戏名、别名与角色标签均可省略；条目之间可用换行或分号分隔；
    整行以 ``#`` 开头则为注释，整行忽略。

    角色标签用于「标签模式」的生图插件（提示词本身就是 NAI tag），
    例如 ``elysia \\(honkai impact 3rd\\)``。
    """
    entries: list[CharacterEntry] = []
    for line in iter_config_entries(raw_text):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 兼容中文输入法打出的全角分隔符
        line = line.replace("｜", "|")
        parts = [part.strip() for part in line.split("|")]
        name = parts[0] if parts else ""
        if not name:
            continue
        game = parts[1] if len(parts) > 1 else ""
        aliases: list[str] = []
        if len(parts) > 2:
            aliases = [
                alias.strip()
                for alias in parts[2].replace("，", ",").split(",")
                if alias.strip()
            ]
        entries.append(
            CharacterEntry(
                name=name,
                game=game,
                aliases=aliases,
                tag=parts[3] if len(parts) > 3 else "",
            )
        )
    return entries


def normalize_prompt_text(text: str) -> str:
    """归一化提示词文本，便于比较角色标签是否已存在。

    去掉 NAI 转义反斜杠与下划线，统一小写并压缩空白，使
    ``elysia \\(honkai impact 3rd\\)`` 与 ``elysia (Honkai Impact 3rd)``
    被视为同一个标签。
    """
    lowered = (text or "").lower().replace("\\", "").replace("_", " ")
    return re.sub(r"\s+", " ", lowered).strip()


def content_contains_tag(content: str, tag: str) -> bool:
    """判断提示词中是否已包含某个角色标签（宽松比较）。"""
    normalized_tag = normalize_prompt_text(tag)
    if not normalized_tag:
        return False
    return normalized_tag in normalize_prompt_text(content)


def _keyword_hits(text: str, keyword: str) -> bool:
    """检查关键词是否命中文本。

    纯 ASCII 关键词使用单词边界匹配，避免 "rem" 误命中 "remember"；
    非 ASCII（中文/日文等）关键词直接做子串匹配。
    """
    if not keyword or not text:
        return False
    lowered = keyword.lower()
    if keyword.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None
    return lowered in text.lower()


def entry_key(entry: CharacterEntry) -> str:
    """返回用于集合去重的角色身份键。"""
    key = normalize_prompt_text(entry.tag) or normalize_prompt_text(entry.name)
    return key or entry.name.strip()


def character_in_texts(entry: CharacterEntry, texts: list[str]) -> bool:
    """判断角色是否已经出现在给定文本中（角色名/别名/标准标签任一中即算）。"""
    for text in texts:
        if not text:
            continue
        for keyword in entry.keywords():
            if _keyword_hits(text, keyword):
                return True
        tag = entry.resolved_tag()
        if tag and content_contains_tag(text, tag):
            return True
    return False


def find_characters_in_text(
    text: str, roster: list[CharacterEntry]
) -> list[CharacterEntry]:
    """在文本中查找所有命中的角色（按角色表顺序，去重）。"""
    if not text:
        return []
    hits: list[CharacterEntry] = []
    seen: set[str] = set()
    for entry in roster:
        for keyword in entry.keywords():
            if not _keyword_hits(text, keyword):
                continue
            key = entry_key(entry)
            if key not in seen:
                seen.add(key)
                hits.append(entry)
            break
    return hits


def find_characters_in_history(
    messages: list[dict[str, Any]],
    roster: list[CharacterEntry],
    limit: int = 0,
) -> list[CharacterEntry]:
    """扫描消息，返回被提及过的角色（按最近一次提及时间倒序，去重）。

    Args:
        messages: 消息字典列表（按时间升序）
        roster: 角色表
        limit: 最多返回多少个；0 表示不限制

    Returns:
        角色列表，最近提及的排在前面
    """
    last_index: dict[str, int] = {}
    by_key: dict[str, CharacterEntry] = {}
    for index, message in enumerate(messages):
        text = str(message.get("processed_plain_text") or message.get("content") or "")
        if not text:
            continue
        for entry in find_characters_in_text(text, roster):
            key = entry_key(entry)
            last_index[key] = index
            by_key[key] = entry

    ordered = sorted(last_index.items(), key=lambda item: item[1], reverse=True)
    result = [by_key[key] for key, _ in ordered]
    return result[:limit] if limit > 0 else result


def _format_roster_for_prompt(roster: list[CharacterEntry]) -> str:
    """将角色表格式化为 LLM 提示词中的参考文本。"""
    lines: list[str] = []
    for entry in roster:
        parts = [entry.name.strip()]
        if entry.game.strip():
            parts.append(entry.game.strip())
        parts.append(f"标签={entry.tag.strip() or '未提供'}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


_EXTRACT_SYSTEM_PROMPT = """你是角色识别助手，服务于生图提示词补全。

任务：
1. 阅读「画面描述」，先判断它**已经写了哪些角色**——这些不要再列出来。
2. 结合「对话上文」，找出**画面需要、但描述里还没写**的具体角色（游戏/动漫/二次元角色），
   按"与本次请求的相关度"从高到低排列。
3. 判断 multi：如果本次画面需要**多个角色同时出镜**（例如用户说"画她们俩""我和她一起""双人同框"，
   或上文明显在讨论两个以上角色共同出现的场景），multi 设为 true，并把需要的角色都列出；
   只画一个角色时 multi 设为 false。
4. 没有任何明确角色时输出 {"multi": false, "characters": []}。

输出格式（只输出一个 JSON 对象）：
{"multi": true 或 false, "characters": [{"name": "角色名", "game": "出处作品名", "tag": "角色标签"}]}

字段说明：
- name：角色的中文或常用名。
- game：该角色的出处作品名（游戏名优先），不确定时留空。
- tag：该角色在 Danbooru / NovelAI 中的标准角色标签，格式为 `角色名 (作品名)`，
  例如 `elysia \\(honkai impact 3rd\\)`、`hu tao \\(genshin impact\\)`。
  必须使用英文标准拼写、小写、空格分隔，括号需转义为 `\\(` 与 `\\)`；
  拼写没有把握时留空，不要编造。

硬性规则：
- 只输出 JSON，不要 markdown 代码块，不要任何解释。
- characters 最多 6 项，按相关度降序；每一项都必须是画面里真实需要的角色。
- 名字必须是具体有名字的角色；“我”“你”、bot 自身或泛指（如“一个女孩”）不算。
- 若角色在「角色表」中出现，优先使用角色表中的名字、游戏名与标签。
- 宁缺毋滥：不确定的角色不要列，不确定的字段留空。"""

_EXTRACT_USER_TEMPLATE = """对话上文（按时间顺序）：
{context}

本次画面描述：
{description}

角色表（参考，可忽略）：
{roster}

请按要求输出 JSON。"""


def _strip_json_code_fence(raw_text: str) -> str:
    """剥离模型可能返回的 JSON 代码块包装。"""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _is_deepseek_model_entry(model_entry: Any) -> bool:
    """判断模型条目是否指向 DeepSeek 提供商。"""
    if not isinstance(model_entry, dict):
        return False

    provider = str(model_entry.get("api_provider") or "").lower()
    base_url = str(model_entry.get("base_url") or "").lower()
    model_identifier = str(model_entry.get("model_identifier") or "").lower()
    return (
        "deepseek" in provider
        or "deepseek" in base_url
        or model_identifier.startswith("deepseek-")
    )


def _prepare_model_set(model_name: str) -> ModelSet:
    """为角色提取任务准备模型集，并对 DeepSeek 做请求级兼容。

    Args:
        model_name: 配置指定的模型名；空字符串时回退 UTILS_SMALL 任务模型

    Returns:
        ModelSet 实例
    """
    try:
        if model_name.strip():
            model_set = llm_api.get_model_set_by_name(model_name.strip())
        else:
            model_set = llm_api.get_model_set_by_task(TaskType.UTILS_SMALL.value)
    except Exception:  # noqa: BLE001 —— 模型集取不到就回退，不能把异常抛给生图链路
        logger.debug(
            f"模型集获取失败（{model_name or 'UTILS_SMALL'}），回退 UTILS_SMALL"
        )
        model_set = llm_api.get_model_set_by_task(TaskType.UTILS_SMALL.value)

    if not isinstance(model_set, list):
        return model_set

    prepared_model_set = deepcopy(model_set)
    for model_entry in prepared_model_set:
        if not _is_deepseek_model_entry(model_entry):
            continue

        extra_params = model_entry.get("extra_params")
        if not isinstance(extra_params, dict):
            extra_params = {}
        else:
            extra_params = dict(extra_params)

        # DeepSeek V4 思考能力切到 thinking 参数；提取任务只需直接文本，
        # 显式关闭 thinking 避免只返回 reasoning 通道导致拿不到正文。
        extra_params.pop("reasoning_effort", None)
        extra_params["enable_thinking"] = False
        extra_params["thinking"] = {"type": "disabled"}
        model_entry["extra_params"] = extra_params

    return prepared_model_set


async def _resolve_response_text(response: Any) -> str:
    """统一提取响应正文；正文为空时回退到 reasoning_content。"""
    raw_text = await response
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()

    reasoning_text = getattr(response, "reasoning_content", None)
    if isinstance(reasoning_text, str) and reasoning_text.strip():
        logger.debug("角色提取响应 content 为空，回退使用 reasoning_content")
        return reasoning_text.strip()

    return raw_text.strip() if isinstance(raw_text, str) else ""


@dataclass
class CharacterExtraction:
    """LLM 提取结果。

    Attributes:
        characters: 按相关度降序排列的角色列表
        multi: 本次画面是否需要多个角色同时出镜
    """

    characters: list[CharacterEntry] = field(default_factory=list)
    multi: bool = False


def _parse_extraction_payload(payload: Any) -> CharacterExtraction | None:
    """解析 LLM 返回的 JSON，兼容新旧两种结构。"""
    if not isinstance(payload, dict):
        return None

    raw_characters = payload.get("characters")
    multi_raw = payload.get("multi")

    entries: list[CharacterEntry] = []
    if isinstance(raw_characters, list):
        for item in raw_characters:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            tag = str(item.get("tag") or "").strip()
            if not name and not tag:
                continue
            entries.append(
                CharacterEntry(
                    name=name or tag,
                    game=str(item.get("game") or "").strip(),
                    tag=tag,
                )
            )
    elif raw_characters is None:
        # 兼容旧版单角色结构 {"name": ..., "game": ..., "tag": ...}
        name = str(payload.get("name") or "").strip()
        tag = str(payload.get("tag") or "").strip()
        if name or tag:
            entries.append(
                CharacterEntry(
                    name=name or tag,
                    game=str(payload.get("game") or "").strip(),
                    tag=tag,
                )
            )

    multi = bool(multi_raw) if isinstance(multi_raw, bool) else False
    if len(entries) > 1:
        # 列出了多个角色，说明画面需要多人，模型漏填 multi 时不让它漏判
        multi = True

    if not entries and not multi:
        return CharacterExtraction()
    return CharacterExtraction(characters=entries, multi=multi)


async def extract_characters_via_llm(
    description: str,
    context_text: str,
    roster: list[CharacterEntry],
    translate_model: str = "",
    timeout: float = 0.0,
) -> CharacterExtraction | None:
    """用 LLM 从对话上文中提取画面需要补充的角色（可多个）。

    Args:
        description: 本次生图的画面描述
        context_text: 最近对话上文（可读文本）
        roster: 角色表（作为规范化参考）
        translate_model: 提取用模型名；空字符串回退 UTILS_SMALL
        timeout: 本次提取的超时秒数；``<=0`` 表示不限时。生图 Action 会同步
            等待本函数，模型端挂起时必须有兜底，否则表现为 bot 卡死。

    Returns:
        提取结果；失败或超时返回 None
    """

    async def _run() -> str:
        request = llm_api.create_llm_request(
            model_set=_prepare_model_set(translate_model),
            request_name="char_prompt_guard_extract",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_EXTRACT_SYSTEM_PROMPT)))
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(
                    _EXTRACT_USER_TEMPLATE.format(
                        context=context_text.strip() or "<无>",
                        description=description.strip() or "<无>",
                        roster=_format_roster_for_prompt(roster) or "<空>",
                    )
                ),
            )
        )
        response = await request.send(stream=False)
        return await _resolve_response_text(response)

    try:
        if timeout and timeout > 0:
            raw_text = await asyncio.wait_for(_run(), timeout=timeout)
        else:
            raw_text = await _run()
        cleaned_text = _strip_json_code_fence(raw_text)
        return _parse_extraction_payload(json.loads(cleaned_text))
    except TimeoutError:
        logger.warning(f"LLM 角色提取超时（{timeout:g}s），本次不补角色")
        return None
    except Exception as e:  # noqa: BLE001 —— 提取失败只记日志并返回 None（放行生图）
        logger.warning(f"LLM 角色提取失败: {e}")
        return None
