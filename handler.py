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

1.0.28 起把**「泛指的穿着类型」**和**「具体皮肤名」**分开（需求原话：「当说的是
『我想看你的 xxx』（如**学生**、**居家**、**泳衣**），判断**不是皮肤**，转成
『穿对应的衣服』；否则衣服这个产出有时相当于黑箱，只能由 bot 决定 —— 要能
**看到注入了什么样的衣服**」）。角色那一侧**依然一个字没改**，变的只有"这次要穿
什么、由谁决定"：

- **最终优先级**（①→⑤，见 :meth:`CharacterGuardHandler._resolve_outfit`）：
  **① 皮肤表命中**（零模型调用）→ **② 内置 / 配置的穿着类型表**
  （``[outfits] wear_types`` + ``outfit.WEAR_TYPE_KEYWORDS``，**零模型调用**）
  → **③ 皮肤模型解析**（``outfits.auto_resolve``，针对具体皮肤名）
  → **④ 穿着模型解析**（同样 ``auto_resolve``，针对"看起来是穿着"的候选，
  穿着专用提示词）→ **⑤ ``infer_unnamed``**（默认关）。③ 与 ④ 互斥、
  ⑤ 与"已识别到穿着 / 皮肤"互斥；
- **穿着类型表**：候选**本身是**表中某个词、或**以它结尾**才算命中
  （``学生装`` / ``居家服`` / ``泳衣`` / ``穿学生装``；1.0.29 起收紧，
  ``学生会`` / ``学生时代`` 这种"只是包含"的不再命中），
  多个词命中取**最长**的；命中日志
  ``穿着类型解析(内置表): 学生 → school uniform, pleated skirt``
  （用户补充的表写 ``(配置表)``），注入日志
  ``已追加穿着（share_visual / natural）: 学生 → school uniform, pleated skirt``；
- **穿着模型解析**：候选既不在皮肤表也不在穿着类型表、但**看起来是穿着**
  （含 ``衣/服/装/裙/…`` 或句式「穿 X」「换上 X」）时，用穿着专用提示词
  （"不是穿着就 ``unknown=true``"）转成服装关键词；缓存键 ``<原词>|__wear__``
  （成功 24 小时 / 失败 10 分钟，读写在调用方
  :meth:`CharacterGuardHandler._resolve_wear_with_cache`），日志
  ``穿着解析(模型): 雨衣 → raincoat`` / ``穿着解析(缓存)`` /
  ``穿着解析(LLM)失败：<原因>``；成功额外给一条
  ``建议加入穿着类型表：雨衣 | raincoat``（只打日志），**不写回记忆库**；
- **措辞与幂等**：穿着那条路在 natural 模式写 ``（穿着：“雨衣”：raincoat）``
  （与 ``（皮肤/时装：“…”：…）`` **刻意分开**）；幂等按**同类**判
  （皮肤看 ``（皮肤/时装：“``、穿着看 ``（穿着：“``），同类不重复追加、
  **不同类可以共存**；
- **豁免与候选选择**：本次识别到的**全部候选**（含被"一图一套"规则忽略的那些）
  都进豁免集 —— 穿着词与皮肤名一样不会被当成"角色表外的角色名"（否则角色解析会
  多问一次模型、``fix`` 还可能误删画面里的正确标签）；多个候选时照旧只处理
  **最新一条消息里最长的那个**，日志写明被忽略的候选；
  ``outfits.enabled = false`` 时仍然只做 1.0.26 那条**纯字符串**识别（只为豁免，
  皮肤表里确实有的才豁免），不注入、不调模型、日志里也没有 ``穿着类型解析``。

1.0.29 起修审计发现的两条（**判定链、优先级、注入位置都没动**）：

- **穿着类型的匹配口径收紧**（:func:`outfit.find_wear_type`）：由"候选**包含**
  表中词"改成"候选**是**表中词或**以它结尾**"（归一后比较）—— 「我想看学生会」
  「学生时代的花火」这类**不含穿着意图**的话不再被零模型调用地注入
  ``school uniform, pleated skirt``（1.0.28 会；1.0.27 时同样的候选走模型兜底，
  模型答 ``unknown`` → 什么都不注入）。**本身是键**的写法（``学生装`` / ``婚服`` /
  ``和服`` …）与**后缀**写法（``穿学生装`` / ``男生学生``）照旧命中；代价是
  ``学生服`` / ``校服外套`` / ``婚纱照`` 这类"键 + 别的后缀"从零模型调用降级为
  走 ④ 问一次模型（残余盲区，见 README 与 ``docs/multi-character-design.md``）；
- **穿着也做归属校验**（:meth:`CharacterGuardHandler._wear_character_hint` +
  :meth:`CharacterGuardHandler._apply_outfit`）：② / ④ 产出的
  ``ResolvedOutfit.character`` 填上"**说这句话的那条消息**里唯一提到的角色表内
  角色"（多个 / 没有就留空 → 照常注入）；非空且与本次判定角色不一致时
  **不注入 + WARNING** ``穿着跳过：穿着「婚纱」属于「爱莉希雅」，但本张判定角色是
  「流萤」`` —— 与 1.0.26 起皮肤那条路的归属校验同一口径（线索不进穿着缓存：
  缓存键只有原词，通用服装词与角色无关）。

1.0.30 起补四件事（**判定链的规则、优先级次序与注入位置都没动**）：

- **用户最新点名 = 角色判定的最高优先**（:meth:`CharacterGuardHandler._latest_named_entry`）：
  用户**最新一条消息**（**触发消息**优先；它没点名才看**触发前最近 1 条用户消息**）
  里明确写出的**角色表内**角色（名字 / 别名 / 标准标签，走既有
  :func:`character.find_characters_in_text`）**压过主题跟踪的粘性与代词回指**。
  实测场景 ``你：那画流萤吧`` → ``你：画爱莉的婚纱～``：婚纱原本会因归属冲突
  "不注入 + 告警"，现在判定 = 爱莉希雅、婚纱**正常注入**，日志
  ``判定依据=用户点名(最新消息):爱莉希雅（优先于主题跟踪:流萤）`` ——
  括号里写的是"**没有这条信号时本次会怎么判**"（由
  :meth:`CharacterGuardHandler._overtaken_conclusion` 用同一套代码算出来，只进日志）。
  口径刻意窄（宁可不采用，也不抢走既有链路），**1.0.30 定型为四条闸门**：
  ① **追加语义 / 多人 → 不采用**（含 ``character.ADDITIVE_WORDS`` 或点名 ≥2 个角色时
  退回主题跟踪的追加/多人分支 —— 「还有伊甸，一起画」是"本条 + 上一条"，
  回归用例 C34/C35/C109）；② **自我指代 / 疑问语气 → 不采用**（含自我指代词 =
  "本尊 vs 壳"交给拆单 1.0.16 与本尊身份那条链路；含 ``？``/``?`` 的选择疑问句
  「是爱莉还是你？」交给 LLM 仲裁 —— 回归用例 C42/C110）；③ **只读清洗后的正文**
  （触发消息走 :func:`character.strip_quoted_blocks`、历史消息走
  :func:`character.message_text`，``[回复…]`` / ``[图片(…)]`` 里的名字是别人说的，
  回归用例 C43/C111）；④ **保留**时间衰减闸门（``_LATEST_NAMED_MIN_DECAY``，与
  ``monitor.min_confidence`` 同值同义）、**描述点名优先**与触发消息自身点名的原路径
  （C37/C52/C108）。它**压不过描述点名**（描述里写了角色时调用方早就放行）——
  描述里点名的是**别人**时，1.0.19 的期望集合对账会把「用户最新说的人 ≠ 画面里的人」
  报出来（``warn`` 只报 / ``fix`` 替换），日志与依据都能看出这一点；
- **C104 的端到端从抽取侧修**（见 :mod:`outfit` 的 1.0.30 说明）：「我想看学生会～」
  的候选不再被虚词「会」截断成 ``学生``，因此不再零模型调用地注入校服 —— 这与
  1.0.29 那条"匹配口径收紧"是同一件事的两半（一个在匹配侧、一个在抽取侧）；
- **穿着类型表的同义写法补齐**（``学生服`` / ``校服外套`` / ``婚纱照`` 等 15 条）：
  1.0.29 的残余盲区因此收口，又是零模型调用命中（详见
  :data:`outfit.WEAR_TYPE_KEYWORDS`）；
- **穿着识别补上「归属句式」**（``…的婚纱`` / ``…的居家服``，见
  :data:`outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN`）：用户说「画爱莉的婚纱～」时
  「婚纱」既不在皮肤表里、也不符合七条句式里的任何一条 —— 穿着这一层因此什么都
  识别不到（C107/C108 卡住的地方）。词表直接用内置穿着类型表的键 + 一个"后面不再
  接名字字符"的边界（**1.0.31 起额外放行句末语气词、1.0.32 起这份额外放行收窄为那 10 个语气词**），
  所以抓到的候选**只可能是已知的穿着词**
  （``我想看你的学生会长制服`` 里的「学生」不会被拆出来，C91 守的就是这条）。
  同一版还把**候选过滤**的虚词表与**句式抽取**对齐（都不再拿 ``会`` 当虚词），
  「我想看学生会～」的候选因此是完整的 ``学生会``（详见 :mod:`outfit`）。

1.0.31 起只动两处（**判定链的规则、优先级次序与闸门语义都没动**）：

- **触发消息没点名时，闸门①②不再拦它**（:meth:`CharacterGuardHandler._latest_named_entry`
  里新加的一条**前置判断**）：触发消息**一个角色表内角色都没有**时，跳过它的
  闸门①②，直接落到"触发前最近 1 条用户消息"上并按**全部**闸门判定 ——
  用户场景 ``那画流萤吧`` → ``画爱莉的婚纱～`` → 触发 ``都试试？``（既含「都」＝追加
  词、又含「？」＝疑问句）：1.0.30 会让整条信号让位、退回旧粘性，1.0.31 起照旧读到
  前一条的点名（回归用例 C112）。候选表最多两格（触发消息 + 1 条历史），所以
  **只会**看"触发前最近 1 条用户消息"、**不会**往更早找；触发消息含**自我指代**时
  照旧让位（C45/C110）。闸门①（有名字时的追加语义）、闸门③、闸门④一个字都没改
  （C107/C108/C109/C110/C111 的语义因此原样保留）；
- **归属句式的边界容忍句末语气词**（:data:`outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN`）：
  「画爱莉的婚纱吧～」/「画爱莉的婚纱呢」里「婚纱」后面紧跟的是语气词（``吧`` / ``呢``
  / ``啊`` …），1.0.30 的硬边界把"后面还有汉字"一律挡掉 → 判定对、衣服却没注入；
  1.0.31 起边界改成"后面**不是名字字符**，**或**是虚词字符"。``会`` 不在那张虚词表里，
  「我想看你的学生会长制服」照旧拆不出 ``学生``（C91 语义不变，回归用例 C113）。

1.0.32 起只收窄两处（**判定链的规则、优先级次序与四条闸门的语义都没动**；
原 C1~C113 的断言一条都没改，新增回归用例 C114/C115）：

- **归属句式的尾边界：不是名字字符即放行，另外只额外放行 10 个句末语气词**
  （:data:`outfit._OUTFIT_TAIL_PARTICLES`，
  见 :mod:`outfit` 的 1.0.32 说明）：1.0.31 放行的是**整张**
  ``outfit._OUTFIT_STOP_CHARS``（``的`` / ``是`` / ``我`` / ``你`` / ``都`` … 全在里面），
  于是「爱莉的**婚纱是**我挑的」「流萤的**校服被**雨淋了」「画的**婚纱的**话」这类
  **不含穿着意图**的话也会被当成穿着候选（1.0.30 不会）—— 与"抓错会把衣服画错、
  宁少勿多"相悖，而文档的承诺只是**额外放行句末语气词**。收窄之后
  「画爱莉的**婚纱吧**～」/「画爱莉的**婚纱呢**」照旧命中（C113 不变），
  ``会`` 照旧不在任何放行表里（C91 不变），回归用例 C114 —— 一句话口径：
  **后面不是名字字符（标点 / 空白 / 串尾 / emoji）即放行，另外额外放行那 10 个句末
  语气词（``吧吗呢啊呀哦嘛呗啦咯喔``）；实词汉字不放行**；
- **触发消息是追加指令时，1.0.31 的让位不再"只看前一条的单名"**（审计 🟠2，
  见 :meth:`CharacterGuardHandler._additive_trigger_needs_tracking`）：触发消息
  **自己没点名**、却含**追加语义词**（``一起`` / ``都`` / ``还有`` …）且**不是疑问句**，
  而 :meth:`CharacterGuardHandler._overtaken_conclusion` 算出的"原来会生效的结论"
  **没定死一个人**（多人 ≥2 角色，或「换人还是追加不确定」＝会交 LLM 仲裁）时，
  本次**不让位** —— 交回主题跟踪 / LLM 仲裁。用户场景
  ``画爱莉和流萤一起`` → ``流萤这张不错`` → 触发 ``一起画吧``：1.0.30 会让位给
  LLM 仲裁（可能两人），1.0.31 却直接按前一条的单名拍板并 ``return``
  （``判定依据=用户点名(最新消息):流萤``），**压过**主题跟踪的"换人还是追加不确定"
  与 LLM 仲裁 → **该画两人只画一人**；1.0.32 起这次不让位（回归用例 C115）。
  疑问句照旧让位（C112 的 ``都试试？`` 不受影响），其余情形与 1.0.31 **一字不差**。

1.0.33 起**只同步文档措辞，无行为改动**：归属句式 ``…的<穿着词>`` 的尾边界此前写成
"1.0.32 起只放行句末语气词"，口径过强 —— 准确说法见上面那条（**后面不是名字字符
（标点 / 空白 / 串尾 / emoji）即放行，另外额外放行那 10 个句末语气词；实词汉字不放行**），
正则、常量、判定链与用例（仍是 C1~C115 共 115 条）一个字都没动。

1.0.34 起**只加一层运行时兼容，判定链一个字都没动**（当时本文件**一行未改**）：插件加载时由
:mod:`compat` 探测 ``src/core/utils/llm_tool_call.py`` 是否已经在发本文件订阅的那两个
「执行前事件」（``before_action_call`` / ``before_tool_call``）—— 没发就在**内存里**给
``run_tool_call`` 补一层包装（**不写任何文件**、不改框架源码、不 import 补丁的内部符号），
已打静态补丁 / 上游原生支持时**一行代码都不执行**。事件参数与既有发布点同形
（``signature`` / ``action_name`` / ``tool_name`` / ``args`` / ``message`` / ``stream_id``），
``stream_id`` 的解析照旧走本文件的 :func:`resolve_stream_id`，所以**判定链无需任何改动**。
新增用例 C116~C118（离线桩）覆盖「识别已打补丁 / 补上并改写 args / 开关与异常兜底」。

1.0.36 起新增一个**实验性功能**「分区覆盖」（``[region_override]``，**默认关闭**，
解析与拼装在 :mod:`region`，注入在本文件的 :meth:`CharacterGuardHandler._apply_region_override`）：

- ⚠️ 实验性功能：打开后**生图效果可能不理想**（模型可能把两个角色的特征混在一起、
  比例异常或细节崩坏）；**默认关闭，普通使用完全不受影响** —— 关着时
  ``_resolve_region_override`` 第一行就返回（不解析、不额外读上文、不调模型、不打日志）；
- 打开后「头/头发/眼睛 用 A 的、身子/体型 用 B 的」这类说法会被解析成
  ``{head: B, body: A}``（**分区词 + 角色名 + 归属句式**三重条件，缺一不认），
  按 ``regions`` 的顺序（默认 head 在最前）拼成一段追加在**角色与皮肤/穿着之后**；
  各分区特征词来自**独立的条目列表** ``[[region_override.entries]]``
  （不是 ``[roster]``、也不是 ``[outfits]``），找不到条目 → 退回不分区 + DEBUG；
- ``negative_swap`` 的负向词只写进目标动作**自己**的负向参数
  （``TargetSpec.negative_arg``，内置只有 neo 的 ``draw_image`` 有），
  nai_artist 没有负向参数 → 只打 DEBUG；
- **不碰**判定链、一致性检查、幂等、归属校验与记忆库写回（注入排在它们之后）。
  新增用例 C119~C124（离线），原 C1~C118 的断言一条都没改，全套变为 C1~C124 共 124 条。
