"""char_prompt_guard 插件入口。

加载后订阅 before_action_call 事件，在生图 Action 执行前检测角色提示词：
已含角色则放行，未含则根据上文对话中的角色自动补入（支持多角色同框）。

同时把「角色补全已开启 + 可用的运行时标记 + 生图参数的标签书写规则」以
system reminder 的形式告知主模型，让它可以按需决定本次要单人还是多人、
用哪种注入方式，并把角色标准标签写在提示词最前面。

1.0.17 起再加一条**更强的**通道：插件加载时（以及「全部插件加载完成」/首次生图
事件时各重试一次）把同一句「最前面必须先写角色的标准标签」**直接追加进生图动作
的参数说明**（``general.tag_first_hint``，纯内存改动）—— 主模型在工具定义里就能
看到这条要求，比 system reminder 这种"旁白"有效得多。逻辑都在 :mod:`tag_hint`。

1.0.18 起关联一个**外部记忆库**（Booku Memory，``[memory]`` 节，见 :mod:`memory`）：
判定**不确定**时（主题跟踪的 ``split`` 拆单 / ``unavailable`` 本尊身份取不到 /
``low_confidence`` 置信度不足 / ``empty`` 无信号）去检索相关信息做一次「再确认」，
命中「触发词 → 角色表内角色」的明确对应就直接用作判定依据，并把这次确认写回
记忆库（``memory.write_back``）。关闭（``memory.enabled = false``）时**完全不发
任何请求**；不可用时静默降级 —— 判定链与 1.0.17 逐字一致。

1.0.19 起针对一次真实事故补上两处对账（都不新增配置项）：

- 参数说明的追加句（:data:`tag_hint.HINT_SUFFIX`）再补一句：标签必须与**本次画面
  描述的角色一致**，不要照搬备忘 / 历史记录 / 上一张图的标签；
- 二次一致性检测（``general.consistency_check``）的对账基准从「判定集合」扩成
  **期望集合**（判定集合 + 主题跟踪主题 + 记忆库确认结果），并且**描述里已经点名
  角色表内角色时**也会额外做一次「对账用」记忆库查询（只用于对账，不改判定、
  不写回）。细节见 :mod:`handler` 模块顶部与 :class:`handler.ExpectationSet`。

1.0.20 起再补上一处**独立**的检查（同一次事故的另一半，同样不新增配置项）：
用户这次点名的是**角色表之外**的角色（「刻律德拉」），画面里却挂着角色表内的
花火标签 —— 1.0.19 的三份证据（判定 / 主题跟踪 / 记忆库）**全是空的**，期望集合
为空 → 一致性检查直接跳过，该报警却没报。所以：

- 参数说明的追加句再补第三句：**要画的角色不在角色表里时，不要写任何标准标签**，
  只写画面描述（不要拿备忘 / 历史记录 / 上一张图的标签顶上）；
- 新增 :meth:`handler.CharacterGuardHandler._apply_outside_naming_check`：扫描
  「触发消息 + 触发前最近几条**用户**消息」提取**表外**点名（引号模式 + 句式模式，
  见 :func:`character.extract_outside_names`），若最终提示词里出现**用户这次没点过**
  的角色表内角色即为冲突 —— ``warn`` 只报不改、``fix`` **删除**画面里那个角色的
  名字 / 别名 / 标准标签（**保留外观描述原文**），``off`` 完全不检查。
  它与期望集合对账**互相独立**、可各自触发（跑在 :meth:`_finalize` 里、对账之后）。

1.0.21 起补上「删掉之后没有补上正确角色的标签」这半边（用户反馈「这个角色的提示词
没有进去」）：新增 ``general.auto_resolve_characters``（默认开）—— 用**已配置的模型**
（同一套 ``llm_api``，模型沿用 ``general.translate_model``、超时沿用 ``llm_timeout``，
不引入新依赖）把角色表之外的角色解析成「正式名 + 作品名 + NAI 标签」
（:func:`character.resolve_character_via_llm`），再走两个入口：

- **替换**：表外点名对账判成冲突 + ``consistency_check="fix"`` + 解析成功 →
  用解析出的标签**替换**画面里的错误标签（而不是只删除）；
- **补入**：画面里**没有任何角色标签**（例如描述只写了外观）+ 解析成功 →
  按 ``natural`` / ``tags`` 模式把解析结果补进提示词最前面（复用既有注入格式）。

解析结果严格校验（``unknown`` / JSON 坏 / 标签不符合规范 / ``confidence`` 低于 0.5
一律判失败）并缓存（命中缓存不重复问模型）；失败时逐字退回 1.0.20 的行为
（``warn`` 不变、``fix`` 只删除），**任何异常都不拦住生图**。

1.0.22 起再补上**「皮肤 / 时装」这一维**（``[outfits]`` 节，见 :mod:`outfit`）：
用户说「我想看你的某某某（皮肤）」时，把它解析成英文关键词并**追加**进提示词
（例如「我想看粉色妖精小姐」→ 爱莉穿着这一套的衣服）。**角色那一侧一个字都没改**
（描述点名 > 主题跟踪 > 记忆库 > LLM 仲裁 > 不补），新增的只是「这次要穿哪一套」：

- 皮肤名只从**用户消息**里找（触发消息 + 触发前最近 4 条用户消息，bot 消息跳过；
  **不扫画面描述**），句式 + 引号两种模式提取，再过长度 / 虚词 / 黑名单 / 通用词 /
  「已经在角色表里」五道过滤；命中 ``[outfits] entries`` 的候选优先；
- **表优先 → 模型兜底**：表命中零模型调用；表未命中且 ``auto_resolve`` 时用已配置的
  模型（同一套 ``llm_api`` / ``translate_model`` / ``llm_timeout``）解析，
  ``unknown`` / JSON 坏 / ``confidence < 0.5`` / 没有可用关键词一律判失败 ——
  **什么都不注入**（绝不瞎编），日志 ``皮肤解析(LLM)失败：<原因>``；
- 缓存按「皮肤名 + 角色名」，成功 24 小时、失败 10 分钟，**读写在调用方**；
- 注入放在 ``_finalize`` 的最后一步：``natural`` 追加
  ``（皮肤/时装：“粉色妖精小姐”：pink elf dress, fairy wings）``，``tags`` 把关键词
  追加进同一个参数串；``[outfits] enabled = false`` 时整条链路零开销。

1.0.23 起修三件事（判定链仍然一行未改，见 :mod:`handler` / :mod:`outfit`）：

- **皮肤名不再被当成表外角色名**：皮肤识别先跑，它认出来的皮肤 / 时装名通过
  ``exclude`` 传给 :func:`character.extract_outside_names` **豁免** —— 用户说
  「画粉色妖精小姐这套」时「粉色妖精小姐」是**一套皮肤**，不会为它多问一次模型、
  ``fix`` 也不会再去动画面里的角色标签（注入位置与语义一个字没变）；
- **作品名去重**（:func:`character._normalize_game_field`）：``game_cn`` 已含
  ``game_en`` 时只保留一个，不再拼出 ``… (Honkai: Star Rail) (Honkai: Star Rail)``；
- **``outfits.infer_unnamed``**（新增，**默认关**）：用户只说「流萤的那一套皮肤」
  而没点名具体皮肤时，开了才由模型推断该角色最知名的一套并注入
  （:func:`outfit.find_unnamed_outfit_owner` + :func:`outfit.infer_outfit_via_llm`，
  校验口径与 1.0.22 完全相同，日志标注「来源=模型推断」，推断结果不写回记忆库）；
  默认关时 0 次模型调用、无相关日志，行为与 1.0.22 逐字一致。

1.0.28 起把**「泛指的穿着类型」**和**「具体皮肤名」**分开（``[outfits] wear_types``，
见 :mod:`outfit` 与 :mod:`handler`）：用户说「我想看你的**学生装 / 居家服 / 泳衣 /
雨衣**」时，判断这不是某一套皮肤，而是**泛指的穿着** —— 插件**确定性**地给出英文
关键词并注入，日志与提示词里都能看到"到底穿了什么"，不再让 bot 黑箱决定。
**角色那一侧依然一个字没改**，优先级固定为：

① 皮肤表命中（零模型调用）→ ② 内置 + 配置的**穿着类型表**（零模型调用，
候选**本身是**表中词或**以它结尾**才命中、取最长的那个）→ ③ 皮肤模型解析
（``auto_resolve``）→ ④ **穿着模型解析**（既不在两张表、又看起来是穿着的候选，
走穿着专用提示词）→ ⑤ ``infer_unnamed``（默认关）。穿着那两条路不写回记忆库、
不参与角色对账（注入排在两条一致性检查之后），幂等按同类判；``enabled = false``
时整条链路零开销（只保留 1.0.26 起那条纯字符串豁免识别）。

1.0.29 起修独立只读审计发现的两条（**判定链、优先级、注入位置都没动**）：

- **穿着类型的匹配口径收紧**（:func:`outfit.find_wear_type`）：由"候选**包含**表中
  词"改为"候选**是**表中词或**以它结尾**"（归一后比较）—— 「我想看学生会」
  「学生时代的花火」这类**不含穿着意图**的话不再被零模型调用地注入
  ``school uniform, pleated skirt``（1.0.28 会；1.0.27 时同候选走模型兜底，
  模型答 ``unknown`` → 什么都不注入）。本身是键的写法（``学生装`` / ``婚服`` /
  ``和服`` …）与后缀写法（``穿学生装`` / ``男生学生``）照旧命中；代价是
  ``学生服`` / ``校服外套`` / ``婚纱照`` 这类"键 + 别的后缀"改为走 ④ 问一次模型；
- **穿着也做归属校验**（``handler._wear_character_hint`` + ``_apply_outfit``）：
  ② / ④ 产出的 ``ResolvedOutfit.character`` 填上"**说这句话的那条消息**里唯一
  提到的角色表内角色"（多个 / 没有就留空 → 照常注入）；非空且与本次判定角色
  不一致时**不注入 + WARNING**（``穿着跳过：穿着「婚纱」属于「爱莉希雅」，
  但本张判定角色是「流萤」``），与 1.0.26 起皮肤那条路的归属校验同一口径。

1.0.30 修掉 1.0.29 留下的那条端到端残留，并补三件产品需求（见 :mod:`outfit` /
:mod:`handler`；**原 C1~C103 与 C105/C106 的行为一个字都没改**）：

- **C104 端到端从抽取侧修**（「我想看学生会～」不再被注入校服）：句式抽取与
  **候选过滤**统一用同一张虚词表（两边都不把「会」当虚词，
  ``outfit._OUTFIT_STOP_CHARS`` / ``_OUTFIT_SENTENCE_STOP_CHARS`` /
  ``_OUTFIT_SENTENCE_NAME_CLASS``）—— 候选因此是完整的「学生会」，既不等于任何
  内置键、也不以任何键结尾 → 不走"零模型调用的确定性注入"，落到模型兜底
  （``unknown`` → 什么都不注入）。**两张表必须一起改**：只改句式那一侧时，完整的
  「学生会」会在候选过滤那一步被整条丢掉。另一条"边界判据"思路（候选后面紧跟 CJK
  字母即视为被截断）**经评估未采用**：它会误伤「她穿学生装的样子」这类正常写法
  （「学生装」后面紧跟的「的」也是 CJK 字母），要做到不误伤就得再引入一张
  "衣服名后面允许跟哪些字"的白名单 —— 等于把复杂度搬个地方；
- **补同义写法**（减少不必要的模型调用）：``学生服`` / ``学生制服`` / ``校服外套`` /
  ``校服裙`` / ``婚纱照`` / ``婚纱裙`` / ``和服裙`` / ``泳衣套装`` / ``居家服装`` /
  ``女仆装束`` / ``护士服装`` / ``西装外套`` / ``运动服装`` / ``旗袍裙`` / ``睡衣套装``
  各自成为 ``outfit.WEAR_TYPE_KEYWORDS`` 里的一条键 —— **1.0.31 校正**：它们**不是**
  "在 1.0.29 本来就命中、只是命中的是较短的键"（旧文案与实现不符），而是"**键 + 别的
  后缀**"的写法（``校服外套`` = ``校服`` + ``外套``），在 1.0.29 的"相等或后缀"口径下
  **14 条一个键都不命中**（``学生服`` / ``校服外套`` / ``校服裙`` / ``婚纱照`` /
  ``婚纱裙`` / ``和服裙`` / ``泳衣套装`` / ``居家服装`` / ``女仆装束`` / ``护士服装`` /
  ``西装外套`` / ``运动服装`` / ``旗袍裙`` / ``睡衣套装``，都要多问一次模型）、
  **1 条**（``学生制服``，它以 ``制服`` 结尾）**被较短的键抢走关键词**（军装风）；
  补成独立词条后 15 条**全部**零模型调用命中，日志写的也是用户的原话；
- **角色判定多一条最高优先信号**（``handler._latest_named_entry``）：用户
  **最新一条消息**（触发消息优先；它没点名才看触发前最近 1 条用户消息）里明确
  写出的**角色表内**角色（名字 / 别名 / 标签，走既有 ``find_characters_in_text``）
  **压过主题跟踪的粘性与代词回指** —— 用户场景 ``那画流萤吧`` → ``画爱莉的婚纱～``
  现在按用户最新说的爱莉希雅画、婚纱**正常注入**，日志写明
  ``判定依据=用户点名(最新消息):爱莉希雅（优先于主题跟踪:…）``（括号里是
  "**原来会生效的那个结论**"，用同一套代码算出来、只进日志）。
  口径刻意窄（**四条闸门**，各有独立用例）：① 含**追加语义词**
  （``character.ADDITIVE_WORDS``：还有 / 一起 / 俩 / 都 / …）或点名 **≥2 个**
  角色表内角色 → 不采用（交回主题跟踪的追加 / 多人分支：C34/C35/C109）；
  ② 含**自我指代** → 不采用（交给拆单 / 本尊身份：C54/C98/C110）、含 ``？`` / ``?``
  → 不采用（选择疑问句交 LLM 仲裁：C42/C110）；③ **只读清洗后的正文**
  （``[回复…]`` / ``[图片(…)]`` 里的名字是别人说的：C43/C111）；④ **保留**
  触发前那条的**时间衰减**闸门（低于 ``monitor.min_confidence`` → 不采用：
  C37/C52）与**描述点名优先**（描述里点名的是别人时仍按描述那一档处理，
  由 1.0.19 的期望集合对账报出 / 修正「用户最新说的人 ≠ 画面里的人」：C108）。
- **穿着识别补上「归属句式」**（``outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN``）：
  ``…的婚纱`` / ``…的居家服`` —— 词表就是内置穿着类型表的键，后面再要求一个
  "不再是名字字符"的边界（**1.0.31 起额外放行句末语气词、1.0.32 起这份额外放行收窄为那 10 个语气词**）。
  原先七条句式都抓不到「画爱莉的婚纱～」里的「婚纱」，
  判定侧改对了、衣服却还是没注入（C107/C108）；同一版还把**句式抽取与候选过滤**
  的虚词表统一成一张（都不把「会」当虚词），「我想看学生会～」的候选因此是完整的
  ``学生会``（C104）。

1.0.31 起只修两条行为（**判定链的规则、优先级次序与「用户最新点名」四条闸门的语义
都没动**；新增回归用例 C112/C113，原 C1~C111 的断言一个字都没改）：

- **触发消息没点名时，闸门①②不再拦它**（``handler._latest_named_entry`` 里新加的
  一条**前置判断**）：触发消息**一个角色表内角色都没有**时，跳过它的闸门①②
  （追加语义 / 疑问语气），直接落到"触发前最近 1 条用户消息"上并按**全部**闸门判定。
  用户场景 ``那画流萤吧`` → ``画爱莉的婚纱～`` → 触发 ``都试试？``（既含「都」＝追加
  语义词、又含「？」）：1.0.30 会让整条信号让位、退回旧粘性；1.0.31 起前一条的点名
  照旧读得到 —— 判定 = 爱莉希雅、婚纱正常注入（C112）。候选表最多两格
  （触发消息 + ``handler._LATEST_NAMED_HISTORY_LIMIT`` = 1 条），所以**只会**看
  "触发前最近 1 条用户消息"、**不会**往更早找；触发消息含**自我指代**时照旧让位
  （「你自己也来一张～」是"要本尊"，交给拆单 / 本尊身份那条链路：C45/C110）；
  闸门①（有名字时的追加语义）、闸门③、闸门④一个字都没改
  （C107/C108/C109/C110/C111 的语义因此原样保留）；
- **归属句式的边界容忍句末语气词**（``outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN``）：
  「画爱莉的**婚纱吧**～」/「画爱莉的**婚纱呢**」里「婚纱」后面紧跟的是句末语气词
  （``吧`` / ``呢`` / ``啊`` …），1.0.30 的硬边界把"后面还有汉字"一律挡掉 →
  判定对、衣服却没注入；1.0.31 起边界改成"后面**不是名字字符**，**或**是虚词字符"
  （C113）。``会`` 不在那张虚词表里，
  「我想看你的学生会长制服」照旧拆不出 ``学生``（C91 语义不变）；
- **文档口径校正**：内置穿着类型表那 15 条同义写法在 1.0.29 的实际情况是
  "14 条一个键都不命中 + 1 条（``学生制服``）被较短的键 ``制服`` 抢走关键词"
  （详见 ``outfit.WEAR_TYPE_KEYWORDS`` 与 ``find_wear_type`` 的 1.0.31 说明）。

1.0.32 起只收窄两处（审计的两条 🟠，**判定链的规则、优先级次序与四条闸门的语义
都没动**；新增回归用例 C114/C115，原 C1~C113 的断言一条都没改）：

- **归属句式的尾边界：不是名字字符即放行，另外只额外放行 10 个句末语气词**
  （``outfit._OUTFIT_TAIL_PARTICLES``）：1.0.31
  放行的是**整张** ``outfit._OUTFIT_STOP_CHARS``（``的`` / ``是`` / ``我`` / ``你`` /
  ``都`` … 全在里面），于是「爱莉的**婚纱是**我挑的」「流萤的**校服被**雨淋了」
  「画的**婚纱的**话」这类**不含穿着意图**的话也会被抓成穿着候选（1.0.30 不会）——
  抓错会把衣服画错，与「宁少勿多」相悖，而文档的承诺只是**额外放行句末语气词**。
  收窄之后「画爱莉的**婚纱吧**～」/「画爱莉的**婚纱呢**」照旧命中（C113 不变）、
  ``会`` 照旧不在任何放行表里（C91 不变），回归用例 C114 —— 一句话口径：
  **后面不是名字字符（标点 / 空白 / 串尾 / emoji）即放行，另外额外放行那 10 个句末
  语气词（``吧吗呢啊呀哦嘛呗啦咯喔``）；实词汉字不放行**；
- **触发消息是追加指令时，1.0.31 的让位不再"只看前一条的单名"**
  （``handler._additive_trigger_needs_tracking``）：触发消息**自己没点名**、却含
  **追加语义词**（``一起`` / ``都`` / ``还有`` …）且**不是疑问句**，而
  ``handler._overtaken_conclusion`` 算出的"原来会生效的结论"**没定死一个人**
  （多人 ≥2 角色，或「换人还是追加不确定」＝会交 LLM 仲裁）时，本次**不让位** ——
  交回主题跟踪 / LLM 仲裁。用户场景 ``画爱莉和流萤一起`` → ``流萤这张不错`` →
  触发 ``一起画吧``：1.0.30 会让位给 LLM 仲裁（可能两人），1.0.31 却直接按前一条的
  单名拍板并 ``return``（``判定依据=用户点名(最新消息):流萤``），**压过**主题跟踪的
  「换人还是追加不确定」与 LLM 仲裁 → **该画两人只画一人**；1.0.32 起不让位
  （回归用例 C115）。疑问句照旧让位（C112 的 ``都试试？`` 一字不改）。

1.0.33 起**只同步文档措辞，无行为改动**：归属句式 ``…的<穿着词>`` 的尾边界此前被写成
"1.0.32 起只放行句末语气词"，口径过强 —— 准确说法是"**后面不是名字字符（标点 / 空白 /
串尾 / emoji）即放行，另外额外放行 ``outfit._OUTFIT_TAIL_PARTICLES`` 里那 10 个
句末语气词（``吧吗呢啊呀哦嘛呗啦咯喔``）；实词汉字不放行**"：``画爱莉的婚纱吧～`` 命中，
而 ``爱莉的婚纱是我挑的`` / ``流萤的校服被雨淋了`` / ``画爱莉的婚纱的话`` 不命中。
正则、常量、判定链与用例（仍是 C1~C115 共 115 条）一个字都没动。

1.0.34 起**不再要求用户手工打框架补丁**（静态补丁仍然可用，二者**自动二选一**，由
:mod:`compat` 在插件加载时探测）：插件启动时检查 ``src/core/utils/llm_tool_call.py``
是否已经在发「执行前事件」——

- **已经在发**（打过 ``framework-patch/llm_tool_call-pre-events.patch``，或上游将来
  原生支持）→ **一行代码都不执行**，日志 ``检测到框架已含执行前事件（静态补丁），
  未重复包装``；
- **没在发** → 在**内存里**给 ``run_tool_call`` 补一层包装（**不写任何文件**、不改
  框架源码、不 import 补丁的内部符号），把订阅者写回的 ``args`` 交给原函数 ——
  与静态补丁二版等价，日志 ``已启用框架运行时兼容（等价 llm_tool_call-pre-events
  补丁 v2）``；
- **认不出**（模块 / 函数不在、源码读不到）→ **不装**（宁可不生效也不重复发事件），
  日志 ``框架版本不支持运行时兼容，已跳过…``。

配置开关 ``general.runtime_framework_compat``（默认开）关掉后不再探测。任何异常都只记
日志、**绝不拦住生图**，最坏等价"没装 shim"（行为与 1.0.33 逐字一致）。用例
C116~C118 用离线桩覆盖「已打补丁不重复装」「没打补丁则补上并能改写 args」「开关关掉 /
原函数抛异常 / 桩里没有 run_tool_call」三组行为。

1.0.36 起新增一个**实验性功能**「分区覆盖」（``[region_override]``，**默认关闭**，
见 :mod:`region` 与 :meth:`handler.CharacterGuardHandler._apply_region_override`）：

- ⚠️ **实验性功能：打开后生图效果可能不理想。** 分区拼接只是"按词重画"，模型可能把
  两个角色的特征混在一起（串味）、比例异常或细节崩坏；**默认关闭，普通使用完全不受
  影响**（关着时不解析、不额外读上文、不调模型、不打日志，行为与 1.0.34 逐字一致）；
- 打开后可以指定「**头 / 头发 / 眼睛** 用 A 的、**身子 / 体型** 用 B 的」这类分区拼接，
  也允许单分区（只换头）。入口语言形如 ``头是流萤的，身子是爱莉的`` / ``头换成流萤`` /
  ``用爱莉的身子配流萤的头``，识别「**分区词 + 角色名 + 归属句式**」**三重条件**
  （缺一不认），识别不到**什么都不做**；
- 各分区的特征词来自**独立的条目列表** ``[[region_override.entries]]``
  （**不写进** ``[roster]`` / ``[outfits]``）：每个条目一个角色（``name`` 支持角色表的
  名字 / 别名），``head`` / ``body`` / ``outfit`` / ``pose`` / ``background`` 分别填该
  角色各分区的特征词；
- **注入**按 ``regions`` 的顺序（默认 head → body → outfit → pose → background，
  **head 在最前**）拼成一段，追加在**角色说明与皮肤/穿着之后**（既有注入通道，
  角色在前）；``negative_swap`` 把「被换掉的那个角色在**同一分区**的原特征」写进负向，
  ``anti_mix`` 多角色时追加 ``mixed attributes, blended eyes``，``stylized_swap``
  才追加 ``stitched neck, visible seam, head slightly tilted``（默认不出现）；
- **负向写到哪**：只有目标动作**本身有负向参数**时才真的写进去
  （``[targets] list`` 新增第 7 段「负向参数名」，内置已为
  ``image_generator_plugin-neo:draw_image`` 填好 ``negative_prompt``）；
  nai_artist 的 ``share_photo`` / ``share_drawing`` **没有**负向参数（它的负向词在
  nai_artist 自己的 ``character.negative_tags`` 里，本插件不碰别的插件配置），
  那种情况只打一条 DEBUG；
- **不碰**角色判定、二次一致性检查、幂等、归属校验与记忆库写回：追加排在它们之后，
  特征词也不是角色表里的字符串，``fix`` 的替换 / 删除碰不到它。
  新增用例 C119~C124（离线），原 C1~C118 的断言一条都没改，全套变为 C124 条。

1.0.375：改了点 bug，并且优化（真的吗？你真的感觉得到吗）
"""

