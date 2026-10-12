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
                "例：爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia | elysia \\(honkai impact 3rd\\)"
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
                "nai_artist:action:share_visual | content | natural | mode ; "
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
                "多人判定方式：bot=由主模型标记与 LLM 判定自动切换（推荐）；"
                "always=总是按多人处理；off=总是按单人处理"
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

    general: GeneralSection = Field(default_factory=GeneralSection)
    roster: RosterSection = Field(default_factory=RosterSection)
    targets: TargetsSection = Field(default_factory=TargetsSection)
    multi: MultiSection = Field(default_factory=MultiSection)