"""

from __future__ import annotations

import importlib
import json
import math
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.message_api import (
    build_readable_messages_to_str,
    get_recent_messages,
)
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from . import appearance as appearance_api
from . import memory as memory_api
from . import region as region_api
from . import tag_hint
from .character import (
    ADDITIVE_WORDS,
    BASIS_LATEST_MENTION,
    BASIS_LATEST_NAMED,
    BASIS_USER_NAMED,
    CharacterEntry,
    NamingDecision,
    ResolvedCharacter,
    character_in_texts,
    contains_any_word,
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
    message_time,
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
    OUTFIT_WEAR_MODEL,
    OUTFIT_WEAR_TYPE,
    OutfitEntry,
    ResolvedOutfit,
    WearTypeMatch,
    extract_outfit_names,
    find_outfit_entry,
    find_unnamed_outfit_owner,
    find_wear_type,
    infer_outfit_via_llm,
    outfit_cache_get,
    outfit_cache_key,
    outfit_cache_put,
    outfit_infer_cache_key,
    outfit_wear_cache_key,
    parse_outfits,
    resolve_outfit_via_llm,
    resolve_wear_via_llm,
    wear_like_candidate,
    wear_type_table,
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

# ── 「用户最新一条消息里点名的角色表内角色」（1.0.30）──────────────────
#
# 用户场景（需求原话）：
#
#     你：那画流萤吧
#     你：画爱莉的婚纱～
#
# 现在判定仍是**流萤**（主题跟踪的粘性 + 代词回指把它按住），于是穿着冲突只能
# 「不注入 + 告警」。用户希望**按他最新说的角色来画**（用"用户说的名字"，
# 不是"从外观猜"）—— 所以新增一条**最高优先**的信号。
#
# 口径（窄到只够解决这个问题，见 :meth:`CharacterGuardHandler._latest_named_entry`）。
# **1.0.30 定型为四条闸门**（缺一不可，每条都有独立回归用例）：
#
#   ① **追加语义 / 多人 → 不采用**：最新一条消息含追加语义词
#      （``character.ADDITIVE_WORDS``：还有 / 一起 / 俩 / 都 / 加上 / 和她 / and / both）
#      或**点名 ≥2 个**角色表内角色时，这条信号一律让位 ——
#      「还有伊甸，一起画」是「本条 + 上一条点名」（回归用例 C35/C109），
#      「她俩一起」是多人同框（C34），都该由主题跟踪 / ``resolve_naming_decision``
#      的多人分支给出结论，不能被"最新一条里恰好 1 个角色"这条窄口径抢走；
#   ② **自我指代 / 疑问语气 → 不采用**：最新一条消息含
#      :func:`character.find_self_reference_word` 或含 ``？`` / ``?`` 时让位 ——
#      「本尊 + 角色名」要的是"本尊 vs 壳"（交给拆单 1.0.16 与本尊身份那条链路），
#      选择疑问句「是爱莉还是你？」要的是"换人还是追加不确定 → 交 LLM 仲裁"
#      （回归用例 C42/C110：LLM 说花火就是花火）；
#   ③ **只读清洗后的正文**：正文一律走 :func:`character.message_text` 口径
#      （先剥 ``[回复…]`` / ``[图片(…)]`` 这类中括号块）—— 触发消息用
#      :func:`character.strip_quoted_blocks`、历史消息用
#      :func:`character.message_text`，两边同一口径。引用块里的名字是**别人说的**
#      或只是文件名，不是用户本次的点名（回归用例 C43/C111）；
#   ④ **保留（1.0.30 之前就有的两件事，一个字都没改）**：
#      · 触发前那条用户消息要**够新**：时间衰减 ≥ :data:`_LATEST_NAMED_MIN_DECAY`
#        （与主题跟踪同一个公式 ``exp(-Δt / monitor.half_life_minutes)`` 与同一个
#        阈值 ``monitor.min_confidence``，默认 45 分钟半衰期 → 约半小时内说的才算）。
#        「画爱莉希雅」（3 小时前）+ 触发「嗯哼？」这种**旧粘性**场景必须照旧不补
#        （回归用例 C52/C37 守的就是这条）；
#      · **描述点名优先**：画面描述里已经写了角色表内角色时，调用方（``execute``
#        的 ``present_keys`` 分支）早就按描述那一档处理了，走不到这里；
#        描述里写的是**别人**时，判定按这里的结论走，由 1.0.19 的期望集合对账
#        把「用户最新说的人 ≠ 画面里的人」报出来（``warn`` 只报 / ``fix`` 替换，
#        回归用例 C108）；
#      · 触发消息本身点名时**沿用原路径**（触发消息不需要衰减闸门：它就是"这一句"）。
#
# **1.0.31 只加一条前置判断（目标 A，不改上面任何一条闸门的语义）**：**触发消息**
# **一个角色表内角色都没有**时，**跳过它的闸门①②**，直接落到"触发前最近 1 条用户
# 消息"上、并在那一条上判**全部**闸门 —— 用户场景 `那画流萤吧` → `画爱莉的婚纱～`
# → 触发 `都试试？`（既含「都」＝追加词、又含「？」＝疑问句，闸门①②全会命中）：
# 1.0.30 会让整条信号让位、退回旧粘性（可能仍按流萤 + `穿着跳过`），1.0.31 起前一条
# 的点名照旧读得到（回归用例 C112）。候选表最多两格，所以**只会**看"触发前最近 1 条
# 用户消息"、**不会**往更早找（**不是**全循环 `if not named: continue`）。
# **例外**：触发消息含**自我指代**时照旧让位（「你自己也来一张～」是要本尊，
# 交给拆单 / 本尊身份那条链路：C45/C110）。
#
# **1.0.32 给这条让位加一个兜底（审计 🟠2，不改上面任何一条闸门）**：触发消息是
# **追加指令**（含 `character.ADDITIVE_WORDS`、**且不是疑问句**）、而"原来会生效的
# 结论"**没定死一个人**（多人 ≥2 角色，或「换人还是追加不确定」＝会交 LLM 仲裁）时，
# 本次**不让位** —— 本条信号整体不采用，交回主题跟踪 / LLM 仲裁
# （见 `CharacterGuardHandler._additive_trigger_needs_tracking`）。用户场景
# `画爱莉和流萤一起` → `流萤这张不错` → 触发 `一起画吧`：1.0.30 会让位给 LLM 仲裁
# （可能两人），1.0.31 的让位却直接按前一条的单名拍板并 return，**压过**主题跟踪的
# 「换人还是追加不确定」与 LLM 仲裁 → 该画两人只画一人（回归用例 C115）。
# 疑问句照旧让位：`都试试？`（C112）走的就是那条路 —— `？` 是"征询/选择"，
# 不是"把上一位一起画"的指令。
#
# 它压过主题跟踪的**粘性与代词回指**，但压不过描述点名 —— 与既有的
# 「描述点名 > 主题跟踪 > …」总优先级一致（描述里写了角色时调用方早就放行了）。
# **日志**：命中时打 ``判定依据=用户点名(最新消息):爱莉希雅（优先于主题跟踪:流萤；
# 来源=…，原文=…）`` —— 括号里写的是"**原来会生效的那个结论**"（由
# ``CharacterGuardHandler._overtaken_conclusion`` 用**同一套代码**算出来，
# 只进日志、不参与判定），用户核对"为什么这次按爱莉希雅画"看的就是这一行。

#: 「用户最新一条消息」这条信号最多回看几条用户消息（触发前）。**就是 1 条**：
#: 再往前翻会把旧话题里的名字当成"用户刚说的"，那是粘性，不是点名。
#: **1.0.31 不动这个值**：触发消息没点名时跳过它的闸门①②、落到下一条，落到的
#: 正是这**唯一**一格（回归用例 C112）—— 候选表最多两格，所以不可能往更早找。
_LATEST_NAMED_HISTORY_LIMIT = 1

#: 触发前那条用户消息的**时间衰减下限**（与 ``monitor.min_confidence`` 同值同义）：
#: ``exp(-Δt / half_life)`` 低于它就不采用 —— 信号强度按 1.0 算（"点名"是最强信号）。
_LATEST_NAMED_MIN_DECAY = 0.5


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
        negative_arg: 负向提示词参数名（1.0.36，**只给分区覆盖的 ``negative_swap`` 用**）。
            留空 = 这个动作**没有负向参数**，负向词只写进 DEBUG 日志、不改画面：
            nai_artist 的 ``share_photo`` / ``share_drawing`` 就是这样（它的负向词在
            nai_artist 自己的 ``character.negative_tags`` 里，本插件不碰别的插件配置）；
            ``image_generator_plugin-neo`` 的 ``draw_image`` 是 ``negative_prompt``
    """

    plugin: str
    action: str
    prompt_arg: str
    append_mode: str = APPEND_MODE_NATURAL
    mode_arg: str = ""
    detect_args: tuple[str, ...] = ()
    characters_arg: str = ""
    negative_arg: str = ""


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
        # 1.0.36：这个动作**有**负向参数，分区覆盖的 negative_swap 才能真的写进去
        negative_arg="negative_prompt",
    ),
)


def parse_targets(raw_text: str) -> list[TargetSpec]:
    """解析目标 Action 表。

    每段格式：``插件名:action:动作名 | 提示词参数名 | 追加模式 | 模式参数 | 额外检测参数 | 角色槽位参数 | 负向参数名``。
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
        # 1.0.36：第 7 段是**负向提示词参数名**（可空）—— 只有动作本身有负向参数时
        # 才填，填了分区覆盖的 negative_swap 才会真的把负向词写进去
        negative_arg = parts[6] if len(parts) > 6 else ""
        targets.append(
            TargetSpec(
                plugin=plugin,
                action=action,
                prompt_arg=prompt_arg,
                append_mode=append_mode,
                mode_arg=mode_arg,
                detect_args=detect_args,
                characters_arg=characters_arg,
                negative_arg=negative_arg,
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
#: 穿着类型表（``[outfits] wear_types`` **合并**内置表，1.0.28）的解析缓存
_wear_types_cache: dict[str, dict[str, WearTypeMatch]] = {}

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


def _region_entries(config: CharPromptGuardConfig) -> list[region_api.RegionEntry]:
    """把 ``region_override.entries``（数组节，**独立条目列表**）归一成条目对象。

    1.0.36 的「分区覆盖」是**实验性功能、默认关闭**：只有
    ``region_override.enabled = true`` 时才会走到这里（见
    :meth:`CharacterGuardHandler._resolve_region_override` 的第一行）。
    列表本身由框架按 ``[[region_override.entries]]`` 解析成
    ``RegionEntrySection`` 实例；:func:`region.parse_region_entries` 对
    "节实例 / 字典 / 单条"三种形状都能接，条目为空时返回空列表 → 调用方直接
    **退回不分区**（不解析、不打日志）。
    """
    section = getattr(config, "region_override", None)
    raw = getattr(section, "entries", None) if section is not None else None
    return region_api.parse_region_entries(raw)


def _wear_types(config: CharPromptGuardConfig) -> dict[str, WearTypeMatch]:
    """内置 + 配置合并后的**穿着类型表**（1.0.28，带配置文本缓存）。

    ``outfits.wear_types`` 留空（默认）时就是内置表 :data:`outfit.WEAR_TYPE_KEYWORDS`
    本身；填了就与内置表**合并**（同名覆盖内置）。缓存键是配置文本，配置一改
    文本就变、自动失效 —— 与 :func:`_outfit_entries` 同一套做法。
    """
    section = getattr(config, "outfits", None)
    return _cached_parse(
        str(getattr(section, "wear_types", "") or ""),
        _wear_types_cache,
        wear_type_table,
    )


def _outfit_scope_key(text: str) -> str:
    """皮肤 / 穿着候选比较用的键：忽略大小写与**所有空白**。

    与 ``outfit._outfit_key`` 同一口径（``粉色 妖精`` 与 ``粉色妖精`` 等价）——
    这里只用它把"被选中的那个候选"从"被忽略的候选"里摘出去（1.0.28 的日志）。
    """
    return re.sub(r"\s+", "", (text or "").lower())


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


#: **1.0.373：多人语义词**（决定「双标签拦截」敢不敢删多余标签）。
#:
#: 命中任一 → 说明用户可能在要多人同框，**只告警、绝不删**（宁可留一个多余的标签，
#: 也不能把合法的第二个角色删掉）。口径 = ``ADDITIVE_WORDS``（还有 / 一起 / 俩 / 都 /
#: 加上 / 和她 / and / both）+ 人数与并列说法（两个 / 俩人 / 双人 / 她们 / 同框 /
#: 并肩 / 和 / 与 / +）。
_MULTI_HINT_WORDS: tuple[str, ...] = (
    *ADDITIVE_WORDS,
    "两个",
    "俩人",
    "双人",
    "她们",
    "他们",
    "她俩",
    "他俩",
    "同框",
    "并肩",
    "和",
    "与",
    "＋",
    "+",
    "多人",
)


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
        labels: 每个表外名字**由哪条句式抓到**（1.0.373）：``{"艾克斯": "句式=按X来"}``，
            走引号模式的是 ``"引号"``、多人枚举是 ``"句式=枚举列表"``
        latest_names: **最新一条用户消息**（``texts[0]``，即触发消息）里的表外名字
            （1.0.373）。它决定「先 B 后 A」那条硬规则要不要生效 —— 用户最新说的话
            里出现了角色表外的名字候选时，**禁止**拿主题粘性角色去补（宁可缺，不补错）
    """

    texts: list[str] = field(default_factory=list)
    scanned: int = 0
    names: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    latest_names: list[str] = field(default_factory=list)

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


@dataclass(frozen=True)
class LatestNamedEntry:
    """「用户**最新一条消息**里点名的角色表内角色」这条信号的结果（1.0.30）。

    Attributes:
        entry: 命中的角色表条目；没有信号时为 ``None``
        source: 名字**来自哪条消息**的说明（``触发消息`` / ``触发前第 1 条用户消息``）
        named: 那条消息里命中的**全部**角色表内角色（日志 / 诊断用：为什么没采用）
        text: 那条消息的正文（截断后进日志）
        basis: 判定依据文案（``用户点名(最新消息):爱莉希雅``）
        note: 没采用时的原因（``多个角色`` / ``自我指代`` / ``太旧`` …）
    """

    entry: CharacterEntry | None = None
    source: str = ""
    named: list[CharacterEntry] = field(default_factory=list)
    text: str = ""
    basis: str = ""
    note: str = ""

    @property
    def usable(self) -> bool:
        """是否拿到了可用的「用户最新点名」。"""
        return self.entry is not None

    def names_text(self, entries: list[CharacterEntry] | None = None) -> str:
        """角色名的可读列表（默认用 :attr:`named`）。"""
        items = self.named if entries is None else entries
        return "、".join(entry.name or entry.tag or "?" for entry in items) or "无"

    def label(self) -> str:
        """一行式诊断文案（DEBUG 日志用）。"""
        if not self.usable:
            return f"用户最新点名=无（{self.note or '最近一条消息里没有角色表内角色'}）"
        return f"{self.basis}（来源={self.source}，原文={self.text[:30]!r}）"


@dataclass(frozen=True)
class OvertakenConclusion:
    """「用户最新点名」这条信号**压过的**那个结论（1.0.30 起）。

    它由 :meth:`CharacterGuardHandler._overtaken_conclusion` 用**与正式判定链同一套
    代码**算出来（``monitor.enabled`` 时是 :func:`monitor.track_subject`，关掉时是
    :func:`character.resolve_naming_decision`），所以它同时也是「没有这条信号时本次
    会怎么判」的自证。

    Attributes:
        text: 进日志的文案，形如 ``主题跟踪:流萤（换人还是追加不确定）`` /
            ``主题跟踪:无(不补)`` / ``用户点名·粘性:爱莉希雅`` ——
            **1.0.30/1.0.31 起逐字不变**（README 与用例都按它断言）；
        unsettled: 这个结论**没有定死一个人** —— 多人（``≥2`` 角色）或
            「换人还是追加不确定」（会交 LLM 仲裁，也可能画成两人）。
            **1.0.32** 起它只参与**一处**前置判断
            （:meth:`CharacterGuardHandler._additive_trigger_needs_tracking`）；
            除此之外 ``text`` 仍然只进日志，不参与本次判定、也不改变既有输出。
    """

    text: str = ""
    unsettled: bool = False