from __future__ import annotations

from typing import Any, ClassVar

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from . import compat, tag_hint
from .config import CharPromptGuardConfig
from .handler import _BUILTIN_TARGETS, CharacterGuardHandler

logger = get_logger("char_prompt_guard")

#: 运行时框架兼容（1.0.34）各状态对应的日志文案（见 :func:`compat.install`）。
#: 用户排查「为什么插件没生效」时看的就是这一行，所以每种状态都有确定的一句。
_RUNTIME_COMPAT_LOG: dict[str, str] = {
    compat.STATUS_APPLIED: (
        "已启用框架运行时兼容（等价 llm_tool_call-pre-events 补丁 v2）"
    ),
    compat.STATUS_ALREADY_PATCHED: (
        "检测到框架已含执行前事件（静态补丁），未重复包装"
    ),
    compat.STATUS_ALREADY_INSTALLED: "运行时兼容已安装，跳过",
    compat.STATUS_UNSUPPORTED: (
        "框架版本不支持运行时兼容，已跳过"
        "（如需生效请应用 framework-patch/llm_tool_call-pre-events.patch）"
    ),
    compat.STATUS_DISABLED: (
        "运行时框架兼容已关闭（general.runtime_framework_compat=false）"
    ),
}

#: 五种状态里唯一按 WARNING 打的那一个（其余都按 INFO）
_RUNTIME_COMPAT_WARNING = compat.STATUS_UNSUPPORTED

