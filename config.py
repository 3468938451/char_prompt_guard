"""char_prompt_guard 插件配置。"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class RegionEntrySection(SectionBase):
    """分区覆盖的一个条目：**某个角色在各分区里的特征词**（1.0.36，实验性功能）。

    这是「分区覆盖」**自己的条目列表**里的**一项** —— 它**不是** ``[roster]`` 的一行，
    也**不是** ``[outfits]`` 的一行：这里写的是"这个角色的头/身子/衣服/姿势/背景
    分别长什么样"，供把不同角色的不同分区拼到同一张画面里用。
    **默认不生效**：整份条目只在 ``[region_override] enabled = true`` 时才被读取。
    """

    name: str = Field(
        default="",
        description=(
            "角色名：**必须是角色表里的名字或别名**（对不上 → 该角色不参与分区，"
            "只打一条 DEBUG，其它注入照旧）"
        ),
    )
    head: str = Field(
        default="",
        description=(
            "head 分区（头 / 头发 / 眼睛）的特征词，英文、逗号分隔，"
            "如 silver hair, long hair, blue eyes, headband"
        ),
    )
    body: str = Field(
        default="",
        description=(
            "body 分区（身子 / 体型）的特征词，如 slender, flat chest, pale skin"
        ),
    )
    outfit: str = Field(
        default="",
        description="outfit 分区（衣服 / 穿着）的特征词，如 white dress",
    )
    pose: str = Field(
        default="",
        description="pose 分区（姿势 / 动作）的特征词，如 standing, hands on hips",
    )
    background: str = Field(
        default="",
        description=(
            "background 分区（背景 / 场景）的特征词，如 night city, neon lights"
        ),
    )


class CharPromptGuardConfig(BaseConfig):
    """角色提示词守卫插件配置类。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "char_prompt_guard 角色提示词守卫配置"

    @config_section("general")
    class GeneralSection(SectionBase):
        """通用行为配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用角色提示词检测与补全；关闭后所有生图请求直接放行",
        )
        apply_modes: str = Field(
            default="drawing",
            description=(
                "对哪些生图模式生效（逗号分隔，drawing=手绘画作，photo=手机拍照）。"
                "photo 模式 nai_artist 会自动吸收角色人设，默认不启用；"
                "如需对 photo 也生效填 drawing,photo"
            ),
        )
        history_hours: float = Field(
            default=2.0,
            description="回溯多少小时内的上文消息用于判断角色",
        )
        history_limit: int = Field(
            default=40,
            description="最多读取多少条上文消息",
        )
        history_cache_ttl: float = Field(
            default=5.0,
            description=(
                "上文消息的进程内缓存秒数：同一会话短时间内的多次生图不必重复查库。"
                "设 0 关闭缓存。触发本次生图的那条消息永远实时读取，不吃缓存"
            ),
        )
        use_llm: bool = Field(
            default=True,
            description=(
                "角色表未命中时是否使用 LLM 从上文中提取角色。"
                "开启后能识别角色表之外的角色，但每次补全会多一次小模型调用"
            ),
        )
        description_first: bool = Field(
            default=True,
            description=(
                "以画面描述为准：当画面描述里没有角色表内的角色时，即使上文能扫到角色表内角色，"
                "也交给 LLM 结合描述判定一次。可避免把几句之前提到的**另一个**角色补进去"
                "（实测：描述里要的是花火、上文最旧那条却在说“不加爱莉”，结果补错了人）。"
                "关闭后回到旧行为：角色表在上文命中就不再问 LLM（更省 token）"
            ),
        )
        auto_resolve_characters: bool = Field(
            default=True,
            description=(
                "角色不在角色表（或角色表条目缺标准标签）时，是否调用已配置的模型把它解析成"
                "「正式名 + 作品名 + NAI 标签」（与角色提取同一套 llm_api，模型沿用 translate_model，"
                "超时沿用 llm_timeout，不引入新依赖）。解析成功后："
                "① 用户点名表外角色、而画面里挂着**用户这次没点过**的角色表内标签时，"
                "把画面里那个错误标签**替换**成解析出的标签（consistency_check=fix 时；"
                "warn 时只在「画面里没有任何角色标签」那种情况下补入）；"
                "② 用户点名表外角色、而画面里**没有任何角色标签**（例如描述只写了外观）时，"
                "按目标的 natural / tags 模式把解析结果**补进**提示词最前面；"
                "③ 额外打一条 INFO 给出可直接粘贴的角色表行（**只打日志，绝不自动改配置文件**）。"
                "解析结果会缓存（进程内 24 小时）并可用于后续注入；"
                "memory.write_back 开启时还会把这次解析写回记忆库（沿用现有写回通道）。"
                "unknown=true / JSON 解析失败 / 标签不符合规范 / confidence 低于 0.5 → 视为解析失败，"
                "退回 1.0.20 的行为（warn 只报、fix 只删除）；任何异常都只记日志，绝不拦住生图。"
                "关闭后**完全退回 1.0.20 行为**（一次模型调用都不发）"
            ),
        )
        tag_completion: bool = Field(
            default=True,
            description=(
                "标签补全：画面描述里已出现角色表内的角色（名字/别名命中）、但没有该角色的"
                "标准标签时，把角色表第 4 列的标签补进提示词。"
                "实测下游翻译模型会把「花火」翻成 hanabi (firefly) (honkai: star rail)"
                "（括号未转义还串了别的角色），补上转义正确的标准标签可让 NAI 稳定锁定角色。"
                "只在角色表提供了第 4 列标签时生效"
            ),
        )
        translate_model: str = Field(
            default="",
            description=(
                "用于角色提取的模型名称（对应 config/model.toml 中 models 列表里的 name）。"
                "留空时回退到 UTILS_SMALL 任务模型"
            ),
        )
        llm_timeout: float = Field(
            default=20.0,
            description=(
                "LLM 角色提取的超时秒数。生图 Action 会同步等待提取结果，"
                "模型端挂起时必须有个兜底，否则用户侧表现为 bot 卡死；"
                "设 0 表示不限时（不推荐）"
            ),
        )
        append_format: str = Field(
            default="（画面角色：“{name}”，出自游戏《{game}》）",
            description="补入提示词末尾的模板（{name}=角色名，{game}=游戏名）",
        )
        append_format_no_game: str = Field(
            default="（画面角色：“{name}”）",
            description="游戏名为空时使用的模板",
        )
        consistency_check: str = Field(
            default="warn",
            description=(
                "二次一致性检测：注入/放行之前，把「最终提示词里出现的角色表内角色」与"
                "「期望集合」对账。期望集合 = 判定集合 + 主题跟踪给出的主题（主题可用时）"
                "+ 记忆库确认结果（命中「触发词 → 角色表内角色」的明确对应时）。"
                "off=不检查（也不发那次对账用的记忆库查询）、"
                "warn（默认）=发现提示词里有期望集合之外的角色表内角色时打一条 WARNING"
                "（同时写出画面那一侧与上下文/记忆那一侧的角色名与依据），内容不改；"
                "fix=仅当「期望集合恰好 1 人」且「画面里冲突的角色恰好 1 个」时，"
                "把画面里那个错误角色的名字/别名/标签替换成期望角色的（替换范围仅限角色表里"
                "已知的字符串），替换后按需补期望角色的标准标签；出现歧义或多角色无法定位时"
                "自动退回 warn 行为并打 WARNING。"
                "1.0.20 起同一个开关还管一条**独立**的检查（不新增配置项）：扫描最近若干条"
                "**用户消息**（触发消息 + 触发前最近 4 条，bot 消息不算）提取「用户点名的"
                "**角色表外**角色名」，若最终提示词里出现**用户这次没点过**的角色表内角色"
                "即为冲突 —— off 完全不检查（连用户消息都不扫）、warn 只打 WARNING 且内容"
                "一字不改、fix **删除**画面里那个错误角色的名字/别名/标准标签（用户点名的"
                "角色没有标准标签可替换，只能删；外观描述原文保留）。它与期望集合对账"
                "互相独立、可各自触发（实测事故里期望集合为空、只有这条能发现冲突）。"
                "1.0.21 起这条 fix 会先尝试用 general.auto_resolve_characters 解析出表外角色的"
                "标准标签：解析成功则把画面里的错误标签**替换**成解析出的标签（而不是只删除），"
                "并按 natural 补中文说明、按 tag_completion 补标准标签，日志为「一致性检查="
                "冲突已修正（fix）：把画面里的「花火」替换为「刻律德拉」（替换 N 处），"
                "依据=角色解析(LLM):刻律德拉 → Kleidra，作品《崩坏：星穹铁道 (Honkai: Star Rail)》」；"
                "解析失败（unknown / 超时 / 标签非法）时行为与 1.0.20 一致"
            ),
        )
        skip_on_self_reference: bool = Field(
            default=True,
            description=(
                "自我指代时不补（保守策略）：画面描述、触发消息或触发之前最近几条用户消息里"
                "出现「本尊 / 你本人 / 你自己 / 真正的你 / 真身 / 脱壳 / 摘壳 / 卸壳 / 本体」，"
                "且画面描述里没有角色表内角色时，判定为不补。"
                "1.0.13 起改为：先按 general.true_self → 框架人格配置 personality.nickname → "
                "alias_names 取「本尊身份」，命中角色表就正常补入那个角色，没命中或取不到才不补。"
                "「本尊是谁」只存在于聊天器（调用方）的内部设定里，插件看不到，"
                "按主角粘性沿用旧点名会补错（实测：用户说「要你自己」，插件却把更早点名的"
                "爱莉希雅补了进去，而描述本身已经写对了外观）。"
                "关闭后回到 1.0.11 行为（普通「想看看你～」这类无自我指代的请求不受影响，"
                "一直保持粘性）"
            ),
        )
        true_self: str = Field(
            default="",
            description=(
                "「本尊/你自己/你本人」这类自我指代指向的角色（填角色表中的名字或别名）。"
                "留空时自动读取框架人格配置的 personality.nickname；填了则以本配置为准。"
                "两者都取不到、或取到的名字不在角色表里时不补（只记日志提示，绝不猜别的角色）"
            ),
        )
        notify_tag_rule: bool = Field(
            default=True,
            description=(
                "是否提醒主模型在生图参数最前面写角色标准标签：system reminder 里追加一条硬规则 —— "
                "调用生图动作（share_photo / share_drawing / draw_image）时，提示词参数"
                "（content / content_description）最前面先写角色表第 4 列的标准标签"
                "（如 Hanabi \\(honkai: star rail\\)），然后再写画面描述；"
                "角色表未提供标签时写「角色名（游戏名）」。"
                "关闭后 reminder 只保留标记说明（#multi# / #nochar# 等）"
            ),
        )
        tag_first_hint: bool = Field(
            default=True,
            description=(
                "是否在生图动作的参数说明里追加「最前面必须先写角色标准标签」这一句"
                "（1.0.17 起，纯内存改动、不写文件）：调用生图动作时，提示词参数"
                "（content / content_description）的说明末尾会多一条附加要求 —— "
                "最前面先写角色表第 4 列的标准标签（如 Hanabi \\(honkai: star rail\\)），"
                "然后再写画面描述；角色表未提供标签时写「角色名（游戏名）」。"
                "1.0.19 起同一段追加文本还会再补一句：标签必须与**本次画面描述的角色一致**，"
                "不要照搬备忘 / 历史记录 / 上一张图的标签（实测事故：主模型照搬了备忘里"
                "过期的固定标签 Hanabi，而这次画的是爱莉希雅）。"
                "与 notify_tag_rule 的区别：那条是 system reminder（旁白），"
                "这一条直接写进工具定义里的参数说明，主模型生成参数时第一眼就能看到，"
                "是让标准标签稳定出现在提示词最前面的主要通道。"
                "只作用于内置目标动作（share_photo / share_drawing / share_visual / "
                "draw_image）的提示词参数；找不到动作、结构不认识时静默跳过，"
                "绝不影响生图。关闭后完全不改动第三方动作的参数说明"
                "（reminder 不受影响，仍由 notify_tag_rule 控制）；"
                "general.enabled = false 或 targets.enabled = false 时同样不注入"
            ),
        )
        runtime_framework_compat: bool = Field(
            default=True,
            description=(
                "框架运行时兼容（1.0.34 起）：插件加载后探测 agent 聊天器的工具调用路径"
                "（src/core/utils/llm_tool_call.py）**是否已经在发「执行前事件」**——"
                "没发就在**内存里**给 run_tool_call 补一层包装（等价 "
                "framework-patch/llm_tool_call-pre-events.patch 二版：补发 "
                "before_action_call / before_tool_call，并把订阅者写回的 args 交给原函数；"
                "**不写任何文件**、不改框架源码、不 import 补丁的内部符号），"
                "让没打补丁的用户也能开箱生效；"
                "**已打静态补丁（或上游将来原生支持）就一个字都不动**，一行代码都不执行；"
                "认不出框架结构（模块 / 函数不在、源码读不到）时同样**不装**"
                "（宁可不生效，也不重复发事件），行为与 1.0.33 逐字一致。"
                "任何异常都只记日志，**绝不拦住生图**。"
                "关闭后不探测、不包装，需要自己应用 "
                "framework-patch/llm_tool_call-pre-events.patch"
            ),
        )

    @config_section("roster")
    class RosterSection(SectionBase):
        """角色表快速匹配配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用角色表快速匹配（零成本、确定性，优先于 LLM）",
        )
        characters: str = Field(
            default="",
            description=(
                "角色表：每段「角色名 | 游戏名 | 别名1,别名2 | 角色标签」，后三项可省略。"
                "第 4 列填 NAI 标准角色标签（如 elysia \\(honkai impact 3rd\\)），"
                "供 image_generator_plugin-neo 这类「提示词即标签」的生图插件直接使用；"
                "nai_artist 不需要该列。建议游戏名带英文作品名、别名带英文角色名。"
                "多行或分号(;)分隔均可；整行以 # 开头为注释。留空则只用 LLM 从上文识别角色。"
                "示例（只有一行，按同样格式逐行添加你自己的角色，照抄改掉即可）："
                "爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia,爱莉 | elysia \\(honkai impact 3rd\\)"
            ),
        )

    @config_section("targets")
    class TargetsSection(SectionBase):
        """被接管的生图 Action 配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用目标 Action 接管；关闭后所有生图请求直接放行",
        )
        list: str = Field(
            default="",
            description=(
                "需要接管的生图 Action，每段："
                "「插件名:action:动作名 | 提示词参数名 | 追加模式 | 模式参数 | 额外检测参数 | "
                "角色槽位参数 | 负向参数名」，后几项可省略。追加模式 natural=追加中文说明"
                "（生图插件会再翻译成标签，适配 nai_artist）；"
                "tags=直接追加 NAI 角色标签（提示词本身就是标签，适配 image_generator_plugin-neo）。"
                "**1.0.36 起最后一段是负向参数名**（可空）：只给「分区覆盖」的 "
                "negative_swap 用 —— 动作用哪个参数承载负向提示词（neo 是 negative_prompt），"
                "留空表示该动作没有负向参数（负向词只进 DEBUG，不改画面）。"
                "留空使用内置默认（内置已为 neo 的 draw_image 填好 negative_prompt，"
                "nai_artist 的三个动作留空）："
                "nai_artist:action:share_photo | content | natural ; "
                "nai_artist:action:share_drawing | content | natural ; "
                "nai_artist:action:share_visual | content | natural | mode（nai_artist 1.2.x 及更早的旧动作名）; "
                "image_generator_plugin-neo:action:draw_image | content_description | tags | "
                "| characters | characters | negative_prompt"
            ),
        )

    @config_section("multi")
    class MultiSection(SectionBase):
        """多角色同框补全配置。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用多角色同框补全。关闭后回到单角色行为："
                "提示词命中任一角色即整体放行，未命中时只补最近讨论的那一个"
            ),
        )
        scope: str = Field(
            default="multi_only",
            description=(
                "multi_only=仅当上文中出现 2 个及以上角色时才启用集合式检测（单人场景行为完全不变，推荐）；"
                "always=所有场景都逐角色比对（提示词写了 A、上文聊 B 时也会补 B）"
            ),
        )
        max_characters: int = Field(
            default=6,
            description=(
                "单次最多补入几个角色（1~6）。image_generator_plugin-neo 在 V4.5 最多 6 人、V5 最多 32 人；"
                "nai_artist 无多角色机制，实际会按其上限截断到 2"
            ),
        )
        natural_max: int = Field(
            default=2,
            description=(
                "natural 模式（nai_artist 这类不使用 NAI 多角色槽位的生图插件）"
                "单次最多补几个角色，超出会截断并打 WARN。"
                "它只能把多人写进同一段提示词，人越多越容易发色/服装互串"
            ),
        )
        decision: str = Field(
            default="bot",
            description=(
                "多人判定方式（旧字段，保留兼容）：bot=由主模型标记与 LLM 判定自动切换（推荐）；"
                "always=总是按多人处理；off=总是按单人处理。"
                "新部署请优先用 decide"
            ),
        )
        decide: str = Field(
            default="bot",
            description=(
                "多人判定方式（新）：bot=按「用户点名」规则自动判定（最近一条点名含 ≥2 个角色、"
                "或含“还有/一起/俩/都/加上/和她/and/both”等追加词时补多人；含“算了/还是/不是/换/改成”"
                "等改口词时只补最新那一个；两者都不含且与上一条点名消息的间隔小于 "
                "multi_window_minutes 时视为不确定，交 LLM 仲裁，LLM 不可用时宁少勿多只补最近那一个）；"
                "latest=总是只补最近点名的那个角色；always_multi=总是按多人处理"
            ),
        )
        llm_mode: str = Field(
            default="fallback",
            description=(
                "何时调用 LLM 判定：fallback=角色表完全没命中时才调用（省 token）；"
                "always=每次都调用，可识别角色表外的角色并判断是否需要多人"
            ),
        )
        recent_only: bool = Field(
            default=True,
            description="候选角色只从最近 N 条消息里提取，降低“擅自加人”的风险",
        )
        recent_messages: int = Field(
            default=6,
            description="recent_only 的消息窗口大小",
        )
        stickiness_hours: float = Field(
            default=6.0,
            description=(
                "主角粘性时效（小时，默认 6.0）：画面描述里没有角色表内角色时，"
                "采用「最近一次被提及的角色表内角色」（按时间从新到旧找第一个命中）。"
                "这条提及若比该时效还旧，就视为没有信号（不补、交给 LLM 兜底），"
                "避免把几小时前聊过的角色补进现在的画面。设 0 表示不限时效"
            ),
        )
        multi_window_minutes: float = Field(
            default=5.0,
            description=(
                "短时间连点两人的判定窗口（分钟，默认 5.0）：最近一条用户消息只点名了 1 个角色、"
                "且既没有追加词也没有改口词时，若它与「上一条点名消息」的间隔小于该值，"
                "就认为用户可能在换人（也可能在追加），判定为不确定并交 LLM 仲裁；"
                "LLM 不可用时宁少勿多，只补最近那一个。间隔大于等于该值则只取本条那一个"
            ),
        )
        strategy_neo: str = Field(
            default="characters",
            description=(
                "image_generator_plugin-neo 的多人注入方式："
                "characters=2 人及以上写入 characters 参数（独立角色槽位，推荐），单人仍追加标签；"
                "append_tags=一律把标签追加进提示词（构图自由但多人易互串）"
            ),
        )
        coord_plan: str = Field(
            default="spread",
            description=(
                "写入 characters 参数时的坐标策略：spread=按顺序均匀分散（2 人 0.35/0.65，3 人 0.25/0.5/0.75）；"
                "none=不写坐标（由生图插件用默认值）。"
                "注意：坐标是否生效取决于 image_generator_plugin-neo 自身的 always_use_coords 开关"
            ),
        )
        bot_control: bool = Field(
            default=True,
            description=(
                "是否允许主模型在提示词里用标记临时切换："
                "#solo# 只补一个、#multi# 强制多人、#chars# 用独立角色槽位、"
                "#tags# 标签直接写进提示词、#nochar# 本次不补。标记会被自动移除，不会进入画面"
            ),
        )
        notify_bot: bool = Field(
            default=True,
            description="是否注入 system reminder 告知主模型这些标记的用法",
        )

    @config_section("monitor")
    class MonitorSection(SectionBase):
        """主题跟踪（``subject_monitor``，1.0.14 起）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用主题跟踪：按时间正序扫最近 window 条消息，维护一个「当前主题」"
                "（这次该画谁），并给出置信度。规则依次是：改口（先清空再按本条的名字设定）→ "
                "本条含 ≥2 个角色表内角色或追加词（多人同框）→ 本条含 1 个角色表内角色 → "
                "自我指代（主题=本尊身份，取不到或不在角色表则不可用）→ 人称代词（主题不变，"
                "回指上一个主题；没有先行词则不可用）→ 普通闲聊（主题不变）。"
                "bot 自己的消息只跳过，既不算点名也不清空主题。"
                "关闭后**完全退回 1.0.13 行为**（自我指代扫描 + 用户点名/主角粘性），"
                "可用 tests/check_logic.py 的 C53 回归验证"
            ),
        )
        window: int = Field(
            default=15,
            description=(
                "主题跟踪的窗口大小（消息条数，只扫最近这么多条，上限 40）："
                "窗口建议 10~20 条 —— 太小会漏掉稍早的主题信号（例如「先说画谁、隔几句才让出图」），"
                "太大则容易把别的话题里的角色翻出来。默认 15 条"
            ),
        )
        min_confidence: float = Field(
            default=0.5,
            description=(
                "置信度阈值（0~1，越低越激进）：主题的置信度 = 信号强度 × 时间衰减，"
                "低于阈值就不补（视同「没有信号」，交给 LLM 兜底或什么都不补）—— "
                "避免把很久以前聊过的角色补进现在的画面。默认 0.5"
            ),
        )
        half_life_minutes: float = Field(
            default=45.0,
            description=(
                "时间衰减的半衰期（分钟）：衰减系数 = exp(-Δt / half_life)，"
                "Δt 是「设定主题的那条消息」距本次触发消息的分钟数。"
                "默认 45 分钟 —— 约 31 分钟后信号强度 1.0 的置信度降到 0.5 阈值附近，"
                "即「半小时内说过的角色」才稳定生效；设 0 表示不随时间衰减"
            ),
        )

    @config_section("memory")
    class MemorySection(SectionBase):
        """外部记忆库（Booku Memory）关联配置（1.0.18 起）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用记忆库关联：判定**不确定**时（主题跟踪的 split 拆单 / "
                "unavailable 本尊身份取不到或代词无先行词 / low_confidence 置信度不足 / "
                "empty 无信号但仍可能要问 LLM）去外部记忆库检索相关信息做一次「再确认」，"
                "命中「触发词 → 角色表内角色」的明确对应就直接用作判定依据，"
                "并把这次确认写回记忆库（见 write_back）。"
                "另外，画面描述里已经点名角色表内角色、且 consistency_check != off 时，"
                "还会做一次**只用于二次一致性检测对账**的查询（1.0.19 起）："
                "它不参与判定、不替换描述点名的角色、也不写回。"
                "关闭后**完全不发任何请求**（读取与写入都不发），判定行为与 1.0.17 完全一致"
            ),
        )
        base_url: str = Field(
            default="http://localhost:8000/booku-memory",
            description=(
                "记忆库地址（Booku Memory）。默认 http://localhost:8000/booku-memory；"
                "若你的记忆库在别的机器或别的端口，把整段地址换成你自己的。"
                "只接受 http/https，地址结尾多写一个斜杠也能容忍（等价于不写）。"
                "地址不是合法 http/https 时插件**完全不访问记忆库**，只打一条 WARNING"
            ),
        )
        timeout: float = Field(
            default=2.0,
            description=(
                "单次请求（检索 / 写入）的超时秒数。超时就当「记忆库不可用」并跳过，"
                "静默降级回 1.0.17 的判定链，绝不拦住生图"
            ),
        )
        limit: int = Field(
            default=5,
            description=(
                "每次检索最多取回几条记忆（1~20，超出按 20 处理）。"
                "条数越多，喂给 LLM 的参考材料越长"
            ),
        )
        write_back: bool = Field(
            default=True,
            description=(
                "是否把这次「确认结果」写回记忆库（记住）——"
                "仅当本次判定**确实依赖了记忆库**（检索命中并影响了结论）时才写，"
                "不是每次生图都写。1.0.19 起的「对账用查询」（画面描述已点名角色时那次）"
                "**永远不写回** —— 它不参与判定，只是拿去和画面里的标签对账。"
                "写入内容：标题「角色指向：<触发词> → <角色名>」、"
                "正文一句话说明场景与标准标签、core_tags=[角色指向, 触发词, 角色名]。"
                "每次写入都会打一条 INFO 日志（含返回的 id 或失败原因）；"
                "写入失败只记日志，不影响生图"
            ),
        )
        bucket: str = Field(
            default="memory",
            description="写入记忆时使用的 bucket（检索侧不受影响）",
        )
        memory_type: str = Field(
            default="knowledge",
            description="写入记忆时使用的记忆类型 memory_type",
        )

    @config_section("outfits")
    class OutfitsSection(SectionBase):
        """皮肤/时装 与 泛指的穿着（1.0.22 起；1.0.23 加推断、1.0.28 加穿着类型）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用「皮肤/时装」与「泛指的穿着」（1.0.28）识别与注入："
                "用户说「我想看你的 XXXX」这类点了一套皮肤/时装、或说了"
                "「学生装 / 居家服 / 雨衣」这类**穿着**时，把它解析成英文关键词"
                "**追加**进提示词（角色照旧由原来的判定链决定，这一维只做"
                "「多追加一层」，失败时一个字都不改）。关闭后整条链路零开销："
                "不读上文、不调模型、不碰提示词（只保留 1.0.24 起那条纯字符串识别，"
                "用于把皮肤名从「表外角色名」里豁免掉）"
            ),
        )
        entries: str = Field(
            default="",
            description=(
                "皮肤表：每段「皮肤名 | 角色名(可空) | 英文关键词或 NAI 标签 | "
                "中文描述(可空)」，后三项可省略。多行或分号(;)分隔均可；"
                "整行以 # 开头为注释。命中表里的皮肤名时**零模型调用**，"
                "直接用表里的关键词与中文描述；留空则每次都由模型解析（见 auto_resolve）。"
                "示例（只有一行，按同样格式逐行添加你自己的皮肤，照抄改掉即可）："
                "粉色妖精小姐 | 爱莉希雅 | pink elf dress, fairy wings | 爱莉希雅的粉色妖精装"
            ),
        )
        wear_types: str = Field(
            default="",
            description=(
                "穿着类型表（1.0.28 起）：每段「中文词 | 英文关键词, 英文关键词」，"
                "多行或分号(;)分隔均可；整行以 # 开头为注释。留空则只用内置表。"
                "这里填的是**泛指的穿着类型**（学生 / 居家 / 泳衣 / 睡衣 …），"
                "**不是具体皮肤名** —— 用户说「我想看你的学生装」时按这里的关键词"
                "**确定性注入**，不必让模型猜，日志为 "
                "穿着类型解析(配置表): 学生 → school uniform, pleated skirt。"
                "填了与内置表**合并**、同名的覆盖内置；候选**本身是该词或以它结尾**"
                "即命中（学生装 / 居家服 / 泳衣 / 穿学生装；1.0.29 起收紧，"
                "学生会 / 学生时代 这种「只是包含」的不算），多个词命中取最长的那个。"
                "1.0.30 起内置表补上了 15 条常见同义写法（学生服 / 学生制服 / 校服外套 / "
                "校服裙 / 婚纱照 / 婚纱裙 / 和服裙 / 泳衣套装 / 居家服装 / 女仆装束 / "
                "护士服装 / 西装外套 / 运动服装 / 旗袍裙 / 睡衣套装），它们现在都是"
                "**独立的键**、零模型调用命中。**1.0.31 校正口径**：它们在 1.0.29 并不是"
                "「本来就命中、只是命中的是较短的键」—— 这 15 条都是「键 + 别的后缀」的"
                "写法，按「相等或后缀」口径逐个核对下来是「14 条一个键都不命中（都要多问"
                "一次模型）+ 1 条（学生制服）被较短的键 制服 抢走关键词（军装风）」；"
                "同一版还从**句式抽取**那一侧去掉了虚词「会」，"
                "「我想看学生会～」不再被截断成恰好等于键的「学生」而误注入校服。"
                "**1.0.32 收窄归属句式 `…的<穿着词>` 的尾边界**：后面不是名字字符"
                "（标点 / 空白 / 串尾 / emoji）即放行，另外**额外只放行 10 个句末语气词**"
                "（吧 / 呢 / 啊 …），**实词汉字不放行**，「爱莉的婚纱是我挑的」"
                "「流萤的校服被雨淋了」这类不含穿着意图的说法不再命中"
                "（「画爱莉的婚纱吧～」照旧命中）。"
                "优先级：皮肤表命中 > 穿着类型表 > 模型解析（皮肤 / 穿着）。"
                "示例（只有一行，按同样格式逐行添加你自己的穿着，照抄改掉即可）："
                "学生 | school uniform, pleated skirt"
            ),
        )
        auto_resolve: bool = Field(
            default=True,
            description=(
                "皮肤表里没有的皮肤名，是否调用已配置的模型解析（与角色解析同一套 "
                "llm_api：模型沿用 translate_model、超时沿用 llm_timeout，"
                "不引入新依赖，结果带进程内缓存）。解析结果必须是小写 ASCII 的英文"
                "关键词：unknown=true / JSON 解析失败 / confidence 低于 0.5 / "
                "既没有 tag 又没有 keywords 一律视为失败，**什么都不注入**"
                "（绝不瞎编），日志 皮肤解析(LLM)失败：<原因>。解析成功会额外打一条"
                "「建议加入皮肤表：…」（只打日志，绝不自动改配置文件）。"
                "关闭后只有皮肤表里写得出的皮肤会被注入。"
                "1.0.28 起同一个开关也管「穿着模型解析」（穿着类型表与皮肤表都"
                "没有、但看起来是穿着的候选，用穿着专用提示词解析，"
                "失败日志 穿着解析(LLM)失败：<原因>)；关掉后这两条模型路一起关"
            ),
        )
        write_back: bool = Field(
            default=True,
            description=(
                "解析成功后是否把这次结果写回记忆库（沿用 memory.py 现有写回通道，"
                "core_tags / diffusion_tags / opposing_tags 三元组同时非空），"
                "标题形如「皮肤指向：粉色妖精小姐 → 爱莉希雅」。"
                "需要 [memory] enabled 为真、memory.write_back 为真、"
                "且 base_url 合法；写入失败只记日志，绝不影响生图。"
                "1.0.23 起只对「用户点名了具体皮肤」的那条路生效："
                "模型推断出来的那一套（见 infer_unnamed）不写回。"
                "1.0.28 起穿着那两条路（穿着类型表 / 穿着模型解析）也一律不写回 —— "
                "穿着关键词是通用服装词，记下来没有复用价值"
            ),
        )
        infer_unnamed: bool = Field(
            default=False,
            description=(
                "当用户只说「<角色>的那一套皮肤/时装」而**没有点名**具体皮肤时，"
                "是否允许模型自行推断该角色已知的皮肤/时装名称与关键词（默认关）。"
                "开启后：先从用户消息里认出「<角色名>的那一套(皮肤|时装|衣服|裙子|装扮|造型)」"
                "这类说法里的角色（角色名必须是角色表内角色，取最近一条消息里提到的），"
                "再让已配置的模型（沿用 translate_model / llm_timeout）给出该角色"
                "最知名/最常被画的一套，注入方式与 1.0.22 完全相同"
                "（角色在前、皮肤在后）。**有编造风险**：推断结果同样要过 1.0.22 的"
                "关键词校验（只收小写 ASCII；unknown=true / JSON 坏 / confidence 低于 0.5 / "
                "没有可用关键词 → 什么都不注入），日志会标注来源"
                "（皮肤推断(模型): 流萤 → <皮肤名> → <关键词>（来源=模型推断，confidence=…），"
                "并照旧打一条「建议加入皮肤表：…」供核对）；推断结果不会写回记忆库。"
                "关闭（默认）时**完全不做这件事**：0 次模型调用、无相关日志，"
                "行为与 1.0.22 逐字一致"
            ),
        )

    @config_section("region_override")
    class RegionOverrideSection(SectionBase):
        """实验性功能（默认关闭）：分区覆盖。

        指定「头 / 头发 / 眼睛 用 A 的、身子 / 体型 用 B 的」这类分区拼接，也允许
        单分区（只换头）。⚠️ 实验性功能：打开后**生图效果可能不理想** —— 分区拼接
        只是"按词重画"，模型可能把两个角色的特征混在一起（串味）、比例异常或细节
        崩坏；默认关闭，普通使用完全不受影响。识别不到说法时**零动作**
        （不额外读上文、不调模型、不注入）。

        本节的 ``entries`` 是**独立条目列表**，**不写进** ``[roster]`` / ``[outfits]``。
        """

        enabled: bool = Field(
            default=False,
            description=(
                "实验性功能（默认关闭）：分区覆盖。打开后生图效果可能不理想 —— "
                "分区拼接只是「按词重画」，模型可能把两个角色的特征混在一起（串味）、"
                "比例异常或细节崩坏；默认关闭，普通使用完全不受影响。"
                "打开后支持「头是B的，身子是A的」「头换成B」"
                "「用A的身子配B的头」这类说法：识别「分区词 + 角色名 + 归属句式」"
                "三重条件（缺一不认），把各分区条目里的特征词按 regions 的顺序拼进提示词"
                "（head 写在最前面）。只从**用户最近几条消息**里做纯字符串解析，不调模型；"
                "关闭 / 识别不到 / 条目里没有该角色 → 零动作"
                "（不额外读上文、不调模型、不注入，其它注入照旧）"
            ),
        )
        regions: list[str] = Field(
            default_factory=lambda: [
                "head",
                "body",
                "outfit",
                "pose",
                "background",
            ],
            description=(
                "允许参与拼接的分区**列表** —— **顺序就是注入顺序**（head 在最前面）。"
                "可用值：head（头 / 头发 / 眼睛）、body（身子 / 体型）、"
                "outfit（衣服 / 穿着）、pose（姿势 / 动作）、background（背景 / 场景）。"
                "没列出来的分区不会被注入；整份都写错/留空时按默认五个分区处理"
            ),
        )
        stylized_swap: bool = Field(
            default=False,
            description=(
                "额外加入「接缝 / 过渡」类描述词"
                "（stitched neck, visible seam, head slightly tilted），"
                "让拼接处更明显一些；默认 false（这些词一个都不出现）"
            ),
        )
        negative_swap: bool = Field(
            default=True,
            description=(
                "把「被换掉的那个角色在**同一个分区**里的原特征」写进负向提示词，"
                "抑制两个角色串味（例：头用了B、身子用了A → 负向里放"
                "A的头/头发/眼睛特征与B的身子/体型特征；"
                "与正向重复的词一律不放）。**只有当目标动作本身带负向参数时才真的写进去**："
                "image_generator_plugin-neo 的 draw_image 有 negative_prompt；"
                "nai_artist 的 share_photo / share_drawing 没有负向参数（它的负向词在"
                "nai_artist 自己的 character.negative_tags 里，本插件不碰别的插件配置），"
                "那种情况只打一条 DEBUG，画面一个字都不改。"
                "**如果你的 nai_artist 版本确实给某个动作加了负向参数**，把它填进 [targets] "
                "list 的**第 7 段**即可（例："
                "nai_artist:action:share_photo | content | natural | | | | negative_prompt），"
                "不用改代码、也不用等插件更新"
            ),
        )
        anti_mix: bool = Field(
            default=True,
            description=(
                "多角色分区拼接时追加防串味负向词（mixed attributes, blended eyes）；"
                "只有一句话里出现 ≥2 个不同角色时才加"
            ),
        )
        entries: list[RegionEntrySection] = Field(
            default_factory=list,
            description=(
                "★ 分区覆盖**自己的条目列表**（独立于 [roster] 与 [outfits]）："
                "每个条目一个角色 —— name 填角色表里的名字或别名，"
                "head / body / outfit / pose / background 分别填该角色各分区的特征词"
                "（英文、逗号分隔）。用法见上面的 enabled 说明；"
                "示例：name = B / head = silver hair, long hair, blue eyes, headband / "
                "body = slender, flat chest, pale skin / outfit = white dress。"
                "找不到条目 → 退回「不分区」并打一条 DEBUG（宁可少拼，也不瞎拼）"
            ),
        )

    @config_section("appearance")
    class AppearanceSection(SectionBase):
        """实验性功能（默认关闭）：外观覆盖（换眼睛 / 换发色）。

        解决的是这条实测问题：角色标签 ``firefly (honkai: star rail)`` 自带「蓝眼」
        先验，权重高过描述里那句裸词 ``purple eyes``，所以用户说「眼睛换成本尊的
        紫红」时出图仍是蓝眼。本功能把那个颜色词**加重**放到提示词最前面，并把画面里
        已有的角色标签**降权**。

        **为什么不做负向**：``nai_artist`` 的 Action（``share_photo`` /
        ``share_drawing`` / ``share_visual``）只有 ``content``（photo 另有 ``form``），
        **没有负向参数**；它的负向词来自它自己的 ``character.negative_tags``，
        本插件不碰别的插件配置 —— 所以「塞负向」这条路在 nai_artist 上无效。

        与 1.0.36 的 ``[region_override]``（分区覆盖）**完全分开**：不同的配置节、
        不同的解析、不同的注入位置（外观覆盖**放最前**，分区覆盖追加在最后）。
        识别不到就**零动作**（不解析、不打日志、不注入）。
        """

        enabled: bool = Field(
            default=False,
            description=(
                "实验性功能（默认关闭）：外观覆盖（换眼睛 / 换发色）。"
                "打开后「眼睛换成本尊的紫红」「发色换成银白」这类说法会被解析成"
                "「哪个外观 + 什么颜色」，把**加权**的颜色词放到提示词**最前面**，"
                "并把画面里已有的角色标准标签**降权**（只改那一处，其它标签不动）。"
                "只从**用户最近几条消息**里做纯字符串解析，不调模型；"
                "关闭 / 识别不到 → 零动作（不解析、不打日志、不注入），"
                "其它注入一个字都不改"
            ),
        )
        eye_weight: float = Field(
            default=1.4,
            description=(
                "覆盖词（眼睛 / 发色）的**正向权重** —— 注意这个权重对两个外观键都生效，"
                "键名沿用需求里的 eye_weight。写成什么语法由 weight_syntax 决定"
                "（默认 1.4::purple-red eyes::）。"
                "**设为 1.0 即退化为「只把词放到最前、不加权」**；"
                "这是权重语法万一被下游翻译模型吞掉时的降级开关"
            ),
        )
        tag_weight: float = Field(
            default=0.8,
            description=(
                "被覆盖时对**画面里已有的角色标准标签**降权（0.8 → 0.8::firefly "
                "\\(honkai: star rail\\)::）。角色标签自带默认外貌（例如蓝眼）先验，"
                "不降权就会压过我们加的加权词。**只改画面里第一处、其它标签一个字不动**；"
                "**1.0 = 完全不降权**（这时只加权覆盖词）"
            ),
        )
        apply_to: list[str] = Field(
            default_factory=lambda: ["eyes", "hair"],
            description=(
                "允许覆盖的键**列表** —— **顺序就是注入顺序**（eyes 在最前面）。"
                "可用值：eyes（眼睛 / 瞳色 / 眼眸 / 双眼 / 瞳孔）、hair（发色 / 头发 / 发型）。"
                "没列出来的键不会被覆盖；整份都写错/留空时按默认两个键处理"
            ),
        )
        weight_syntax: str = Field(
            default="nai",
            description=(
                "权重语法。**默认 nai**：``1.4::purple-red eyes::`` —— 这是 nai_artist "
                "自己的翻译提示词里唯一认可的数值写法（它的 prompt_builder 明确要求"
                "「必须严格写成 N::tag::」，并禁止 ``1.5:gentle smile:`` 这类写法），"
                "所以默认用它。可选：``paren`` = ``((purple-red eyes:1.4))``"
                "（A1111/WebUI 风格括号写法，有被 nai_artist 翻译模型改写或丢弃的风险）；"
                "``none`` = 不加权重，只把词放最前（与 eye_weight = 1.0 等价）。"
                "写错时按 nai 处理"
            ),
        )

    general: GeneralSection = Field(default_factory=GeneralSection)
    roster: RosterSection = Field(default_factory=RosterSection)
    targets: TargetsSection = Field(default_factory=TargetsSection)
    multi: MultiSection = Field(default_factory=MultiSection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    outfits: OutfitsSection = Field(default_factory=OutfitsSection)
    region_override: RegionOverrideSection = Field(
        default_factory=RegionOverrideSection
    )
    appearance: AppearanceSection = Field(default_factory=AppearanceSection)