# ── 皮肤/时装 与 泛指的穿着（1.0.22 / 1.0.28）─────────────────────────
#
# 需求原话：「当说『我想看看你的 xxxx（皮肤）』，可以搜索并加进去」——
# **角色那一侧一个字都没改**（描述点名 > 主题跟踪 > LLM 仲裁 > 不补），
# 这里只是多出「这次要穿什么」这一维：
#
#   ① 识别（``_scan_outfit_names`` + ``outfit.extract_outfit_names``）：
#      只在**用户消息**里找（触发消息 + 触发前最近 N 条用户消息，bot 消息跳过；
#      与表外点名 / 自我指代同一个焦点范围），**不扫画面描述**；
#   ② 解析（``_resolve_outfit``，1.0.28 的最终优先级）：
#      **① 皮肤表命中**（零模型调用）→ **② 穿着类型表命中**（内置 + 配置，
#      零模型调用）→ **③ 皮肤模型解析**（具体皮肤名，``auto_resolve``）
#      → **④ 穿着模型解析**（"看起来是穿着"的候选，穿着专用提示词）
#      → **⑤ ``infer_unnamed``**（用户没说名字时由模型推断一套，默认关）；
#   ③ 注入（``_apply_outfit``，在 ``_finalize`` 的最后一步）：
#      按目标的 natural / tags 模式把关键词**追加**在角色之后；
#      措辞分两种：皮肤 ``（皮肤/时装：“…”：…）``、穿着 ``（穿着：“…”：…）``。
#
# 为什么注入放在 ``_finalize`` 的最后（两条一致性检查**之后**）：皮肤 / 穿着关键词是
# 「多一层外观」，它们不该参与角色对账（那两条检查只认角色表内的字符串），
# 也不该被任何角色修正逻辑（fix 替换 / 删除）顺手删掉 —— 追加在最后既保证
# 「角色在前、衣服在后」，又保证「角色链路失败 / 修正时衣服那条链路一个字都不受影响」。

#: natural 模式追加的皮肤说明模板（皮肤名 + 关键词必须都在里面；中文描述可选）
OUTFIT_NATURAL_TEMPLATE = "（皮肤/时装：“{name}”：{keywords}）"
OUTFIT_NATURAL_TEMPLATE_WITH_DESC = "（皮肤/时装：“{name}”：{keywords}；{description}）"

#: natural 模式皮肤说明的**固定前缀**：用于"已经追加过就不再追加"的幂等判断（1.0.26）
#:
#: 只匹配模板开头（``（皮肤/时装：“``）而不是裸的 ``皮肤/时装：`` —— 后者会和用户自己在
#: 画面描述里写的同样五个字撞车，导致本张该补的皮肤被静默跳过（窄审计 1.0.26 发现）。
OUTFIT_NATURAL_MARKER = "（皮肤/时装：“"

#: 1.0.28：natural 模式追加的**穿着**说明模板（「泛指的穿着类型」那两条路）。
#:
#: 措辞与「皮肤/时装」**刻意分开**：用户说「我想看你的学生装」时产出的是"穿对应的
#: 衣服"（泛指的穿着），而不是"点名了一套皮肤"—— 日志与画面里都要能一眼看出区别。
WEAR_NATURAL_TEMPLATE = "（穿着：“{name}”：{keywords}）"

#: natural 模式穿着说明的**固定前缀**，与 :data:`OUTFIT_NATURAL_MARKER` 同一口径
#: （只匹配模板开头，避免和用户自己写的「穿着：」撞车）。
#:
#: **两个标记各管各的**（1.0.28 冲突矩阵第 7 条）：同类说明不重复追加
#: （多图轮里第 2、3 张已经带着第 1 张的说明 → 跳过），**不同类可以共存** ——
#: 画面里已经有皮肤说明时，穿着说明照样追加（反之亦然）；它们本来就不是同一个
#: 识别结果（一次识别只选一个候选），能同时出现只可能是别处已经写过一段。
WEAR_NATURAL_MARKER = "（穿着：“"