_REMINDER_BUCKET = "actor"
_REMINDER_NAME = "关于生图时的角色提示词补全"
_REMINDER_CONTENT = (
    "【生图角色补全已开启】你调用生图工具时，如果提示词里没有写出画面需要的角色，"
    "系统会自动根据最近对话把角色补进去；多人同框的场景也会一并补齐。"
    "你正常写画面描述即可，不需要为了这个机制改变习惯。"
    "需要显式控制时，可以在提示词里加入下列标记（系统会自动移除，不会进入画面）："
    "#solo# 本次只补一个角色；#multi# 强制按多人同框补齐；"
    "#chars# 用角色的独立槽位参数；#tags# 把角色标签直接追加进提示词；"
    "#nochar# 本次完全不要自动补角色。"
)

#: 生图参数的「标签在前」硬规则（1.0.12 起，随 ``general.notify_tag_rule`` 开关）。
#: 实测下游翻译模型会把中文角色名翻坏（「花火」→ ``hanabi (firefly) (honkai: star rail)``，
#: 括号未转义还串了角色），标签写错位置（追加在末尾/混在描述中间）时权重与稳定性都更差，
#: 所以在 reminder 里明确要求主模型**自己就把标准标签写在最前面**。
_REMINDER_TAG_RULE = (
    "【生图参数硬规则】调用生图动作（share_photo / share_drawing / draw_image）时，"
    "提示词参数（content / content_description）**最前面必须先写角色的标准标签**"
    "（角色表第 4 列，例如 Hanabi \\(honkai: star rail\\)），然后再写画面描述；"
    "角色表没有提供标签时，写「角色名（游戏名）」。"
)


