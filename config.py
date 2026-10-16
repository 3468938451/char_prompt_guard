"""char_prompt_guard 插件配置。"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


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
                "「插件名:action:动作名 | 提示词参数名 | 追加模式 | 模式参数 | 额外检测参数」，"
                "后三项可省略。追加模式 natural=追加中文说明（生图插件会再翻译成标签，适配 nai_artist）；"
                "tags=直接追加 NAI 角色标签（提示词本身就是标签，适配 image_generator_plugin-neo）。"
                "留空使用内置默认："
                "nai_artist:action:share_photo | content | natural ; "
                "nai_artist:action:share_drawing | content | natural ; "
                "nai_artist:action:share_visual | content | natural | mode（nai_artist 1.2.x 及更早的旧动作名）; "
                "image_generator_plugin-neo:action:draw_image | content_description | tags"
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
        """皮肤/时装（装扮）识别与注入配置（1.0.22 起；1.0.23 起多一个推断开关）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用「皮肤/时装」识别与注入：用户说「我想看你的 XXXX」这类"
                "点了一套皮肤/时装时，把它解析成英文关键词**追加**进提示词"
                "（角色照旧由原来的判定链决定，这一维只做「多追加一层」，"
                "失败时一个字都不改）。关闭后整条链路零开销："
                "不读上文、不调模型、不碰提示词"
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
                "关闭后只有皮肤表里写得出的皮肤会被注入"
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
                "模型推断出来的那一套（见 infer_unnamed）不写回"
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

    general: GeneralSection = Field(default_factory=GeneralSection)
    roster: RosterSection = Field(default_factory=RosterSection)
    targets: TargetsSection = Field(default_factory=TargetsSection)
    multi: MultiSection = Field(default_factory=MultiSection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    outfits: OutfitsSection = Field(default_factory=OutfitsSection)