@dataclass(frozen=True)
class OutfitScope:
    """一次「皮肤/时装名扫描」的结果（1.0.22）。

    Attributes:
        texts: 参与扫描的用户消息正文（**触发消息在前**，其后由新到旧）；
            角色线索（给模型当提示、也是缓存键的一半）就是从这一份里取的
        name: 选中的皮肤名（空串表示这次没有皮肤意图）
        source: 这个皮肤名**来自哪条消息**（给模型当上下文线索，也让日志可核对）；
            **1.0.29 起穿着的归属线索也从这里取**（见
            :meth:`CharacterGuardHandler._wear_character_hint`）
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
        # 1.0.36：**实验性功能**「分区覆盖」（[region_override]，默认关闭）——
        # 与角色判定 / 一致性检查 / 幂等 / 归属校验 / 记忆库写回**全部解耦**：
        # 只拿上面**已经读到的**那批用户消息（outfit_scope.texts，触发消息在前）
        # 做一次纯字符串解析，得到「哪个分区用谁的特征词」，注入统一放在
        # _finalize 的最后一步（角色 → 皮肤/穿着 → 分区覆盖）。
        # 开关关闭时本调用**立刻返回**（不解析、不额外读上文、不调模型、不打日志）。
        region_swap = self._resolve_region_override(
            params=params,
            config=config,
            roster=roster,
            texts=outfit_scope.texts,
        )
        # 1.0.38：**实验性功能**「外观覆盖」（[appearance]，默认关闭）——
        # 与 1.0.36 的分区覆盖**完全分开**（不同的配置节、不同的解析），也与角色
        # 判定 / 一致性检查 / 幂等 / 归属校验 / 记忆库写回**全部解耦**：
        # 只拿上面**已经读到的**那批用户消息做一次纯字符串解析，得到「眼睛/发色
        # 换成了什么颜色」，注入统一放在 _finalize 的最后一步并在内容最前面写加权词。
        # 开关关闭时本调用**立刻返回**（不解析、不额外读上文、不调模型、不打日志）。
        appearance_override = self._resolve_appearance(
            params=params,
            config=config,
            texts=outfit_scope.texts,
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
            exclude=[
                *outfit_scope.names,
                # 1.0.38：本次外观覆盖命中的**颜色词**也豁免 —— 「发色换成银白」里的
                # 「银白」是颜色不是角色名，不该为它多问一次模型（与皮肤名同一套做法）
                *(appearance_override.exempt_words if appearance_override else ()),
            ],
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
                region=region_swap,
                appearance=appearance_override,
            )

        # ── 1.0.373：表外点名「先 B 后 A」 ──────────────────────────────
        # 用户**最新一条消息**里出现了"角色表外的名字候选"时，禁止用主题粘性角色去补。
        # B（解析成功）：上面入口② 已经补入并返回；走不到这里时交给 _finalize 的
        #   入口① 做替换，同样**不再补**粘性角色。
        # A（解析失败 / 异常 / 关键字全被丢弃）：**什么都不补** + 一条 WARNING ——
        #   宁可缺，绝不补错（实测事故：用户要的是角色X，插件补了上文的另一个人）。
        blocked = self._outside_naming_sticky_block(
            user_naming=user_naming, resolved=resolved
        )
        if blocked is not None:
            is_warning, blocked_note = blocked
            if is_warning:
                logger.warning(blocked_note)
            else:
                logger.info(blocked_note)
            return await self._finalize(
                params=params,
                args=args,
                spec=spec,
                original=raw_content,
                final_content=content,
                decided=[],
                roster=roster,
                config=config,
                basis="判定依据=表外点名（先 B 后 A）：本次不补角色",
                track=None,
                memory=None,
                naming_scope=user_naming,
                resolved=resolved,
                outfit=outfit,
                region=region_swap,
                appearance=appearance_override,
                sticky_blocked=True,
            )

        # 1.0.18：判定**不确定**且描述里没有角色表内角色时，先去外部记忆库
        # （Booku Memory）做一次「再确认」。命中「触发词 → 角色表内角色」的明确
        # 对应就直接用作判定依据（并写回记忆库），否则**静默降级**回 1.0.17 的
        # 原链路（记忆库模块自己不抛异常、失败只打 DEBUG）。
        #
        # 这里放在 _collect_candidates **之前**是有意的：命中时省掉一次 LLM 调用
        # （确定性记忆比模型猜测更可信），命中不了时 _collect_candidates 会照旧
        # 把命中片段的 title + snippet 作为参考材料喂给 LLM。
        # 1.0.26：口径与 present_keys 统一 —— 用同一个 `texts`（含 tags 模式的
        # `characters` 槽位），否则"角色标签只写在槽位里"时会被当成"画面里没有角色"，
        # 于是多跑一次记忆库再确认、并跳过 1.0.19 的「描述点名对账」。
        description_characters = [
            entry for entry in roster if character_in_texts(entry, texts)
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
                    region=region_swap,
                    appearance=appearance_override,
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
                region=region_swap,
                appearance=appearance_override,
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
                region=region_swap,
                appearance=appearance_override,
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
            region=region_swap,
            appearance=appearance_override,
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

        判定优先级（1.0.10 起，1.0.14 起第二级由主题跟踪承担；1.0.30 起在最前面
        插了一条"用户最新点名"）：**描述点名 > 用户最新点名（1.0.30）>
        主题跟踪（用户点名/粘性） > LLM 仲裁 > 不补**。

        - 描述点名：画面描述里已经写了角色表内角色 —— 由调用方直接放行/补标签，
          本函数只负责算出「判定集合」，供二次一致性检测对账；
        - **用户最新点名**（1.0.30，:meth:`_latest_named_entry`）：**触发消息**里
          明确写出、或（触发消息没写时）**触发前最近 1 条用户消息**里明确写出的
          角色表内角色（名字 / 别名 / 标签）。它压过主题跟踪的**粘性与代词回指** ——
          实测场景：`你：那画流萤吧` → `你：画爱莉的婚纱～`，1.0.29 判定仍是流萤，
          穿着冲突只能"不注入 + 告警"；用户要的是**按他最新说的角色画**。
          口径窄：那一条里**恰好 1 个**角色表内角色才采用（**多个 → 不采用**）、
          含自我指代 → 不采用（交给拆单 / 本尊身份）、触发前那条太旧 → 不采用；
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
        #
        # ①.5 用户最新点名（1.0.30）：**先算「用户最新一条消息里点名的角色表内角色」**
        # （``_latest_named_entry``，只读触发消息 / 触发前最近 1 条用户消息，
        # 见那一处的口径与闸门）。它压过下面主题跟踪的**粘性与代词回指**，
        # 也压过 ``monitor.enabled = false`` 时那条 1.0.13 的"用户点名·粘性"链路
        # （同一个口径）—— 但**压不过描述点名**（画面描述里写了角色时调用方早就
        # 放行了），也不改变下面任何一条既有分支的规则。
        latest_named = self._latest_named_entry(
            trigger_text=self._trigger_text(params),
            window=window,
            bot_ids=bot_ids,
            config=config,
            roster=roster,
        )
        if latest_named.usable:
            logger.debug(f"用户最新点名: {latest_named.label()}")
        track: SubjectTrack | None = None
        naming = NamingDecision()

        # 1.0.30：**用户最新一条消息里点名的角色表内角色 = 最高优先** ——
        # 它压过主题跟踪的**粘性与代词回指**（"画爱莉的婚纱～" 要按爱莉画，而不是按
        # 上一条 "那画流萤吧" 的粘性），也压过 1.0.13 那条"用户点名·粘性"链路 ——
        # 所以**放在两条链路之前**（monitor 开着关着同一个口径），但排在**描述点名**
        # 之后（画面描述里写了角色时调用方早就放行了，走不到这里）。
        # **四条闸门**（追加语义/多人、自我指代/疑问语气、只读清洗后的正文、保留
        # 时间衰减与描述优先）全部在 `_latest_named_entry` 里 —— 被闸门挡掉的情形
        # 一律退回下面原来的判定链，一个字都不变。
        # 命中时日志写明**被压过的那个结论**（`_overtaken_conclusion`）。
        if latest_named.usable and latest_named.entry is not None:
            entry = latest_named.entry
            basis = f"判定依据={latest_named.basis}"
            # 1.0.30：把「被这条信号压过的那个结论」一并写进同一行日志 —— 用户核对
            # 「为什么这次按爱莉希雅画、而不是上一条粘住的流萤」看的就是这一行
            # （README / docs 给的样例就是
            # ``判定依据=用户点名(最新消息):爱莉希雅（优先于主题跟踪:流萤）``）。
            # 括号里写的**一律是"原来会生效的那个结论"**（用**同一套代码**算出来的，
            # 见 :meth:`_overtaken_conclusion`），不是写死的角色名 —— 所以它同时也是
            # 「没有这条信号时本次会怎么判」的自证。
            #
            # **1.0.32**：这次调用提到"采用之前"（1.0.30/1.0.31 只在即将 return 时才算）
            # —— ``text`` 照旧只进日志（逐字不变），新加的结构位 ``unsettled`` 只用于
            # 一处前置判断：触发消息是**追加指令**、而它没定死一个人时，本次**不让位**，
            # 交回主题跟踪 / LLM 仲裁（审计 🟠2：否则"该画两人只画一人"）。
            overtaken = self._overtaken_conclusion(
                messages=messages,
                config=config,
                roster=roster,
                bot_ids=bot_ids,
                description_has_character=description_has_character,
            )
            if self._additive_trigger_needs_tracking(
                trigger_text=self._trigger_text(params),
                roster=roster,
                overtaken=overtaken,
            ):
                logger.debug(
                    "用户最新点名不采用（1.0.32）：触发消息是追加指令"
                    "（含追加语义词、不是疑问句），而原来会生效的结论是"
                    f"「{overtaken.text}」—— 不让位，交回主题跟踪 / LLM 仲裁"
                    "（避免「该画两人只画一人」）"
                )
            else:
                logger.debug(
                    f"{basis}（优先于{overtaken.text}；来源={latest_named.source}，"
                    f"原文={latest_named.text[:30]!r}）"
                )
                return CandidateResult(
                    candidates=[entry],
                    decided=[entry],
                    llm_multi=False,
                    mentioned=mentioned,
                    naming=NamingDecision(
                        kind="single",
                        characters=[entry],
                        basis=BASIS_LATEST_NAMED,
                        note=(
                            f"用户最新一条消息（{latest_named.source}）里点名，"
                            f"优先于{overtaken.text}"
                        ),
                    ),
                    basis=basis,
                    memory=memory_lookup,
                    track=track,
                )

        monitor = getattr(config, "monitor", None)
        monitor_on = bool(getattr(monitor, "enabled", False)) and bool(roster)

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

    # ── 「用户最新一条消息里点名的角色表内角色」（1.0.30）──────────────

    @staticmethod
    def _overtaken_conclusion(
        *,
        messages: list[dict[str, Any]],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        bot_ids: set[str] | frozenset[str],
        description_has_character: bool,
    ) -> OvertakenConclusion:
        """「用户最新点名」这条信号**压过的**那个结论（1.0.30；``text`` 只用于日志）。

        为什么要算它：这条信号是最高优先，但"它压过的到底是什么"必须能一眼看出来 ——
        README / ``docs/multi-character-design.md`` 给的日志样例就是
        ``判定依据=用户点名(最新消息):爱莉希雅（优先于主题跟踪:流萤）``。
        写死角色名不行：真实事故里被压过的是"上一条粘住的流萤"，换个场景可能是主题
        跟踪的另一个人、甚至"无(不补)"（主题不可用 / 置信度不足）。

        口径：**与正式判定链同一套代码** —— ``monitor.enabled`` 时是
        :func:`monitor.track_subject`（参数与 :meth:`_collect_candidates` 里那次
        **完全一致**），关掉时是 1.0.13 那条
        :func:`character.resolve_naming_decision`。所以括号里写出来的就是
        「**没有这条信号时，本次会怎么判**」：

        - ``主题跟踪:爱莉希雅`` / ``主题跟踪:伊甸、爱莉希雅``：主题跟踪的结论；
          带 ``（换人还是追加不确定）`` 时说明它原本会交 LLM 仲裁，而这条信号
          直接按用户最新点名拍板（1.0.30 要的正是"别让仲裁改掉用户刚说的名字"）；
        - ``主题跟踪:无(不补)``：主题不可用（本尊身份取不到 / 代词无先行词 /
          置信度不足 / 无信号）；
        - ``用户点名·粘性:…``：``monitor.enabled = false`` 时那条 1.0.13 链路。

        Returns:
            :class:`OvertakenConclusion` —— ``text`` 的文案与 1.0.30/1.0.31
            **逐字一致**（README 与用例都按它断言）；``unsettled`` 是 **1.0.32**
            新加的结构位（多人 / 换人还是追加不确定），**只**用于
            :meth:`_additive_trigger_needs_tracking` 那一处前置判断。

        **只读、只算、不写任何状态**：``text`` 只进日志，``unsettled`` 只参与上面
        那一处判断，**都不参与本次判定、也不改变既有输出**；任何异常都退成
        ``…:未知``（``unsettled`` 保持假 = 不做兜底，与 1.0.31 行为一致），
        绝不允许影响判定或拦住生图（这条信号本身就是"多一条证据"，
        它的诊断文案更加不能反过来把生图拦住）。
        """
        monitor = getattr(config, "monitor", None)
        if bool(getattr(monitor, "enabled", False)) and roster:
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
                    self_reference_enabled=(
                        bool(config.general.skip_on_self_reference)
                        and not description_has_character
                    ),
                    bot_ids=bot_ids,
                    resolve_identity=lambda: resolve_true_self_identity(config),
                )
            except Exception as e:  # noqa: BLE001 —— 只为日志，失败就写"未知"
                return OvertakenConclusion(
                    text=f"主题跟踪:未知（{type(e).__name__}）"
                )
            if track.usable:
                note = "（换人还是追加不确定）" if track.ambiguous else ""
                return OvertakenConclusion(
                    text=f"主题跟踪:{track.subject_names()}{note}",
                    # 多人（≥2 角色）或「换人还是追加不确定」（会交 LLM 仲裁、
                    # 也可能画成两人）都算"**没定死一个人**"（1.0.32 的兜底用）
                    unsettled=len(track.entries) >= 2 or track.ambiguous,
                )
            return OvertakenConclusion(text="主题跟踪:无(不补)")
        try:
            naming = resolve_naming_decision(
                messages,
                roster,
                stickiness_hours=float(config.multi.stickiness_hours),
                multi_window_minutes=float(config.multi.multi_window_minutes),
                bot_ids=bot_ids,
            )
        except Exception as e:  # noqa: BLE001 —— 同上
            return OvertakenConclusion(
                text=f"用户点名·粘性:未知（{type(e).__name__}）"
            )
        if naming.kind in {"single", "multi", "uncertain"} and naming.characters:
            return OvertakenConclusion(
                text=f"用户点名·粘性:{naming.names()}",
                # ``uncertain`` 就是 1.0.13 那条链路的「换人还是追加不确定」
                unsettled=naming.kind == "uncertain" or len(naming.characters) >= 2,
            )
        return OvertakenConclusion(text="用户点名·粘性:无(不补)")

    @staticmethod
    def _additive_trigger_needs_tracking(
        *,
        trigger_text: str,
        roster: list[CharacterEntry],
        overtaken: OvertakenConclusion,
    ) -> bool:
        """触发消息是**追加指令**、而"原来会生效的结论"没定死一个人 → **不让位**（1.0.32）。

        背景（审计 🟠2）：1.0.31 的前置判断让「触发消息没点名」的整条信号**让位**给
        "触发前最近 1 条用户消息"（见 :meth:`_latest_named_entry`）。触发消息自己含
        **追加语义词**时，这个让位会把"追加语义"一起丢掉 —— 用户场景::

            你：画爱莉和流萤一起
            你：流萤这张不错
            你：一起画吧          ← 触发这次生图

        主题跟踪的结论是**流萤**、状态是「换人还是追加不确定」（与上一条点名间隔
        < ``multi.multi_window_minutes``）：1.0.30 会让位给 LLM 仲裁（它读得到画面
        描述，可能判定为**两人**），1.0.31 的让位却直接按前一条的单名拍板
        （``判定依据=用户点名(最新消息):流萤``）并 ``return``，**压过**了主题跟踪的
        「换人还是追加不确定」与 LLM 仲裁 → **该画两人只画一人**。

        所以这里加一条**兜底**（只对"触发消息自己没点名"这条让位路径生效）：

        1. 触发消息含 :data:`character.ADDITIVE_WORDS`（追加指令：一起 / 都 / 还有 …）、
           **且不是疑问句** —— ``？`` / ``?`` 照旧让位：1.0.31 定下的 C112
           （``都试试？`` 要读的是"用户刚说的那个人"）走的就是那条路，
           ``？`` 是"征询 / 选择"，不是"把上一位一起画"的指令；
        2. :meth:`_overtaken_conclusion` 算出的"原来会生效的结论"**没定死一个人**
           （``unsettled``：多人 ≥2 角色，或「换人还是追加不确定」）——
           这时"前一条那个单名"很可能只是追加的一支，必须交回主题跟踪 / LLM 仲裁。

        两条都成立时才**不让位**（本条信号整体不采用），其余情形与 1.0.31 **一字不差**。
        它**不改变任何既有输出**：不采用时走的正是既有判定链（主题跟踪 → LLM 仲裁 →
        宁少勿多），命中时读到的 ``overtaken`` 依然只用于日志与这一处判断。

        Args:
            trigger_text: 触发本次生图的那条消息正文（:meth:`_trigger_text`）
            roster: 角色表（用来确认"触发消息自己没点名"）
            overtaken: 原来会生效的那个结论（调用方已算好）

        Returns:
            为真表示**本次不让位**：本条信号不采用，交回主题跟踪 / LLM 仲裁。
        """
        if not trigger_text:
            return False
        # 与 `_latest_named_entry` 同一口径：先剥掉 `[回复…]` / `[图片(…)]` 块
        text = strip_quoted_blocks(trigger_text)
        if not text:
            return False
        # 这条兜底只对"1.0.31 的前置让位"生效 —— 触发消息自己没点名、也没要本尊，
        # 正是那处前置判断的两个前提（触发消息点了名时走的是闸门①，轮不到这里）。
        if find_characters_in_text(text, roster) or find_self_reference_word(text):
            return False
        # 疑问句照旧让位（C112 的 `都试试？`：既含「都」又含「？」）
        if "？" in text or "?" in text:
            return False
        if not contains_any_word(text, ADDITIVE_WORDS):
            return False
        return overtaken.unsettled

    @staticmethod
    def _latest_named_entry(
        *,
        trigger_text: str,
        window: list[dict[str, Any]],
        bot_ids: set[str] | frozenset[str],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
    ) -> LatestNamedEntry:
        """用户**最新一条消息**里点名的角色表内角色（1.0.30）—— 角色判定的**最高优先**。

        需求场景（原话）::

            你：那画流萤吧
            你：画爱莉的婚纱～

        用户要的是「**按我最新说的角色来画**」：这两句里最新点名的是**爱莉希雅**，
        而既有的判定链会被"上一条粘住的流萤"按住（``_apply_outfit`` 的归属校验于是
        把婚纱拦下来：不注入 + 告警）。所以这里给出**一条最高优先的信号**，依据文案
        写明它来自"最新消息"，日志里同时写明它压过了哪个结论
        （``_overtaken_conclusion``）—— 穿着那一侧因此不再因归属冲突被拦
        （回归用例 C107/C108）。

        **只认用户说的名字，不从外观猜**：命中判定复用
        :func:`character.find_characters_in_text`（名字 / 别名 / 标准标签三种写法），
        与既有判定链完全同一口径。

        口径（窄到只解决这个问题 —— 宁可不采用，也不抢走既有链路）。
        **1.0.30 的四条闸门**（与模块顶部那段注释同一份口径）：

        1. **只看两条**：**触发消息**优先；它没点名才看**触发前最近 1 条用户消息**
           （``_LATEST_NAMED_HISTORY_LIMIT`` = 1，再往前是粘性、不是"刚说的"）；
        2. **闸门①：追加语义 / 多人 → 不采用**。那一条里命中**恰好 1 个**角色表内
           角色才采用 —— **≥2 个一律不采用**（「她俩一起」「爱莉和流萤」这种一单多人
           / 闲聊会误判）；含**追加语义词**（``character.ADDITIVE_WORDS``：还有 /
           一起 / 俩 / 都 / 加上 / 和她 / and / both）同样不采用 ——
           「还有伊甸，一起画」＝**本条 + 上一条点名**，要交回主题跟踪（或 1.0.13 的
           ``resolve_naming_decision``）的追加分支，否则上一条点名的角色会丢
           （回归用例 C35/C109）；
        3. **闸门②：自我指代 / 疑问语气 → 不采用**。含**自我指代**
           （``本尊`` / ``你自己`` …）→ 不采用：「本尊 + 角色名」要的是"本尊 vs 壳"，
           由既有的拆单（1.0.16）与本尊身份那条链路决定（C54/C98/C110）；
           含 ``？`` / ``?`` → 不采用：选择疑问句「是爱莉还是你？」不是点名，它要的是
           「换人还是追加不确定 → 交 LLM 仲裁」（C42/C110：LLM 说花火就是花火）；
        4. **闸门③：只读清洗后的正文**。触发消息先走
           :func:`character.strip_quoted_blocks`、历史消息走
           :func:`character.message_text`（本来就是同一个口径）—— ``[回复…]`` /
           ``[图片(…)]`` 这类中括号块里的名字是**别人说的**，不算用户点名
           （C43/C111）；
        5. **闸门④（1.0.30 之前就有的两件事，一个字都没改）**：触发前那条用户消息要
           **够新** —— 时间衰减 ``exp(-Δt / monitor.half_life_minutes)`` ≥
           ``handler._LATEST_NAMED_MIN_DECAY``（**数值与语义都对齐**
           ``monitor.min_confidence`` 的默认 0.5：按默认 45 分钟半衰期，约半小时内
           说的才算"刚说的"）—— 「画爱莉希雅」（3 小时前）+ 触发「嗯哼？」这种
           **旧粘性**必须照旧不补（C52/C37 守的）；触发消息**不需要**衰减闸门：
           它就是"这一句"；时间戳缺失的**历史**消息一律不采用（拿不到 Δt 就不敢说
           "刚说的"）。**描述点名优先**同样保留：画面描述里写了角色表内角色时调用方
           早就按那一档放行了，走不到这里。

        **1.0.31 的前置判断（目标 A，只加在"触发消息那一条"上）**：触发消息
        **一个角色表内角色都没有**时，**跳过它的闸门①②**，直接落到"触发前最近 1 条
        用户消息"上、并在那一条上判**全部**闸门。用户场景::

            你：那画流萤吧
            你：画爱莉的婚纱～
            你：都试试？   ← 触发消息：既没点名，又含「都」（追加词）与「？」（疑问）

        1.0.30 在触发消息那一条上先撞闸门①② → 整条信号让位 → 退回旧粘性
        （判定可能仍是流萤 + ``穿着跳过``）；1.0.31 起前一条的点名照旧读得到
        （回归用例 C112）。候选表最多两格（触发消息 + ``_LATEST_NAMED_HISTORY_LIMIT``
        = 1 条），所以这条前置判断**只会**看"触发前最近 1 条用户消息"、**不会**往更早
        找 —— 它不是"把 ``if not named`` 写成 ``continue``"那种全循环重排。
        **例外**：触发消息含**自我指代**时照旧让位（「你自己也来一张～」是"要本尊"的
        明确表达，必须交给拆单 / 本尊身份那条链路：C45/C110）。

        **1.0.32 给这条让位加一个兜底（审计 🟠2）**：触发消息是**追加指令**
        （含 ``character.ADDITIVE_WORDS``、**且不是疑问句**）、而
        :meth:`_overtaken_conclusion` 算出的"原来会生效的结论"**没定死一个人**
        （多人 ≥2 角色，或「换人还是追加不确定」＝会交 LLM 仲裁）时，调用方
        （:meth:`_collect_candidates`，见 :meth:`_additive_trigger_needs_tracking`）
        **不采用**本条信号 —— 让位会被"前一条的单名"按住，把可能的**两人**画成一人。
        用户场景 ``画爱莉和流萤一起`` → ``流萤这张不错`` → 触发 ``一起画吧``
        （回归用例 C115）。疑问句照旧让位（``都试试？``：C112 一字不改）。

        它压过主题跟踪的**粘性与代词回指**，也压过 ``monitor.enabled = false`` 时
        1.0.13 那条"用户点名·粘性"链路（所以调用方把它放在两条链路**之前**）；
        但**压不过描述点名** —— 画面描述里写了角色时调用方早就放行了，走不到这里。
        描述里写的是**别的**角色时，判定按这里的结论走，由 1.0.19 的期望集合对账把
        「用户最新说的人 ≠ 画面里的人」报出来（``warn`` 只报 / ``fix`` 替换）。

        本函数**不抛异常**（任何意外都返回"无信号"）：它只是多一条信号，
        绝不允许影响既有判定链或拦住生图。

        Args:
            trigger_text: 触发本次生图的那条消息正文（:meth:`_trigger_text`）
            window: 上文窗口（``_collect_context`` 的结果，末尾是触发消息）
            bot_ids: 本机 bot 的 ``sender_id`` 集合（bot 的复述不算用户点名）
            config: 插件配置（取 ``monitor.half_life_minutes``）
            roster: 角色表（空表直接返回"无信号"）

        Returns:
            :class:`LatestNamedEntry`；``entry is None`` 表示本次没有这条信号
        """
        if not roster:
            return LatestNamedEntry(note="角色表为空")
        monitor = getattr(config, "monitor", None)
        half_life = float(getattr(monitor, "half_life_minutes", 45.0) or 0.0)

        # 候选：触发消息（若有）在前，其后是触发前最近 N 条用户消息（新 → 旧）
        #
        # **闸门③（只读清洗后的正文）**：触发消息也要先剥掉 ``[回复…]`` / ``[图片(…)]``
        # 这类中括号块（:func:`character.strip_quoted_blocks` 就是
        # :func:`character.message_text` 内部那一步），历史消息本来就走
        # :func:`character.message_text` —— 两边同一口径。引用块 / 媒体占位里的文字
        # 是**别人说的话**或文件名，不是用户本次的点名：实测事故原文
        # ``[回复<…>：那你告诉我，是爱莉还是你？][图片(xxx)]`` 里的「爱莉」正是这样
        # 被当成"用户点名"的（回归用例 C43/C111）。
        candidates: list[tuple[dict[str, Any] | None, str, str]] = []
        clean_trigger = strip_quoted_blocks(trigger_text) if trigger_text else ""
        if clean_trigger:
            candidates.append((None, "触发消息", clean_trigger))
        # 触发消息是否被 append 进 window 由**原始** trigger_text 决定
        # （见 ``_collect_context``），所以这里判空仍用原始值。
        history = list(window[:-1]) if trigger_text else list(window)
        scanned = 0
        for message in order_messages_newest_first(history):
            if scanned >= _LATEST_NAMED_HISTORY_LIMIT:
                break
            if is_known_bot_message(message, bot_ids):
                continue
            scanned += 1
            candidates.append(
                (message, f"触发前第 {scanned} 条用户消息", message_text(message))
            )
        stale = False
        for message, source, text in candidates:
            if not text:
                continue
            # **1.0.31（目标 A）：触发消息自己没点名 → 跳过它的闸门①②，看下一条**。
            #
            # 闸门①②（追加语义 / 疑问语气）守的是"**用户自己点名的那一条**"：
            # 「还有伊甸，一起画」＝本条 + 上一条、「是爱莉还是你？」＝选择疑问句。
            # 而**触发消息根本没点名**时，这两条闸门只会把整条信号让位、退回旧粘性 ——
            # `那画流萤吧` → `画爱莉的婚纱～` → 触发 `都试试？`（既是「都」＝追加词、
            # 又是「？」＝疑问句）时，用户要的明明是"刚才说的那个人"，插件却读不到
            # 前一条的点名（回归用例 C112）。
            #
            # **为什么不会往更早找**：这里只对**触发消息这一条**（``message is None``）
            # 放行到下一格，而候选表最多两格（触发消息 + ``_LATEST_NAMED_HISTORY_LIMIT``
            # = **1** 条"触发前最近 1 条用户消息"，见上面的候选构造），所以 `continue`
            # 之后**只可能**落到那一条；那一条上照旧判**全部**闸门（与 1.0.30 一字不差），
            # 再往下没有候选、循环自然结束。**没有**把 `if not named` 写成 `continue`
            # 那种全循环重排。
            #
            # **例外（必须有）**：触发消息含**自我指代**时照旧让位 —— 「你自己也来一张～」
            # 配 1 分钟前那句「画爱莉希雅」时，这条信号必须让位给"本尊 vs 壳"那条链路
            # （C45/C110 断言"内容一字不改"）。自我指代是"要本尊"的明确表达，
            # 不是"接着刚才那句说"的回指；而且此时触发消息**不是**"没点名"，
            # 它是"点名了另一件事（本尊）"。
            #
            # **1.0.32 的兜底**：这里让位之后，调用方（`_collect_candidates` →
            # `_additive_trigger_needs_tracking`）还会看两件事 —— 触发消息是不是
            # **追加指令**（含追加语义词、不是疑问句）、以及"原来会生效的结论"是不是
            # **没定死一个人**（多人 / 换人还是追加不确定）。两条都成立时本次**不让位**
            # （本条信号整体不采用），交回主题跟踪 / LLM 仲裁：`画爱莉和流萤一起`
            # → `流萤这张不错` → 触发 `一起画吧` 若按前一条的单名拍板，
            # 就会**该画两人只画一人**（审计 🟠2，回归用例 C115）。
            if (
                message is None
                and not find_characters_in_text(text, roster)
                and not find_self_reference_word(text)
            ):
                # 1.0.31：触发消息**没点名**（且不含自我指代）时，不用它的问号/回指去否掉信号
                # —— 直接落到下一格（触发前最近 1 条用户消息），并在那一条上判全部闸门。
                # 只对触发消息这一格生效，候选表最多两格，因此不会往更早的消息找。
                continue
            if find_self_reference_word(text):
                # 闸门②（自我指代）：「本尊 + 角色名」＝一单多件，交给拆单 / 本尊
                # 身份那条链路（口径 ②）
                return LatestNamedEntry(
                    source=source,
                    text=text,
                    note=f"{source}里有自我指代，交给拆单/本尊身份判断",
                )
            if "？" in text or "?" in text:
                # 闸门②（疑问语气）：选择疑问句「是爱莉还是你？」不是用户点名 ——
                # 它要的是「换人还是追加不确定 → 交 LLM 仲裁」（回归用例 C42/C110：
                # LLM 说花火就是花火）。含问号就整条让位，宁可不采用。
                return LatestNamedEntry(
                    source=source,
                    text=text,
                    note=f"{source}是疑问句（含问号），交给 LLM 仲裁/原判定链",
                )
            if contains_any_word(text, ADDITIVE_WORDS):
                # 闸门①（追加语义）：「还有伊甸，一起画」＝**本条 + 上一条点名**，
                # 该由主题跟踪的追加分支（``monitor.enabled=false`` 时是
                # ``resolve_naming_decision`` 的追加分支）给出多人结论 ——
                # 被"最新一条里恰好 1 个角色"这条窄口径接管的话，上一条点名的角色
                # 就丢了（回归用例 C35/C109）。
                return LatestNamedEntry(
                    source=source,
                    text=text,
                    note=(
                        f"{source}含追加语义词（还有/一起/俩/都/加上/和她…），"
                        "交给「本条 + 上一条点名」那条链路"
                    ),
                )
            if message is not None and half_life > 0:
                stamp = message_time(message)
                if stamp is None:
                    stale = True
                    continue
                age_minutes = max(0.0, (time.time() - stamp) / 60.0)
                if math.exp(-age_minutes / half_life) < _LATEST_NAMED_MIN_DECAY:
                    stale = True
                    continue
            named = find_characters_in_text(text, roster)
            if not named:
                continue
            if len(named) > 1:
                # 闸门①（多人）：同一条消息里出现多个角色表内角色 → **不采用**
                # （「爱莉和流萤」既可能是一单多人、也可能只是闲聊里提到两个名字，
                #  拿它当"用户点名"会抢掉既有判定链的正确结论；回归用例 C34 守的是
                #  "同一条消息点两名 → 补两人"）
                return LatestNamedEntry(
                    source=source,
                    named=list(named),
                    text=text,
                    note=(
                        f"{source}里有 {len(named)} 个角色表内角色"
                        f"（{_entry_names(named)}），不采用"
                    ),
                )
            return LatestNamedEntry(
                entry=named[0],
                source=source,
                named=list(named),
                text=text,
                basis=f"{BASIS_LATEST_NAMED}:{named[0].name or named[0].tag or '?'}",
            )
        if stale:
            return LatestNamedEntry(
                note="触发前最近 1 条用户消息太旧（按时间衰减闸门不采用）"
            )
        return LatestNamedEntry(note="最近一条用户消息里没有角色表内角色")

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
        一致（归一后**完全相等**（1.0.24 起；早先的互为子串已废弃），忽略大小写 / 空白），被豁免的候选会记一条 DEBUG
        ``皮肤名豁免: …``，方便核对"是不是把真的表外角色名也豁免掉了"。
        没有皮肤名（``exclude`` 为空）时行为与 1.0.22 逐字一致。

        **1.0.38 追加一类豁免**：``exclude`` 里也可能带**外观覆盖命中的中文颜色词**
        （见 :meth:`_resolve_appearance` 的 ``exempt_words``）—— 「发色换成银白」里的
        「银白」是**颜色**不是角色名，同样不该为它多问一次模型（日志仍是上面那一行，
        括号里会说明"或由外观覆盖认定为颜色词"）。

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
            labels: dict[str, str] = {}
            latest_names: list[str] = []
            exempted: list[str] = []
            for index, text in enumerate(texts):
                found_labels: dict[str, str] = {}
                found = extract_outside_names(
                    text,
                    roster,
                    exclude=exclude or (),
                    dropped=exempted,
                    labels=found_labels,
                )
                for name in found:
                    if name not in names:
                        names.append(name)
                        # 记住它来自哪句话（1.0.21：解析成功后写回记忆库时的触发语）
                        sources[name] = text
                        labels[name] = found_labels.get(name, "")
                        # 1.0.373：``texts[0]`` 就是最新一条用户消息（触发消息在前），
                        # 它里面的候选决定「先 B 后 A」那条硬规则
                        if index == 0:
                            latest_names.append(name)
            if exempted:
                # 去重保序：同一套皮肤可能在好几条消息里被抓到
                unique = list(dict.fromkeys(exempted))
                logger.debug(
                    "皮肤名豁免: "
                    + "、".join(unique)
                    + "（已由皮肤识别认定为皮肤/时装名、或由外观覆盖认定为颜色词，"
                    "不算表外角色名）"
                )
            scope = UserNamingScope(
                texts=texts,
                scanned=scanned,
                names=names,
                sources=sources,
                labels=labels,
                latest_names=latest_names,
            )
            if names:
                # 1.0.373：先说清「每条名字是被哪条句式抓到的」（用户核对全靠这几行），
                # 再打一行汇总
                for name in names:
                    logger.debug(
                        f"表外点名({labels.get(name) or '未知来源'}): {name}"
                    )
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

        **1.0.28**：识别到的东西不再只有"皮肤名"，还包括**泛指的穿着词**
        （「学生装」「雨衣」…）。两者的识别口径**完全相同**（同一批候选），
        区别只在解析那一侧（``_resolve_outfit``）。这里同时保证两件事：

        - 被选中的名字与**被忽略的候选**都写在日志里（一图一套：取最新一条消息里
          最长的那个，与 1.0.22 的皮肤选择规则一致）；
        - 豁免集（``names``）**包含本次识别到的全部候选**，不只命中皮肤表的那些 ——
          穿着词（「雨衣」）与皮肤名一样不能被当成"角色表外的角色名"，
          否则角色解析会多问一次模型、``fix`` 还可能误删画面里的正确标签
          （1.0.23 的皮肤豁免同一条理由；关闭皮肤功能时仍按 1.0.26 的窄口径，
          只有"皮肤表里确实有"的名字才豁免）。
        """
        # 1.0.24：`enabled=false` 时**仍然**做一次纯字符串识别 —— 只为给"表外点名"供豁免集
        # （否则皮肤名又会被当成角色表外的角色名：多花一次模型调用，fix 模式下还会误删
        # 画面里正确的角色标签）。此时不解析、不注入、不调模型。
        exempt_only = not bool(getattr(getattr(config, "outfits", None), "enabled", False))
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
            # 1.0.28：本次识别到的**全部**候选（含被"一图一套"规则忽略的那些），
            # 只用于两件事：豁免集、以及日志里写明"被忽略的候选是哪些"。
            candidates: list[str] = []
            for text in focus:
                hits = extract_outfit_names(text, entries, roster)
                # 候选的原始集合（不带皮肤表）—— 命中皮肤表时 extract_outfit_names
                # **只**返回表里那条，被它压掉的其它候选要靠这一次调用才看得到。
                # 关闭皮肤功能（`exempt_only`）时用不到，就不做这一次多余扫描。
                extra = (
                    extract_outfit_names(text, [], roster)
                    if entries and not exempt_only
                    else hits
                )
                for candidate in [*hits, *extra]:
                    if (
                        candidate not in candidates
                        and not find_roster_identity(candidate, roster)
                    ):
                        candidates.append(candidate)
                if not hits:
                    continue
                if not name:
                    # 多个候选时取**最新一条消息里的、最长**的一个
                    name = max(hits, key=len)
                    source = text
                for hit in hits:
                    matched = find_outfit_entry(hit, entries)
                    if exempt_only and matched is None:
                        # 关闭皮肤功能时**只豁免"皮肤表里确实有"的名字**：否则句式抓到的
                        # 普通候选（例如用户点名的角色名）会被误豁免，把真角色名吞掉。
                        continue
                    # 1.0.24：豁免集**只收"不在角色表里"的名字** —— 否则皮肤表里恰好与某个
                    # 角色同名（或角色名是其子串）时，会把真角色名一起豁免掉。
                    if hit not in names and not find_roster_identity(hit, roster):
                        names.append(hit)
                    # 命中皮肤表的候选：把表里的**正式名**也记进来（用户写「粉色妖精」时抓到的
                    # 候选与表里的正式名并不相等，豁免与缓存都要按正式名走）
                    if (
                        matched is not None
                        and matched.name not in names
                        and not find_roster_identity(matched.name, roster)
                    ):
                        names.append(matched.name)
            if exempt_only:
                # 关闭皮肤功能：识别只为豁免，不产生"本次皮肤" → 不解析、不注入
                logger.debug(f"皮肤/时装=仅豁免识别（outfits.enabled=false）：{names}")
                return OutfitScope(texts=focus, names=names)
            if not name:
                logger.debug("皮肤/时装=跳过（用户最近的几条消息里没有皮肤/时装的说法）")
                return OutfitScope(texts=focus, names=names)
            # 1.0.28：豁免集补上"没被选中"的候选（穿着词常常落在这里）——
            # 它们同样是穿着/皮肤名，不该被表外点名那条链路当成角色名。
            for candidate in candidates:
                if candidate not in names and not find_roster_identity(candidate, roster):
                    names.append(candidate)
            ignored = [
                candidate
                for candidate in candidates
                if _outfit_scope_key(candidate) != _outfit_scope_key(name)
            ]
            if ignored:
                logger.debug(
                    f"皮肤/穿着候选: 本次只处理「{name}」"
                    f"（一图一套：取最新一条消息里最长的候选；"
                    f"被忽略的候选：{'、'.join(ignored[:4])}）"
                )
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
        """识别并解析本次的皮肤/时装 / 穿着（1.0.22；1.0.23 与 1.0.28 各加一档）。

        流程（1.0.28 的**最终优先级**，注释与 README 都按这一份写）：

        1. ``outfits.enabled = false`` → **立刻返回**（不解析皮肤表、不读上文、不调模型）；
        2. 解析皮肤表（``outfits.entries``，带配置文本缓存）；``scope`` 由调用方
           （``execute``）先跑 :meth:`_scan_outfit_names` 备好时直接复用，
           没给（本方法被单独调用）才自己扫一次；
        3. **① 皮肤表命中** → 直接用表里的关键词 / 中文描述，**零模型调用**；表里的
           那一行没写关键词时按「表未命中」处理（继续往下走，用户少填一列也能用）；
        4. **② 穿着类型命中**（1.0.28；1.0.29 收紧了匹配口径）→ 内置表
           :data:`outfit.WEAR_TYPE_KEYWORDS` 与 ``outfits.wear_types`` 合并后的表里
           **候选本身是表中词、或以它结尾**才算命中（``学生装`` / ``居家服`` /
           ``泳衣`` / ``穿学生装``；``学生会`` / ``学生时代`` 这类"只是包含"的
           不再命中 —— 见 :func:`outfit.find_wear_type`），直接用表里的关键词，
           **同样零模型调用** —— 需求原话：「判断不是皮肤，转成'穿对应的衣服'……
           要能**看到注入了什么样的衣服**」；
        5. **③/④ 模型解析**（``outfits.auto_resolve``，默认开）：候选**看起来是
           穿着**（:func:`outfit.wear_like_candidate`：含 ``衣/服/装/裙/…`` 或句式
           「穿 X」「换上 X」）→ **④ 穿着模型解析**（:meth:`_resolve_wear_with_cache`
           → :func:`outfit.resolve_wear_via_llm`，穿着专用提示词）；否则 →
           **③ 皮肤模型解析**（:meth:`_resolve_outfit_with_cache` → 皮肤提示词）。
           两条路互斥（同一次只走一条），校验口径完全相同；``auto_resolve = false``
           时只留一条 DEBUG ``…解析=跳过（…）``；
        6. **⑤** 这次**一个候选都没有**（``scope.name`` 为空）时，若
           ``outfits.infer_unnamed`` 为真 → 交给 :meth:`_infer_unnamed_outfit`
           去认「<角色>的那一套皮肤」并让模型推断（默认关）。
           **与穿着互斥**：已经识别到穿着词 / 皮肤名（``scope.name`` 非空）时
           绝不进这一步。

        **1.0.29 的两处增量**（判定链与优先级一个字都没改）：

        - **② 的匹配口径收紧**（见第 4 条与 :func:`outfit.find_wear_type`）：
          "候选**包含**表中词"→"候选**是**表中词或**以它结尾**"，于是
          「学生会」「学生时代」这类不含穿着意图的话不再被零模型调用地注入；
        - **穿着也带归属线索**：② / ④ 产出的 ``ResolvedOutfit.character`` 由
          :meth:`_wear_character_hint` 填（"说这句话的那条消息"里**唯一**提到的
          角色表内角色），:meth:`_apply_outfit` 的归属校验据此对穿着同样生效。

        **1.0.30 的两处增量**（优先级次序与注入位置同样一个字都没改）：

        - **② 的候选不再被虚词截断**：句式抽取去掉虚词「会」
          （:data:`outfit._OUTFIT_SENTENCE_STOP_CHARS`），「我想看学生会～」的候选
          是完整的 ``学生会`` → 这里既不命中 ② 也不命中 ① ，落到 ③/④ 模型兜底；
          1.0.29 时它是被截断的 ``学生``（**相等命中** ② → 零模型调用注入校服）；
        - **② 的同义写法补全**：``学生服`` / ``校服外套`` / ``婚纱照`` 等 15 条
          常见写法各自成为内置表里的**键**（见 :data:`outfit.WEAR_TYPE_KEYWORDS`），
          它们因此又是零模型调用命中，而且日志里写的是用户的原话。

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
            # 用户没点名具体的皮肤名 / 穿着 —— 1.0.23：只有显式开了 infer_unnamed 才继续，
            # 默认关时这里与 1.0.22 完全一致（返回 None，什么都不注入）。
            # 1.0.28：上面已经识别到穿着词时走不到这里（穿着与推断互斥）。
            return await self._infer_unnamed_outfit(
                config=config, roster=roster, texts=scope.texts
            )
        character = self._outfit_character_hint(scope, entries, roster)
        # 1.0.29：穿着那两条路（② / ④）也带**归属线索** —— 归属校验（_apply_outfit）
        # 才对穿着同样生效。线索只从"说这句话的那条消息"里取，且**唯一**时才填，
        # 见 :meth:`_wear_character_hint` 的 docstring。
        wear_character = self._wear_character_hint(scope, roster)
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
        # ② 穿着类型（1.0.28）：泛指的「学生 / 居家 / 泳衣 / 睡衣」…… 直接用表里的
        # 关键词，**一次模型都不问** —— 这一批词是"确定答案"，不该交给模型去猜。
        # 放在皮肤表**之后**是有意的：具体皮肤名（例如表里的「学生会长制服」）
        # 永远压过类型词，不会因为候选里含「学生」两个字就被拆成"学生装"。
        wear_type = find_wear_type(scope.name, _wear_types(config))
        if wear_type is not None:
            outfit = ResolvedOutfit(
                name=wear_type.word,
                # 1.0.29：穿着类型表没有归属列，归属线索由"说这句话的那条消息"给出
                # （唯一才填）—— _apply_outfit 的归属校验据此拦"婚纱贴到流萤身上"。
                character=wear_character,
                keywords=list(wear_type.keywords),
                source=OUTFIT_SOURCE_TABLE,
                confidence=1.0,
                raw_name=scope.name,
                wear=OUTFIT_WEAR_TYPE,
                wear_source=wear_type.source,
            )
            logger.debug(outfit.log_line())
            return outfit
        wear_like = wear_like_candidate(scope.name, scope.source)
        if not bool(getattr(section, "auto_resolve", True)):
            logger.debug(
                f"{'穿着' if wear_like else '皮肤'}解析=跳过（{scope.name} 不在"
                f"{'皮肤表 / 穿着类型表' if wear_like else '皮肤表'}里，"
                "且 outfits.auto_resolve=false）"
            )
            return None
        if wear_like:
            # ④ 穿着模型解析（1.0.28）：候选"看起来是穿着"（「雨衣」「白色长裙」
            # 「换上战甲」…），但内置表与皮肤表都没有 → 交给穿着专用提示词。
            resolved = await self._resolve_wear_with_cache(
                config=config, name=scope.name, context_text=scope.source
            )
            # 1.0.29：归属线索**在缓存之外**补 —— 穿着缓存的键只有原词（不含角色，
            # 通用服装词与角色无关），把归属烤进缓存会让"同一个词、换个角色"的
            # 下一次解析用上上一次的归属；命中缓存时同样要按**本次**这句话重判。
            if resolved is not None and wear_character:
                resolved = replace(resolved, character=wear_character)
            return resolved
        # ③ 皮肤模型解析（1.0.22 起的原路，一个字没改）
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

    @staticmethod
    def _wear_character_hint(scope: OutfitScope, roster: list[CharacterEntry]) -> str:
        """穿着候选的**归属线索**（1.0.29）——"说这句话的那条消息"里提到的角色表内角色。

        与 :meth:`_outfit_character_hint` 的区别只有**线索范围**：

        - 皮肤那条路取整个焦点范围（``scope.texts`` = 触发消息 + 触发前最近几条
          用户消息）：用户常常在**上一条**消息里说清楚这套皮肤属于谁；
        - 穿着这条路只取**抓到候选的那条消息**（``scope.source``）：要拦的场景是
          「爱莉穿婚纱的样子」这种"同一句话里既有归属又有穿着"；跨消息取线索会把
          C98「本尊看看，然后穿着爱莉来看看～」（本张主题是**花火**，爱莉只是那句
          话里的旧名字）也算成归属，从而**误杀**本来正常的注入。

        **只在能唯一确定时填**：这条消息里的角色表内角色恰好一个才返回它的正式名；
        多个（「爱莉和流萤穿婚纱」）或一个都没有（「我想看你的雨衣」）一律返回空串
        —— 留空 = 照常注入（见 :meth:`_apply_outfit`），**宁可不拦，也不误杀**。

        线索本身不注入任何东西，只用来回答"这件衣服属于谁"。
        """
        text = (scope.source or "").strip()
        if not text or not roster:
            return ""
        names: list[str] = []
        for entry in find_characters_in_text(text, roster):
            name = (entry.name or entry.tag or "").strip()
            if name and name not in names:
                names.append(name)
        return names[0] if len(names) == 1 else ""

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

    # ── 穿着解析（LLM，1.0.28 第 ④ 档）──────────────────────────────

    async def _resolve_wear_with_cache(
        self,
        *,
        config: CharPromptGuardConfig,
        name: str,
        context_text: str = "",
    ) -> ResolvedOutfit | None:
        """把「穿着类候选」交给模型转成服装关键词（1.0.28 第 ④ 档，带调用方缓存）。

        场景：用户说的既不在皮肤表（①）、也不在内置 / 配置的穿着类型表（②），
        但**看起来是穿着**（「雨衣」「白色长裙」「换上战甲」…，判据见
        :func:`outfit.wear_like_candidate`）—— 需求要求这种词也自动找出关键词并
        注入，而不是留给"黑箱"。解析用**穿着专用提示词**
        （:func:`outfit.resolve_wear_via_llm`，明确要求"不是穿着就 unknown=true"），
        与 ③ 皮肤模型解析互斥。

        缓存与皮肤解析**同一套分层**（存储与 TTL 常量在 :mod:`outfit`，读写在调用方）：

        - 缓存键 ``<原词>|__wear__``（:func:`outfit.outfit_wear_cache_key`；
          穿着关键词是通用服装词，与角色无关，所以键里不带角色）；
        - 成功 24 小时、失败负缓存 10 分钟；命中 → ``穿着解析(缓存)``，**不再问模型**；
        - 未命中 → 问一次模型：成功 INFO ``穿着解析(模型): <原词> → <关键词>``
          + ``建议加入穿着类型表：…``（只打日志，下次就能零模型调用）；
          失败 DEBUG ``穿着解析(LLM)失败：<原因>``（异常则 WARNING）。

        **不写回记忆库**（冲突矩阵第 8 条）：记忆库那条通道写的是"某一套皮肤属于谁"
        （标题「皮肤指向：…」），穿着关键词是**通用服装词**（「雨衣 → raincoat」），
        记下来既没有复用价值，也不该被当成"用户确认过的身份事实"。
        ``outfits.write_back`` 因此对这条链路无效（皮肤那条路照旧）。

        **1.0.29**：归属线索（``ResolvedOutfit.character``）**不在这里填、也不进缓存**
        —— 缓存键只有原词（通用服装词与角色无关），由调用方
        （:meth:`_resolve_outfit`）在拿到结果后按"**本次**这句话"补上，命中缓存时
        同样按本次重判（见 :meth:`_wear_character_hint`）。

        **本函数不抛异常**（异常一律转成失败结果并写负缓存）；调用方拿到 ``None``
        或 ``ok`` 为假的结果时**什么都不注入**。
        """
        cache_key = outfit_wear_cache_key(name)
        cached = outfit_cache_get(name, key=cache_key)
        if cached is not None:
            logger.info(cached.log_line())
            if cached.ok:
                self._log_wear_suggestion(cached)
                return cached
            return None

        try:
            result = await resolve_wear_via_llm(
                name,
                translate_model=str(config.general.translate_model or ""),
                timeout=float(config.general.llm_timeout or 0.0),
                context_text=context_text,
            )
        except Exception as e:  # noqa: BLE001 —— 解析是「多一层外观」，不许拦住生图
            logger.warning(f"穿着解析(LLM)失败：{type(e).__name__}: {e}（穿着={name!r}）")
            # 异常也要进负缓存：同一个词不必为每一张图都再等一次超时
            outfit_cache_put(
                name,
                ResolvedOutfit(
                    name=name,
                    reason=f"调用失败({type(e).__name__}: {e})",
                    raw_name=name,
                    wear=OUTFIT_WEAR_MODEL,
                ),
                key=cache_key,
            )
            return None

        outfit_cache_put(name, result, key=cache_key)
        if not result.ok:
            logger.debug(f"{result.log_line()}（穿着={name!r}）")
            return None
        logger.info(result.log_line())
        self._log_wear_suggestion(result)
        return result

    @staticmethod
    def _log_wear_suggestion(outfit: ResolvedOutfit) -> None:
        """打一条「建议加入穿着类型表：…」的 INFO（**只打日志，绝不自动改配置文件**）。

        形如 ``建议加入穿着类型表：雨衣 | raincoat`` —— 直接粘进
        ``outfits.wear_types`` 就再也不用问模型了（这正好呼应需求里的
        "要能**看到注入了什么样的衣服**"）。
        """
        if not outfit.ok:
            return
        word = (outfit.raw_name or outfit.name or "").strip()
        if word:
            logger.info(f"建议加入穿着类型表：{word} | {outfit.keywords_text()}")

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
        # 1.0.26：**写回只在真正会用到解析结果时发生**。
        # `warn`（默认）只会把结论写进日志与"建议加入角色表"，却会把**未被验证过的
        # 模型结论**写进记忆库 —— 既白花一次写入，又可能被后续"再确认"当成明确证据。
        # 因此默认档不写回；`fix` 才会用解析结果去替换/补入，此时写回才是有意义的记忆。
        mode = str(getattr(config.general, "consistency_check", "") or "").strip().lower()
        if mode == "fix":
            await self._write_resolved_to_memory(
                config=config,
                resolved=result,
                trigger=self._resolve_trigger(name, naming_scope),
            )
        else:
            logger.debug(
                f"角色解析结果不入记忆库（consistency_check={mode or 'warn'}："
                "本次仅用于日志/建议加入角色表）"
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
        3. ~~用户最近说过的话里没有这个解析结果~~ —— **1.0.373 起取消这条**：
           表外名字本来就是从用户消息里抓出来的，这条等于让入口②永远不生效
           （实测事故：用户说「按角色X来」、画面里又没有角色标签 → 什么都没补，
           反而让判定链补了上文粘性的另一个人）。"已经写过就不必再补" 由
           :meth:`_resolved_presentation` 里的 ``content_contains_tag`` 兜住。

        画面里已经有角色标签时不在这里动：那种情况属于**替换**（入口①，
        见 :meth:`_apply_outside_naming_check`），两条入口不会互相打架。
        """
        if resolved is None or not resolved.resolved_tag():
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

    @staticmethod
    def _outside_naming_sticky_block(
        *,
        user_naming: UserNamingScope | None,
        resolved: CharacterEntry | None,
    ) -> tuple[bool, str] | None:
        """「先 B 后 A」：最新消息里有表外名字候选时，是否**禁止**用粘性角色补（1.0.373）。

        修的是这条实测事故（1.0.38 已部署）：用户说的是「要角色X那层壳上来、眼睛换成
        本尊的紫红。摆一张紫红瞳的角色X。」—— 角色X**不在角色表**，旧句式一条都
        抓不到；画面内容里又没有角色标签，于是判定链按**主题粘性**补了上文那个角色，
        用户要的人**一个字都没进提示词**。

        用户选定的策略是「**先 B 后 A**」：

        - **B**：能解析出表外角色（``resolved`` 非空）→ 用解析出的标准标签（走既有
          入口② / 入口①），并且**不得**再补粘性角色；
        - **A**：解析不出来 / 出错 → **什么都不补**（宁可缺，绝不补错），只留一条
          WARNING 建议把该角色加进 ``[roster]``。

        只看**最新一条用户消息**（``latest_names``，即 ``texts[0]``）里的候选：
        更早几条消息里的表外点名由 1.0.20 那条独立检查负责，不在这里扩大弃权范围
        （否则"半小时前顺口提过一个表外角色"会让本次也不补，太激进）。

        Returns:
            ``None`` = 不适用（最新消息里没有表外名字候选）；
            ``(是否 WARNING, 日志文案)`` = 适用 —— 调用方**不补任何角色**，
            直接以「画面原样」进 :meth:`_finalize`（``sticky_blocked=True``）。
        """
        if user_naming is None or not user_naming.latest_names:
            return None
        name = user_naming.latest_names[0]
        if resolved is not None:
            return (
                False,
                (
                    f"用户最新消息里的「{name}」不在角色表，但已解析为"
                    f"「{resolved.name or resolved.tag or '?'}」→ 按解析结果处理，"
                    "本次**不补**主题粘性角色（宁可缺，不补错）"
                ),
            )
        return (
            True,
            (
                f"用户说的「{name}」不在角色表且未能解析，本次不补角色"
                "（宁可缺，不补错）；建议把它加入 [roster]"
            ),
        )

    @staticmethod
    def _recent_multi_hint(
        texts: list[str],
        roster: list[CharacterEntry],
        naming_scope: UserNamingScope | None = None,
    ) -> str:
        """用户最近消息里的**多人语义**（1.0.373；空串 = 没有）。

        「双标签拦截」要靠它决定敢不敢删：消息里只要出现 ``一起 / 两个 / 她们 /
        同框 / 并肩 / 和 / 与 …``，或者点名了 ≥2 个角色（表内 + 表外），就说明这
        很可能是**合法的双人图**，此时**只能告警**，绝不能把第二个角色删掉。

        Returns:
            命中的说法（``一起、两个``）或 ``点名了 2 个角色``；都没有返回 ``""``。
        """
        # 只看**最新一条**用户消息：``一起 / 两个 / 和`` 出现在更早的对话里并不代表
        # 这次要多人（1.0.30 的实测场景「先画 A」→「画 B 的婚纱～」就是**换人**
        # 而不是追加，把上文也算进来会永远判成"多人语义"）。
        latest = next((text for text in texts if text), "")
        if not latest:
            return ""
        lowered = latest.lower()
        hits = [
            word
            for word in _MULTI_HINT_WORDS
            if word and (word in latest or word.lower() in lowered)
        ]
        named = {
            entry_key(entry) for entry in roster if character_in_texts(entry, [latest])
        }
        outside = list(getattr(naming_scope, "latest_names", []) or [])
        total = len(named) + len(outside)
        if total >= 2:
            hits.append(f"点名了 {total} 个角色")
        return "、".join(dict.fromkeys(hits))

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

        # 1.0.38：**多角色画面只补不替换** —— 与 _apply_consistency_check 同一个
        # 守卫（`及任何 fix 分支`）：画面里已有 ≥2 个角色表内角色、且用户自己点了
        # ≥2 个表外名字时，画面里任何角色标签都不许被替换或删除。期望那一侧在这里
        # 就是**用户点名的表外角色**（没有 roster 条目可补，所以只写日志、不补标签）。
        guarded = self._multi_character_guard(
            content=content,
            spec=spec,
            texts=texts,
            roster=roster,
            expected_names=outside,
            decision_multi=len(naming_scope.names) >= 2,
            expected=None,
        )
        if guarded is not None:
            return guarded

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
        region: region_api.RegionSwap | None = None,
        appearance: appearance_api.AppearanceOverride | None = None,
        sticky_blocked: bool = False,
    ) -> tuple[EventDecision, dict[str, Any]]:
        """统一的收尾：跑二次一致性检测 → 追加皮肤 → 追加分区覆盖 → 前置外观覆盖 → 回写 args。

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

        **1.0.28**：``outfit`` 也可能是**泛指的穿着**（穿着类型表命中 / 穿着模型解析，
        见 :meth:`_resolve_outfit`）—— 注入位置与顺序同样不变，只是措辞换成
        ``（穿着：“…”：…）``、幂等标记换成 :data:`WEAR_NATURAL_MARKER`。
        「穿着不参与角色对账 / 不被 fix 碰」这一点由下面这个**调用顺序**直接保证。

        **1.0.36**：``region`` 是本次解析出的**分区覆盖**（实验性功能，默认关闭，
        见 :meth:`_resolve_region_override` 与 :mod:`region`），由
        :meth:`_apply_region_override` 追加在**更后面**的一步 —— 角色 → 皮肤/穿着
        → 分区覆盖。它同样**不参与**角色判定、两条一致性检查、幂等与归属校验：
        分区特征词不是角色表里的字符串，``fix`` 替换 / 删除碰不到它；
        负向词只写进目标动作**自己**的负向参数（:attr:`TargetSpec.negative_arg`），
        没有负向参数时只打一条 DEBUG。

        **1.0.38**：``appearance`` 是本次解析出的**外观覆盖**（实验性功能，默认关闭，
        见 :meth:`_resolve_appearance` 与 :mod:`appearance`），由
        :meth:`_apply_appearance` 在**最后一步**调用 —— 注入顺序是
        角色 → 皮肤/穿着 → 分区覆盖 → 外观覆盖，与 1.0.36 的 ``[region_override]``
        **完全分开**（不同的配置节、不同的解析、不同的注入位置）。它同样**不参与**
        角色判定、两条一致性检查、幂等与归属校验：颜色词不是角色表里的字符串，
        ``fix`` 替换 / 删除碰不到它。它写进内容的是**最前面**的加权词
        （需求原话「外观覆盖放最前」），并在 ``tag_weight < 1.0`` 时把画面里
        **第一处**角色标准标签降权（只改这一处，其它标签一个字不动）。

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
            # 1.0.373：``sticky_blocked`` = 本次「先 B 后 A」弃权了 —— 不能用粘性
            # 角色去替换 / 删除画面里的标签（只在日志里说明）
            allow_sticky=not sticky_blocked,
            recent_texts=list(naming_scope.texts) if naming_scope else [],
            naming_scope=naming_scope,
            # 注入**前**的画面：双标签拦截靠它判断"本来就多"，而不是被本次注入带偏
            initial_content=original,
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
        checked = self._apply_region_override(
            content=checked,
            spec=spec,
            args=args,
            region=region,
        )
        # 1.0.38：外观覆盖排在**最后一步**（角色 → 皮肤/穿着 → 分区覆盖 → 外观覆盖），
        # 但它写进内容的是**最前面**的加权词 —— 需求原话就是「外观覆盖放最前」。
        checked = self._apply_appearance(
            content=checked,
            spec=spec,
            appearance=appearance,
            roster=roster,
        )
        if checked != original:
            args[spec.prompt_arg] = checked
            params["args"] = args
        return EventDecision.SUCCESS, params

    def _multi_character_guard(
        self,
        *,
        content: str,
        spec: TargetSpec,
        texts: list[str],
        roster: list[CharacterEntry],
        expected_names: str,
        decision_multi: bool,
        expected: ExpectationSet | None = None,
    ) -> str | None:
        """「画面里已有 ≥2 个角色表内角色」时**不做任何替换 / 删除**（1.0.38）。

        修的是这条实测事故：用户要「花火 + 流萤」两人，``fix`` 却把画面里那个
        **合法的第二个角色标签**当成冲突替换掉了 —— 日志

            一致性检查=冲突已修正（fix）：把画面里的「花火」替换为「流萤」

        结果是画面只剩一个角色标签、两套特征混写，两个人被揉成一个。根因是
        「期望集合恰好 1 人 + 冲突角色恰好 1 个」这个"可唯一定位"判据在**多角色
        画面**里也成立：期望集合只认出了其中一个角色，画面里另一个合法角色就
        成了"多余的那一个"。

        **两个条件同时成立才拦**（``decision_multi`` 是这道守卫的关键）：

        1. 画面里（``texts``）已出现 **≥2 个角色表内角色**；
        2. 本次**判定本身就认为画面里该有 ≥2 个角色**（``decision_multi``）——
           判定集合 ≥2 人，或用户自己点了 ≥2 个表外名字。

        为什么必须有第 2 条（1.0.38 定稿时实测出来的）：**"1 人判定 + 双人画面"**
        有另一种合法情形 —— 主模型在描述里写错了角色标签（把流萤的标签写进了要画
        爱莉希雅的画面），此时 ``fix`` 把它替换成正确的角色**正是**要的行为
        （用例 C108 锁着：判定=爱莉希雅、画面里只有流萤那个错标签 → 必须替换）。
        这两种情形在"画面里有几个角色"上完全一样，能区分的只有**判定自己**：
        判定只点名 1 个人时，多出来的那个是错的；判定要 ≥2 个人时，多出来的是
        队友，一个都不能少。少了第 2 条，C108 这种「写错标签」就再也修不了。

        拦下之后：

        - 记一条 ``一致性检查=多角色画面，不做替换（画面：…；期望：…）``
          —— ``expected_names`` 由调用方给（期望集合的角色名，或用户点名的表外角色）；
        - 期望集合里**有人缺失**（``expected`` 给了且画面里找不到）→ **补上**那个
          角色的标准标签（**只补不删**）；一个都不缺 → 只 WARN，内容一段不动。

        不在多人画面里（<2 个）时返回 ``None``，调用方照旧走原来的替换 / 删除。

        Returns:
            ``None`` = 不适用（照旧走原逻辑）；否则是**最终**内容（已含补充标签）。
        """
        if not decision_multi:
            # 「1 人判定 + 多角色画面」：这是**另一种**合法情形（主模型把角色标签
            # 写错了，例如要画爱莉希雅却写了流萤的标签），照旧走下面的替换 / 删除。
            # 留一条 DEBUG 让用户能区分「守卫为什么没拦」（用例 C127 钉着它）。
            picture = [entry for entry in roster if character_in_texts(entry, texts)]
            if len(picture) >= 2:
                logger.debug(
                    "一致性检查=多角色画面但判定只点名 1 人，照旧按替换处理"
                    f"（画面：{_entry_names(picture)}；期望：{expected_names}）"
                )
            return None
        present = [entry for entry in roster if character_in_texts(entry, texts)]
        if len(present) < 2:
            return None
        logger.warning(
            "一致性检查=多角色画面，不做替换"
            f"（画面：{_entry_names(present)}；期望：{expected_names}）"
        )
        if not expected:
            return content
        present_keys = {entry_key(entry) for entry in present}
        missing = [
            entry
            for entry in expected.entries
            if entry_key(entry) not in present_keys
        ]
        if not missing:
            return content
        tags = [
            entry.resolved_tag()
            for entry in missing
            if entry.resolved_tag()
            and not content_contains_tag(content, entry.resolved_tag())
        ]
        if not tags:
            return content
        logger.info(
            f"一致性检查=多角色画面补充：{_entry_names(missing)} → {', '.join(tags)}"
        )
        return self._complete(spec, content, tags)

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
        allow_sticky: bool = True,
        recent_texts: list[str] | None = None,
        naming_scope: UserNamingScope | None = None,
        initial_content: str | None = None,
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
        # 1.0.373：画面里**实际**出现了几个角色表内角色（双标签拦截要用）
        picture = [entry for entry in roster if character_in_texts(entry, texts)]
        # …但"本来就多"要看**注入前**：本次刚补进去的判定角色不能算成多余的那一个
        # （否则「画面只有 1 个错标签 → 替换成判定角色」这条 1.0.19 的既有修法
        # 会退化成"删掉错标签"、判定角色的标准标签反而进不去，用例 C108 就是这么红的）
        picture_before = (
            [
                entry
                for entry in roster
                if character_in_texts(entry, [initial_content])
            ]
            if initial_content
            else list(picture)
        )
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
            # 1.0.373：warn 模式对「画面里 ≥2 个角色标签、判定只要 1 个」只报不改
            if len(expected.entries) == 1 and len(picture_before) >= 2:
                logger.warning(
                    f"发现 {len(picture_before)} 个角色标签但判定只有 1 个"
                    f"（warn 只报不改）：画面={_entry_names(picture_before)}，"
                    f"期望={expected.names()}{suffix}"
                )
            return content

        # 1.0.38：**多角色画面只补不替换** —— 拦在所有 fix 动作之前
        guarded = self._multi_character_guard(
            content=content,
            spec=spec,
            texts=texts,
            roster=roster,
            expected_names=expected.names(),
            decision_multi=len(decided) >= 2 or len(expected.entries) >= 2,
            expected=expected,
        )
        if guarded is not None:
            return guarded

        # 1.0.373：**本次弃权（先 B 后 A）时不要用粘性角色去替换 / 删除**。
        # 用户最新消息里点名了角色表外的角色、而那个角色没能解析出来 —— 此时
        # 任何"按主题粘性换人"的动作都是在赌另一个角色，宁可什么都不做。
        if not allow_sticky:
            logger.warning(
                "一致性检查=冲突但本次不用主题粘性角色替换"
                f"（用户最新消息里点名了角色表外的角色、且没能解析）：{detail}{suffix}"
            )
            return content

        # ── 1.0.373：④ 双角色标签拦截 ──────────────────────────────────
        # 实测事故的另一半：画面里同时挂着两个角色标准标签（nait_artist 自己加了一个
        # 错的），判定只要 1 个。旧逻辑是"把多余的**替换**成判定角色"，可判定角色
        # 本来就在画面里 → 结果多出一个**一模一样**的标签。
        # 现在：判定角色已在画面里时**删除多余**，而不是替换。
        # **三条闸门同时成立才删**（另一半是"只告警"），防误删合法双人图：
        #   ① 判定本身只要 1 人（``len(expected.entries) == 1`` 且 ``len(decided) < 2``）；
        #   ② 画面里角色标签 ≥2 个；
        #   ③ 用户最近消息里**没有多人语义**，且只点名了 1 个角色。
        # 任一不成立 → 只 WARN（多角色守卫 / 旧替换路径照旧），绝不删。
        if len(expected.entries) == 1 and len(picture_before) >= 2:
            multi_hint = self._recent_multi_hint(
                recent_texts or [], roster, naming_scope
            )
            if multi_hint:
                logger.warning(
                    f"发现 {len(picture_before)} 个角色标签但判定只有 1 个，"
                    f"且消息里有多人语义（{multi_hint}） → 不删（避免误删合法角色）；"
                    f"画面={_entry_names(picture_before)}，期望={expected.names()}"
                    f"{suffix}"
                )
                return content
            right = expected.entries[0]
            if character_in_texts(right, texts):
                cleaned = content
                hits_total = 0
                removed_names: list[str] = []
                for entry in list(extras):
                    cleaned, hits = delete_character_reference(cleaned, entry)
                    if hits:
                        hits_total += hits
                        removed_names.append(entry.name or entry.tag or "?")
                if hits_total > 0:
                    logger.warning(
                        f"一致性检查=冲突已修正（fix）：发现 {len(picture_before)} 个角色标签，"
                        f"保留判定角色「{right.name or right.tag or '?'}」，"
                        f"删除多余：「{'、'.join(removed_names)}」"
                        f"（删除 {hits_total} 处）{suffix}"
                    )
                    return cleaned

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

        **1.0.28**：泛指的穿着用另一套措辞 —— ``（穿着：“学生装”：school uniform,
        pleated skirt）``（穿着那两条路没有中文描述，模板里也就不带描述位）。
        """
        keywords = outfit.keywords_text()
        if outfit.is_wear:
            return WEAR_NATURAL_TEMPLATE.format(name=outfit.name, keywords=keywords)
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
        """把皮肤/时装 / 穿着**追加**在角色之后（1.0.22；1.0.28 起两种措辞）。

        这是 1.0.22 新增的**唯一**改动点：角色那一侧一个字都没改，皮肤只是
        「多追加一层」。规则：

        - ``natural``（nai_artist）：另起一行追加
          ``（皮肤/时装：“<皮肤名>”：<关键词>）``，位置在角色中文说明之后；
          **1.0.28 的穿着**写 ``（穿着：“<穿着词>”：<关键词>）``（措辞刻意分开，
          不与"皮肤/时装"混淆）；
        - ``tags``（image_generator_plugin-neo）：把解析出的关键词追加进**同一个**
          参数字符串（``content_description``）末尾（``, pink elf dress, fairy wings``），
          与角色标签同处一个槽位；已经在提示词里的关键词不重复追加；
        - **没有解析出角色**（``decided`` 为空）时照样注入关键词，只打一条 DEBUG
          说明「无角色，仅注入…关键词」—— 用户说「穿这套」时外观描述里通常
          已经写了角色（此时角色在画面里、只是判定链没给结论）；
        - 注入失败 / 解析失败 / 开关关掉时 ``outfit`` 为 ``None``，本方法原样返回，
          **绝不影响角色注入，也绝不拦住生图**。

        **1.0.28 的三条冲突裁决**（冲突矩阵第 6、7 条）：

        - **归属校验对穿着同样生效**（1.0.26 皮肤 / **1.0.29 起穿着**）：
          ``outfit.character`` 非空（皮肤表第 2 列、皮肤 / 穿着的角色线索）而本次
          判定的角色里没有它 → **不注入** + WARNING
          ``穿着跳过：穿着「婚纱」属于「爱莉希雅」，但本张判定角色是「流萤」``；
          穿着类型表本身没有归属列，它的线索由 :meth:`_wear_character_hint` 从
          "说这句话的那条消息"里取（唯一才填），皮肤那条路沿用
          :meth:`_outfit_character_hint`。**判不出角色**（``decided`` 为空）时
          保持原行为：照常注入，只打一条 DEBUG。
        - 穿着关键词**只走** :func:`outfit.validate_outfit_keyword`（含 1.0.26 那条
          "拒收 ``\\(…\\)`` 形状"），所以它**不可能**被角色对账 / ``fix`` 当成角色
          标签 —— 而本方法本来就跑在两条一致性检查**之后**，``fix`` 替换 / 删除
          角色标签时也碰不到这里追加的内容。
        - **幂等按"同类"判**：皮肤说明看 :data:`OUTFIT_NATURAL_MARKER`、穿着说明看
          :data:`WEAR_NATURAL_MARKER`，同类不重复追加（多图轮的第 2、3 张），
          **不同类可以共存**（画面里已经有皮肤说明时，穿着说明照样追加）。
        """
        if outfit is None or not outfit.ok:
            return content
        kind = "穿着" if outfit.is_wear else "皮肤"
        # 1.0.26：**归属校验** —— 皮肤表第 2 列（或推断结果）写明"这套衣服属于谁"时，
        # 若本次判定的角色里没有它，就**不注入**（否则会出现"流萤的招牌衣服被贴到爱莉
        # 身上"这种错图，而插件既不报也不拦）。判不出角色（decided 为空）时保持原行为：
        # 照样注入，只打一条 DEBUG。
        # 1.0.29：穿着那两条路的 character 由 handler._wear_character_hint 填
        # （"说这句话的那条消息"里唯一提到的角色表内角色）→ 这一关对穿着**同样生效**：
        # 「爱莉穿婚纱的样子」+ 本张判定=流萤 → 不注入并告警，与皮肤那条路口径一致。
        if outfit.character and decided:
            wanted = str(outfit.character).strip().casefold()
            owners = {
                str(value).strip().casefold()
                for entry in decided
                for value in (entry.name, entry.tag, *entry.aliases)
                if value
            }
            if wanted and wanted not in owners:
                logger.warning(
                    f"{kind}跳过：{kind}「{outfit.name}」属于「{outfit.character}」，"
                    f"但本张判定角色是「{'、'.join(entry.name for entry in decided)}」"
                )
                return content
        owner = next((entry.name for entry in decided if entry.name), "")
        if owner:
            logger.debug(f"{kind}注入: {outfit.name}（搭配角色={owner}）")
        else:
            logger.debug(f"{kind}注入: {outfit.name}（无角色，仅注入{kind}关键词）")

        if spec.append_mode == APPEND_MODE_TAGS:
            missing = [
                keyword
                for keyword in outfit.keywords
                if not content_contains_tag(content, keyword)
            ]
            if not missing:
                logger.debug(f"{kind}关键词已在提示词里，不重复追加: {outfit.name}")
                return content
            joined = ", ".join(missing)
            stripped = content.rstrip().rstrip(",").rstrip()
            merged = f"{stripped}, {joined}" if stripped else joined
            logger.info(
                f"已追加{'穿着' if outfit.is_wear else '皮肤/时装'}"
                f"（{spec.action} / tags）: {outfit.name} → {joined}"
            )
            return merged

        clause = self._outfit_clause(outfit)
        # 1.0.26：**幂等** —— 单次多图（「画三张」）时第 2、3 张的入参 content 里已经带着
        # 第 1 张追加过的说明，而它们由**同一轮**触发、_scan_outfit_names 对它们拿不到
        # 候选，所以整段 clause 比对不上、会被重复追加（提示词膨胀 + 关键词权重叠加）。
        # 这里改成"只要已有**同类**说明段就不再追加"（皮肤看 `（皮肤/时装：“`、
        # 穿着看 `（穿着：“`）—— 1.0.28 起两类**各自**判：不同类可以共存
        # （画面里已有皮肤说明时，穿着说明照样追加，反之亦然）。
        marker = WEAR_NATURAL_MARKER if outfit.is_wear else OUTFIT_NATURAL_MARKER
        if clause in content or marker in content:
            return content
        merged = f"{content.rstrip()}\n{clause}" if content.strip() else clause
        logger.info(
            f"已追加{'穿着' if outfit.is_wear else '皮肤/时装'}"
            f"（{spec.action} / natural）: "
            f"{outfit.name} → {outfit.keywords_text()}"
        )
        return merged

    # ── 分区覆盖（实验性功能，默认关闭，1.0.36）─────────────────────
    #
    # ⚠️ 实验性功能：打开后生图效果可能不理想 —— 分区拼接只是"按词重画"，模型可能
    # 把两个角色的特征混在一起（串味）、比例异常或细节崩坏；默认关闭，
    # 普通使用完全不受影响。
    #
    # 与 1.0.22 的皮肤 / 1.0.28 的穿着同一套分层：**识别与拼装在 :mod:`region`**
    # （纯字符串，不调模型），**注入在 _finalize 的最后一步**（角色 → 皮肤/穿着 →
    # 分区覆盖），**开关关闭时零动作**（不解析、不额外读上文、不调模型、不打日志）。
    #
    # 三条解耦保证（需求硬约束）：
    #   ① 角色判定：本方法**不参与**任何判定链，只读已经算好的 `roster`；
    #   ② 一致性检查 / 幂等 / 归属校验：分区覆盖跑在它们**之后**，特征词也不是角色表
    #      里的字符串，fix 的替换 / 删除碰不到它；
    #   ③ 记忆库写回：完全不碰（不写回、也不查询）。

    def _resolve_region_override(
        self,
        *,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        roster: list[CharacterEntry],
        texts: list[str] | None = None,
    ) -> region_api.RegionSwap | None:
        """解析本次的**分区覆盖**（实验性功能，默认关闭）；不需要/不可用时返回 ``None``。

        流程：

        1. ``region_override.enabled = false``（默认）→ **立刻返回**：不读配置、
           不解析字符串、不额外读上文、不调模型、**一条日志都不打**（零动作）；
        2. 归一 ``region_override.entries``（**独立条目列表**，不是 ``[roster]`` /
           不是 ``[outfits]``）：整份为空 → 返回 ``None``（同样是零动作）；
        3. 在 ``texts``（**触发消息在前、其后由新到旧**，就是本次生图已经读到的
           那批用户消息）里找**第一条**能解析出分区归属的消息 ——
           :func:`region.parse_region_overrides` 的**三重条件**（分区词 + 角色名 +
           归属句式）缺一不认，所以「B的头饰很好看」「头疼」「画B」都不会命中；
        4. :func:`region.build_region_swap` 按 ``regions`` 的顺序（默认
           head → body → outfit → pose → background）拼正向段，并算负向词：
           ``negative_swap`` = 被换掉的角色在**同一分区**的原特征，
           ``anti_mix`` = 多角色时的防串味词；
        5. 条目里找不到该角色 / 条目没填这个分区 → :meth:`RegionSwap.skip_line`
           打一条 **DEBUG** 并返回 ``None``：**退回不分区**，其它注入一个字都不改。

        ``texts`` 为空时只拿**触发消息**（:meth:`_trigger_text`，不读库）兜底 ——
        且与 1.0.30 闸门③ 同一个正文口径（:func:`character.strip_quoted_blocks`）：
        ``[回复…]`` / ``[图片(…)]`` 这种中括号块里的文字是**别人说的**或媒体占位，
        不算用户本次的说法。
        任何异常只记 DEBUG 并返回 ``None`` —— 这是"多一层外观"，绝不拦住生图。
        """
        section = getattr(config, "region_override", None)
        if section is None or not bool(getattr(section, "enabled", False)):
            # 默认关闭 → 零动作（连配置都不解析、日志都不打）
            return None
        try:
            entries = _region_entries(config)
            if not entries:
                # 没配条目就没有"各分区特征词"可用 → 什么都不做（也不打日志）
                return None
            focus = [text for text in (texts or []) if text]
            if not focus:
                trigger_text = strip_quoted_blocks(self._trigger_text(params))
                if trigger_text:
                    focus = [trigger_text]
            if not focus:
                return None
            assignments: dict[str, str] = {}
            source = ""
            for text in focus:
                found = region_api.parse_region_overrides(text, roster, entries)
                if found:
                    assignments = found
                    source = text
                    break
            if not assignments:
                return None
            swap = region_api.build_region_swap(
                assignments,
                entries,
                roster=roster,
                regions=region_api.parse_regions(
                    getattr(section, "regions", "")
                ),
                negative_swap=bool(getattr(section, "negative_swap", True)),
                anti_mix=bool(getattr(section, "anti_mix", True)),
                stylized_swap=bool(getattr(section, "stylized_swap", False)),
                source=source,
            )
            if not swap.ok:
                logger.debug(swap.skip_line())
                return None
            # 把"到底拼了谁、每个分区用了哪些词、负向算了什么"一次写清楚：
            # 这是实验性功能，用户核对效果全靠这一行
            logger.info(swap.log_line())
            return swap
        except Exception as e:  # noqa: BLE001 —— 多一层外观，失败即静默退回
            logger.debug(f"分区覆盖解析失败，本次跳过（{type(e).__name__}: {e}）")
            return None

    def _apply_region_override(
        self,
        *,
        content: str,
        spec: TargetSpec,
        args: dict[str, Any],
        region: region_api.RegionSwap | None,
    ) -> str:
        """把分区覆盖**追加**在角色与皮肤/穿着之后（1.0.36，实验性功能）。

        - ``natural``（nai_artist）：另起一行追加
          ``（分区覆盖：头=B：silver hair, …；身子=A：…）`` ——
          **分区顺序就是 regions 的顺序，head 在最前**；
        - ``tags``（image_generator_plugin-neo）：把各分区特征词按同一顺序追加进
          同一个提示词参数串末尾（``, silver hair, …, slender, …``）；
        - **幂等**：natural 看 :data:`region.REGION_NATURAL_MARKER`
          （``（分区覆盖：``），tags 看"关键词是否都已在提示词里" —— 多图轮的第 2、3 张
          不重复追加；
        - **负向**：只有 ``region.negatives`` 非空时才动
          （见 :meth:`_write_region_negative`），且只写目标动作**自己**的负向参数；
        - ``region`` 为 ``None`` / 没有正向段时**原样返回**，不留任何痕迹。
        """
        if region is None or not region.ok:
            return content
        if region.negatives:
            self._write_region_negative(spec=spec, args=args, region=region)
        if spec.append_mode == APPEND_MODE_TAGS:
            missing = [
                keyword
                for keyword in region.positive_keywords()
                if not content_contains_tag(content, keyword)
            ]
            if not missing:
                logger.debug("分区覆盖关键词已在提示词里，不重复追加")
                return content
            joined = ", ".join(missing)
            stripped = content.rstrip().rstrip(",").rstrip()
            merged = f"{stripped}, {joined}" if stripped else joined
            logger.info(
                f"已追加分区覆盖（{spec.action} / tags）: "
                f"{region.summary()} → {joined}"
            )
            return merged

        clause = region.natural_clause()
        if clause in content or region_api.REGION_NATURAL_MARKER in content:
            # 1.0.36 幂等：同一轮多图时第 2、3 张的入参里已经带着这段说明
            return content
        merged = f"{content.rstrip()}\n{clause}" if content.strip() else clause
        logger.info(
            f"已追加分区覆盖（{spec.action} / natural）: {region.summary()}"
        )
        return merged

    @staticmethod
    def _write_region_negative(
        *,
        spec: TargetSpec,
        args: dict[str, Any],
        region: region_api.RegionSwap,
    ) -> None:
        """把分区覆盖的负向词写进目标动作**自己的**负向参数（1.0.36）。

        **能写到哪**（确定口径，别指望它凭空出现负向约束）：

        - 动作**本身有负向参数**（:attr:`TargetSpec.negative_arg` 非空）→ 追加进那个
          参数（逗号分隔、已在里面的词不重复）。目前内置目标里只有
          ``image_generator_plugin-neo`` 的 ``draw_image``（``negative_prompt``）符合；
        - 动作**没有**负向参数（``targets.negative_arg`` 留空，nai_artist 的
          ``share_photo`` / ``share_drawing`` / ``share_visual`` 都是这种）→
          **一个字都不写**，只打一条 DEBUG 把"本该写进去的负向词"列出来。
          原因：nai_artist 的负向词来自它自己的配置 ``character.negative_tags``，
          运行时的 Action 参数里没有承载它的位置；本插件**不碰别的插件配置**
          （既不读也不写它的 config、更不落盘），所以对 nai_artist 这条只能是
          日志级信息。用户真要生效，就把这些词自己填进 nai_artist 的
          ``character.negative_tags``。
        """
        terms = list(region.negatives)
        if not terms:
            return
        arg_name = str(getattr(spec, "negative_arg", "") or "").strip()
        if not arg_name:
            logger.debug(
                "分区覆盖：负向词无槽位可写（目标动作没有负向参数，"
                "nai_artist 的负向词在它自己的 character.negative_tags 里）："
                + ", ".join(terms)
            )
            return
        existing = args.get(arg_name)
        text = existing if isinstance(existing, str) else ""
        missing = [term for term in terms if not content_contains_tag(text, term)]
        if not missing:
            logger.debug(f"分区覆盖负向词已在 {arg_name} 里，不重复追加")
            return
        stripped = text.rstrip().rstrip(",").rstrip()
        joined = ", ".join(missing)
        args[arg_name] = f"{stripped}, {joined}" if stripped else joined
        logger.info(
            f"已写入分区覆盖负向词（{spec.action} / {arg_name}）: {joined}"
        )

    # ── 外观覆盖（1.0.38，**实验性功能**，默认关闭）─────────────────────────
    #
    # 需求原话：「脸是薇塔的，眼睛换成本尊的紫红」→ 提示词里确实有 purple eyes，
    # 但出图仍是蓝眼。原因是角色标签 ``firefly (honkai: star rail)`` 自带「蓝眼」
    # 先验，权重高过描述里那句裸词；而 nai_artist **没有负向参数**
    # （``share_photo`` / ``share_drawing`` / ``share_visual`` 都只有 ``content``），
    # 它的负向词在它自己的 ``character.negative_tags`` 里，本插件不碰别的插件配置
    # —— 所以「塞负向」这条路在 nai_artist 上**无效**。
    #
    # 本功能因此只做两件事：
    #   ① 把颜色词**加权**放到提示词**最前面**（``1.4::purple-red eyes::``，语法见
    #      :func:`appearance.format_weight`）；
    #   ② ``tag_weight < 1.0`` 时把画面里**第一处**角色标准标签**降权**
    #      （``0.8::firefly \(honkai: star rail\)::``）——「只改这一处，其它标签不动」。
    #
    # 与 1.0.36 的 ``[region_override]`` **完全分开**（需求硬约束）：不同的配置节
    # （``[appearance]`` vs ``[region_override]``）、不同的解析、不同的注入位置。
    # 与 1.0.22 皮肤 / 1.0.28 穿着同一套分层：**识别与拼装在** :mod:`appearance`
    # （纯字符串，不调模型、不读上文、不写记忆库），**注入在 _finalize 的最后一步**
    # （角色 → 皮肤/穿着 → 分区覆盖 → 外观覆盖），**开关关闭时零动作**
    # （不解析、不额外读上文、不调模型、不打日志）。
    #
    # 三条解耦保证：
    #   ① 判定链 / 一致性检查 / 幂等 / 归属校验 / 记忆库写回：一个字都不碰；
    #   ② 注入顺序虽然排在最后，但写进内容的加权词在**最前面**（需求原话
    #      「外观覆盖放最前」）；
    #   ③ 降权只作用于**角色标准标签**那一个字面，特征词与描述原文不动。

    def _resolve_appearance(
        self,
        *,
        params: dict[str, Any],
        config: CharPromptGuardConfig,
        texts: list[str] | None = None,
    ) -> appearance_api.AppearanceOverride | None:
        """解析本次的**外观覆盖**（实验性功能，默认关闭）；不需要/不可用时返回 ``None``。

        流程：

        1. ``appearance.enabled = false``（默认）→ **立刻返回**：不读配置、不解析
           字符串、不额外读上文、不调模型、**一条日志都不打**（零动作）；
        2. 归一 ``apply_to`` / ``eye_weight`` / ``tag_weight`` / ``weight_syntax``
           （非法值一律退回默认，绝不因为一项写错就整条失效）；
        3. 在 ``texts``（**触发消息在前、其后由新到旧**，就是本次生图已经读到的那批
           用户消息）里找**第一条**能解析出外观归属的消息 ——
           :func:`appearance.parse_appearance_overrides` 的**三重条件**
           （外观词 + 更换动词 + 颜色词，紧邻出现）缺一不认，所以「眼睛很好看」
           「我想看看你的眼睛」「瞳色是蓝的」都**不会**命中；
        4. :func:`appearance.build_appearance_override` 按 ``apply_to`` 的顺序
           （默认 eyes → hair）拼加权词；
        5. 识别不到 → 静默返回 ``None``（**零动作**，其它注入一个字都不改）。

        ``texts`` 为空时只拿**触发消息**（:meth:`_trigger_text`，不读库）兜底，
        且与 1.0.30 闸门③ 同一个正文口径（:func:`character.strip_quoted_blocks`）。
        任何异常只记 DEBUG 并返回 ``None`` —— 这是"多一层外观"，绝不拦住生图。
        """
        section = getattr(config, "appearance", None)
        if section is None or not bool(getattr(section, "enabled", False)):
            # 默认关闭 → 零动作（连配置都不解析、日志都不打）
            return None
        try:
            apply_to = appearance_api.parse_apply_to(
                getattr(section, "apply_to", None)
            )
            weight = appearance_api.parse_weight(
                getattr(section, "eye_weight", None),
                appearance_api.DEFAULT_OVERRIDE_WEIGHT,
            )
            tag_weight = appearance_api.parse_weight(
                getattr(section, "tag_weight", None),
                appearance_api.DEFAULT_TAG_WEIGHT,
            )
            syntax = appearance_api.parse_weight_syntax(
                getattr(section, "weight_syntax", None)
            )
            focus = [text for text in (texts or []) if text]
            if not focus:
                trigger_text = strip_quoted_blocks(self._trigger_text(params))
                if trigger_text:
                    focus = [trigger_text]
            if not focus:
                return None
            assignments: dict[str, str] = {}
            source = ""
            for text in focus:
                found = appearance_api.parse_appearance_overrides(text, apply_to)
                if found:
                    assignments = found
                    source = text
                    break
            if not assignments:
                return None
            override = appearance_api.build_appearance_override(
                assignments,
                apply_to=apply_to,
                weight=weight,
                tag_weight=tag_weight,
                weight_syntax=syntax,
                source=source,
                # 命中的中文颜色词（「银白」这类）要能豁免表外点名链路：
                # 它是颜色、不是角色名，不该为它多问一次模型（与 1.0.23 皮肤名同一套做法）
                exempt_words=appearance_api.find_appearance_colors(source, apply_to),
            )
            if not override.ok:
                return None
            # 把"到底换了哪儿、权重多少、要不要降权角色标签"一次写清楚：
            # 实验性功能，用户核对效果全靠这一行
            logger.info(override.log_line())
            return override
        except Exception as e:  # noqa: BLE001 —— 多一层外观，失败即静默退回
            logger.debug(f"外观覆盖解析失败，本次跳过（{type(e).__name__}: {e}）")
            return None

    def _apply_appearance(
        self,
        *,
        content: str,
        spec: TargetSpec,
        appearance: appearance_api.AppearanceOverride | None,
        roster: list[CharacterEntry] | None = None,
    ) -> str:
        """把外观覆盖的**加权词放到最前面**（1.0.38，实验性功能）。

        - ``natural``（nai_artist）与 ``tags``（image_generator_plugin-neo）走**同一条**
          路径：加权词一律放**最前**（``1.4::purple-red eyes::, <原文>``）——
          需求原话是「外观覆盖放最前」，与分区覆盖（追加在最后）刚好相反；
        - ``tag_weight < 1.0`` 且语法不是 ``none`` 时，先调
          :func:`appearance.downweight_character_tag` 把画面里**第一处**角色标准标签
          改写成降权写法（**只改这一处**，其它标签一个字不动）；
        - **幂等**：内容里已经带着这次的加权词时不重复追加（多图轮的第 2、3 张）；
        - ``appearance`` 为 ``None`` / 空结果时**原样返回**，不留任何痕迹。
        """
        if appearance is None or not appearance.ok:
            return content
        if appearance.is_applied(content):
            logger.debug("外观覆盖的加权词已在提示词里，不重复追加")
            return content
        updated = content
        rewritten = ""
        if appearance.downweights:
            updated, rewritten = appearance_api.downweight_character_tag(
                updated,
                roster,
                weight=appearance.tag_weight,
                weight_syntax=appearance.weight_syntax,
            )
        prefix = appearance.prefix_text()
        if not prefix:
            return content
        stripped = updated.strip()
        merged = f"{prefix}, {stripped}" if stripped else prefix
        logger.info(
            f"已追加外观覆盖（{spec.action} / {spec.append_mode}）: "
            f"{appearance.summary()} → {prefix}"
        )
        if rewritten:
            # 降权到底改成了什么，用户要能一眼核对（含被改的那个标签原文）
            logger.info(f"外观覆盖：角色标签已降权 → {rewritten}")
        return merged