def _tag_rule_enabled(config: Any) -> bool:
    """读取 ``general.notify_tag_rule``；读不到时按默认（**包含**这条规则）处理。

    配置对象缺失、字段缺失或取值不是布尔时一律返回 True：reminder 是给主模型的
    提示，多一条硬规则不会让生图失败，少一条却会让标准标签被翻译模型改坏。
    """
    general = getattr(config, "general", None)
    value = getattr(general, "notify_tag_rule", None)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(value)


def build_reminder_content(config: Any = None) -> str:
    """拼出 system reminder 的正文。

    - 标记说明（``#multi#`` / ``#nochar#`` 等）是 1.0.11 及以前就有的内容，
      **任何情况下都不能丢**；
    - 「生图参数最前面先写标准标签」这条硬规则由 ``general.notify_tag_rule``
      控制（默认开启），关掉后只保留标记说明。
    """
    if _tag_rule_enabled(config):
        return f"{_REMINDER_CONTENT}\n{_REMINDER_TAG_RULE}"
    return _REMINDER_CONTENT


@register_plugin
class CharPromptGuardPlugin(BasePlugin):
    """角色提示词守卫插件。"""

    plugin_name: str = "char_prompt_guard"
    plugin_description: str = (
        "生图前检测角色提示词：已含角色则放行，否则根据上文自动补入角色名+游戏名"
        "（或 NAI 角色标签），支持多角色同框"
    )
    plugin_version: str = "1.0.375"

    # 注意：这里刻意**不加** ClassVar 注解 —— MPDT 的 ConfigValidator 用 libcst 只识别
    # 普通赋值（cst.Assign），写成 `configs: list[type] = [...]` 会被判成"插件未定义配置类"。
    configs = [CharPromptGuardConfig]  # noqa: RUF012
    dependencies: ClassVar[list[str]] = []

    def get_components(self) -> list[type]:
        """返回插件提供的组件类列表。"""
        return [CharacterGuardHandler]

    @staticmethod
    def _log_runtime_capability() -> None:
        """启动时打一条能力自检日志，避免"装上了却没反应"无处排查。

        两条执行路径分别依赖不同的钩子：

        - ``before_action_call``：`ActionManager` 路径会发；
        - ``before_tool_call``：agent 聊天器把 Action 当工具调用时才会用到。

        核心没提供 ``before_tool_call`` 时说明核心偏旧；核心提供了事件、
        但 agent 聊天器路径仍不发（上游在 ``llm_tool_call.py`` 直接
        ``_wrap_execute``）时，需要打框架补丁，这里只能提示、无法自检。
        """
        from src.app.plugin_system.types import EventType

        has_tool_event = getattr(EventType, "BEFORE_TOOL_CALL", None) is not None
        if has_tool_event:
            logger.info(
                "char_prompt_guard 运行环境自检：核心提供 before_tool_call，"
                "已同时订阅 before_action_call + before_tool_call"
            )
        else:
            logger.warning(
                "char_prompt_guard 运行环境自检：核心未提供 before_tool_call，"
                "只订阅 before_action_call；agent 聊天器路径需要更新核心或打框架补丁"
            )

    @staticmethod
    def _install_runtime_compat(config: Any) -> None:
        """1.0.34：探测框架是否已经在发「执行前事件」，没发就在内存里补一层。

        判定与包装全在 :mod:`compat`（**只改内存，不写任何文件**）：框架已经在发
        （打过静态补丁 / 上游将来原生支持）时**什么都不做**；没在发且结构认得出时
        才原地包一层 ``run_tool_call``；认不出就不装。这里只负责把返回的状态翻成
        一条 INFO / WARNING —— 用户排查「为什么没生效」看的就是这一行。
        """
        status = compat.install(logger, config)
        message = _RUNTIME_COMPAT_LOG.get(status)
        if message is None:
            logger.debug(f"运行时兼容：未知状态 {status!r}，已跳过")
        elif status == _RUNTIME_COMPAT_WARNING:
            logger.warning(message)
        else:
            logger.info(message)

    async def on_plugin_loaded(self) -> None:
        """插件加载时告知主模型角色补全机制、可用标记与标签书写规则。"""
        config = self.config
        if not isinstance(config, CharPromptGuardConfig):
            config = CharPromptGuardConfig()

        self._log_runtime_capability()

        # 1.0.34：探测框架是否已经在发「执行前事件」，没发就在内存里补一层
        # （让没打 framework-patch 的用户也开箱可用；已打补丁的机器一行代码都不执行）。
        # 与下面的 reminder / 参数说明注入**互相独立**：那两条失败也不影响兼容层，
        # 这里失败（极罕见）同样只记日志、不影响它们。
        self._install_runtime_compat(config)

        # 1.0.17：把「标签写在最前面」直接追加进生图动作的**参数说明**（纯内存改动，
        # 不写文件）。这是比 reminder 强得多的通道，所以放在 reminder 之前、且不受
        # multi.notify_bot 影响 —— 两条通道互相独立，各有各的开关
        # （tag_first_hint / notify_tag_rule）。
        #
        # 本插件 dependencies 为空，拓扑排序通常把它排在 nai_artist /
        # image_generator_plugin-neo **前面**，所以这一次多半会因为「目标动作还没注册」
        # 而空手而归；那种情况下 handler 会在「全部插件加载完成」或首次生图事件时
        # 再试一次（见 tag_hint.maybe_inject_on_event）。这里失败只记 DEBUG，不报错。
        tag_hint.inject_on_load(_BUILTIN_TARGETS, config=config)

        if not config.general.enabled or not config.multi.notify_bot:
            logger.debug("char_prompt_guard 未注入标记说明 reminder")
            return

        try:
            from src.app.plugin_system.api import prompt_api

            prompt_api.add_system_reminder(
                bucket=_REMINDER_BUCKET,
                name=_REMINDER_NAME,
                content=build_reminder_content(config),
            )
            logger.debug(
                "char_prompt_guard 标记说明 reminder 已注册"
                f"（标签硬规则：{'含' if _tag_rule_enabled(config) else '不含'}）"
            )
        except Exception as e:  # noqa: BLE001 —— 提醒注入失败不该影响插件加载
            logger.warning(f"注册 system reminder 失败: {e}")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时移除 reminder，并把改过的参数说明还原。

        注意：``prompt_api`` 目前只提供 reminder 的 ``add`` / ``get``，
        **没有 bucket 级删除接口**（只有 stream 级有 ``delete_stream_reminder``），
        所以这里只能直接走底层的 SystemReminderStore；等上游补上 API 再换回
        prompt_api。store 是纯内存单例，进程重启即清空。

        1.0.17 起参数说明也是内存内改动，同样在卸载时还原（``tag_hint`` 只记
        「改过哪几个参数 + 原说明」），尽力而为、失败只记 DEBUG。
        """
        try:
            tag_hint.restore_tag_first_hint()
        except Exception as e:  # noqa: BLE001 —— 还原失败不该打断卸载流程
            logger.debug(f"还原生图动作的参数说明失败: {e}")

        try:
            from src.core.prompt import get_system_reminder_store

            get_system_reminder_store().delete(_REMINDER_BUCKET, _REMINDER_NAME)
            logger.debug("char_prompt_guard 标记说明 reminder 已移除")
        except Exception as e:  # noqa: BLE001 —— 卸载清理失败只记日志，不打断卸载流程
            logger.debug(f"移除 system reminder 失败: {e}")
