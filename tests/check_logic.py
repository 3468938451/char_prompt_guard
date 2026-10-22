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

1.0.23 起修三件事（C83~C88），判定链仍然一行未改：

1. **皮肤名不再被当成表外角色名**：用户说「画粉色妖精小姐这套」时，「粉色妖精小姐」
   是**一套皮肤**（皮肤表里有），可 1.0.20 的「表外点名」提取链可能把同一句话里的
   名字当成**角色表外的角色名** → 1.0.21 会为此多问一次模型，`consistency_check="fix"`
   时还会用解析结果去替换 / 删除画面里**用户这批消息没提过**的表内角色标签。
   现在皮肤识别（`handler._scan_outfit_names`）**先跑**，它认出来的皮肤 / 时装名
   （句式 / 引号抓到的候选 + 命中皮肤表的正式名）通过 `exclude` 传给
   `character.extract_outside_names` **豁免**；注入位置与语义一个字没变。
   - C86：用户消息「画粉色妖精小姐这套」+ 皮肤表命中 + 角色表里**没有**它 +
   `consistency_check="fix"` + 画面里有用户这次没提过的 `elysia` 标签 →
   皮肤**照旧注入**（表命中、0 次皮肤模型调用），角色侧**一次解析都不问**，
   日志里没有 `用户点名「粉色妖精小姐」`，画面里的 `elysia` 标签原样保留；
   - C88（豁免真的生效）：换一个 1.0.20 **确实会抓到**的写法「画「粉色妖精小姐」这套」
     —— 单元级断言「不传 exclude 抓得到、传了就抓不到」，端到端断言角色侧 0 次解析、
     日志里有 `皮肤名豁免`。为什么必须单独一条：C86 的写法会被
     `_SENTENCE_NAME_STOP_CHARS` 的「这」提前拦掉，光靠它证明不了豁免在起作用；
2. **作品名去重**（`character._normalize_game_field`）：模型把英文作品名也塞进
   `game_cn` 时（`game_cn="崩坏：星穹铁道 (Honkai: Star Rail)"`、
   `game_en="Honkai: Star Rail"`），1.0.22 会拼出
   「… (Honkai: Star Rail) (Honkai: Star Rail)」—— C87 断言生成的中文说明与日志里
   `Honkai: Star Rail` **只出现一次**（原始字段一个字没动）；
3. **`outfits.infer_unnamed`**（新增，**默认关**）：用户只说「流萤的那一套皮肤」而
   没点名具体皮肤时，开了才由模型推断该角色最知名的一套并注入（角色名必须是角色表内
   角色，取最近一条消息里提到的；解析走 `outfit.infer_outfit_via_llm`，校验口径与
   1.0.22 完全相同；缓存键 `<角色名>|__infer__`；日志标注「来源=模型推断」）。
   - C83：`infer_unnamed=false`（默认）+「我想看流萤的那一套皮肤～」→ **0 次模型调用**
     （解析与推断两条路都是 0）、无皮肤注入、日志无 `皮肤推断`、内容与关掉 `[outfits]`
     时逐字一致；
   - C84：`infer_unnamed=true` + 同一句话 + 假推断返回合法 payload（`inferred=true`、
     关键词 `firefly's outfit` / `white dress`，**全是小写 ASCII**）→ 关键词注入成功、
     **角色在前皮肤在后**，日志含 `皮肤推断(模型)`、`来源=模型推断`、`建议加入皮肤表`；
   - C85：`infer_unnamed=true` + 假推断 `unknown=true` → **什么都不注入**、
     日志含 `皮肤推断(LLM)失败` 与失败原因，内容与不开这一维时逐字一致。

**C77~C88 的 ``make_config`` 需要皮肤功能时显式传 ``outfits_enabled=True`` 与
``outfits=...``**（1.0.23 的 ``outfits_infer_unnamed`` 默认 false，与插件默认值一致）。

1.0.28 起把**「泛指的穿着类型」**和**「具体皮肤名」**分开（C89~C103），起因是用户
需求：「当说的是『我想看你的 xxx』（如**学生**、**居家**、**泳衣**），判断**不是
皮肤**，转成"穿对应的衣服"；否则衣服这个产出有时相当于黑箱，只能由 bot 决定 ——
要能**看到注入了什么样的衣服**」；随后追加两条：「内置词表和皮肤表都没有的'穿着词'，
也要自动用大模型找出提示词并注入」「判断逻辑这一步加上，防止和之前的打架；打到没有
架可打再上传」。最终**优先级**（写进注释与 README）：

**① 皮肤表命中**（零模型调用）→ **② 内置 / 配置的穿着类型表**
（``outfit.WEAR_TYPE_KEYWORDS`` + ``[outfits] wear_types``，零模型调用）
→ **③ 皮肤模型解析**（具体皮肤名，``auto_resolve``）→ **④ 穿着模型解析**
（"看起来是穿着"的候选，穿着专用提示词，同样 ``auto_resolve``）
→ **⑤ ``infer_unnamed``**（没说名字时由模型推断一套，默认关）。

| 用例 | 钉住什么（冲突矩阵第几条） |
| --- | --- |
| C89 | 「学生装」命中**内置表**→ 零模型调用、注入 ``school uniform, pleated skirt``、日志 ``穿着类型解析(内置表)``、措辞是 ``（穿着：“…”：…）``（不是「皮肤/时装」），皮肤表内容一个字都不进来 |
| C90 | 内置表的**同义写法**：「泳衣」/「居家服」各自命中并注入，日志可核对，0 次模型调用 |
| C91 | **#1 皮肤表 ↔ 穿着类型**：皮肤表里的「学生会长制服」（本身也含穿着特征字）仍按**皮肤名**走 |
| C92 | **#10 穿着 ↔ `outfits.enabled=false`**：零开销、不注入、无穿着日志；1.0.26 那条**纯字符串豁免识别**仍在 |
| C93 | **④ 穿着模型解析**：「雨衣」（两张表都没有）→ 模型返回 ``raincoat`` → 注入、**恰好 1 次**调用、日志 ``穿着解析(模型)`` |
| C94 | **④ 的降级**：模型 ``unknown=true`` → 不注入、日志 ``穿着解析(LLM)失败``、内容与关掉 ``[outfits]`` 时逐字一致 |
| C95 | **#2 皮肤名 ↔ 穿着词同句**：只处理一个（皮肤表优先）、日志写明**被忽略的候选** |
| C96 | **#3 穿着名 ↔ 表外点名豁免**：「画「雨衣」这套」→ 穿着名进豁免集，角色侧 0 次解析、``fix`` 不动画面里的角色标签、穿着照常注入 |
| C97 | **#4 穿着词 ↔ 角色表内名字**：角色表里有「雨衣」时那是**角色**，绝不注入穿着关键词 |
| C98 | **#5 穿着 ↔ 拆单**：角色仍按拆单规则（LLM 按画面判定补花火），穿着关键词仍追加 |
| C99 | **#6 穿着 ↔ 一致性检测 fix**：替换角色标签时 ``（穿着：“…”：…）`` 段**原样保留** |
| C100 | **#7 穿着 ↔ 幂等**：同类（穿着）不重复追加；不同类（皮肤 + 穿着）**可以共存** |
| C101 | **#8 穿着 ↔ 记忆库写回**：穿着解析**不写回**（对照组：同一个配置下皮肤解析照旧写回 1 次） |
| C102 | **#9 穿着 ↔ `infer_unnamed`**：识别到穿着时**绝不进推断**（推断调用 0 次） |
| C103 | **穿着模型解析的缓存**：同一个穿着词连问两次 → 第二次命中缓存（0 次新调用）、注入逐字一致、日志 `穿着解析(缓存)` |

``FakeOutfit`` 1.0.28 起再多一条桩：``outfit.resolve_wear_via_llm``（``wear`` 方法，
用 ``wear_payload`` / ``wear_error`` 决定返回什么、调用记在 ``wear_calls``）——
三条模型路（皮肤解析 / 皮肤推断 / 穿着解析）各自的 payload 与计数器互相独立。
``make_config`` 的 ``[outfits]`` 默认值与 1.0.27 相同（C1~C88 一个字都没改）。

1.0.29 起修**独立只读审计发现的两条**（C104~C106，**判定链、优先级、注入位置
一个字都没改**）：

| 用例 | 钉住什么 |
| --- | --- |
| C104 | **穿着匹配收紧为「相等或后缀」**（审计 🟠1）：1.0.28 的"候选**包含**表中词即命中"会把**不含穿着意图**的话吞掉 —— 「学生会」「学生时代」里含「学生」→ 零模型调用地注入 ``school uniform, pleated skirt``（1.0.27 时同候选走模型兜底 → unknown → 不注入）。单元级钉住 ``学生 / 学生装 / 居家服 / 泳衣 / 婚服 / 和服 / 穿学生装 / 我想看你的学生 / 男生学生`` 仍命中、``学生会 / 学生时代 / 我想看学生会`` 不命中；端到端钉住「我想看学生时代的花火～」（候选 ``学生时代``）**不注入**、日志无 ``穿着类型解析(内置表)``、走模型兜底且 ``unknown`` → 什么都不注入。**用例内同时钉住一条残留**：「我想看学生会～」的候选在**抽取**那一步被虚词「会」截断成「学生」→ 相等命中仍然注入（抽取侧问题，不在本次两条范围内；**已在 1.0.30 修掉**，见下表 C104 那一行） |
| C105 | **穿着也做归属校验**（审计 🟠2）：「爱莉穿婚纱的样子」是**穿着**那句话（同句提到爱莉），判定角色由更新的那句「画流萤吧」给出（→ decided=[流萤]）→ ② / ④ 两条路都**不注入** + WARNING ``穿着跳过：穿着「婚纱」属于「爱莉希雅」，但本张判定角色是「流萤」``；② 走内置表（0 次模型调用）、④ 走穿着模型解析（恰好 1 次），两条都先**真的解析出关键词**再被归属校验拦下（不是空转） |
| C106 | **防误杀**：同一句穿着请求，判定角色 = 爱莉希雅（与归属一致）→ 婚纱**照常注入**、日志无 ``穿着跳过`` |

``FakeOutfit`` 1.0.29 起不用加桩：C104 用默认 payload（``{"unknown": True}``），
C105/C106 只用 1.0.28 已有的 ``wear_payload`` / ``wear_calls``。

**1.0.30 修掉上面那条残留，并补三件产品需求**（C104 重写、C107/C108 新增、
C109~C111 新增；**原 C1~C103 与 C105/C106 的断言一个字都没改**）：

| 用例 | 钉住什么 |
| --- | --- |
| C104（重写） | **抽取侧修掉虚词截断**：句式抽取与候选过滤**共用同一张虚词表**（都不把「会」当虚词，``outfit._OUTFIT_STOP_CHARS`` / ``_OUTFIT_SENTENCE_STOP_CHARS`` / ``_OUTFIT_SENTENCE_NAME_CLASS``），「我想看学生会～」的候选因此是完整的 ``学生会``（1.0.29 时是截断的 ``学生``，只会把句式那一侧改掉时又会被**过滤**整条丢掉）→ 不等于任何键、也不以任何键结尾 → ② 不命中 → 走 ③/④ 模型兜底（假模型 ``unknown``）→ **什么都不注入**。断言：``council_candidate == ["学生会"]``、端到端「走的是皮肤模型路（``stub.calls == ["学生时代"]``）且没有注入」；单元样例表**加严**（1.0.30 新补的 15 条同义写法全部要求命中）。**这是"按真实可达范围重写"，不是放宽** —— 改动前断言的是 bug 存在，改动后断言的是 bug 已修 |
| C107 | **用户最新点名 = 角色判定最高优先**：``那画流萤吧``（2 分钟前）→ ``画爱莉的婚纱～``（1 分钟前）→ 触发消息 ``嗯～``。1.0.30 起判定 = **爱莉希雅**（依据 ``判定依据=用户点名(最新消息):爱莉希雅``）→ ``穿着类型解析(内置表): 婚纱`` → **正常注入** ``wedding dress, bridal veil, white dress``（不再 ``穿着跳过``）、0 次模型调用；括号里写的永远是"**原来会生效的那个结论**"（``handler._overtaken_conclusion``），本 fixture 里同样是爱莉希雅（``换人还是追加不确定``）—— 真实事故里被压过的是上一条粘住的流萤（C105/C106 守的就是那个场景）。修这条用例时一并补上了**归属句式** ``…的婚纱``（``outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN``）：原先七条句式都抓不到「画爱莉的婚纱～」里的「婚纱」，判定改对了、衣服却还是没注入 |
| C108 | 同一判定 + **画面描述里已经写着** ``firefly \(honkai: star rail\)`` + ``consistency_check="fix"`` → 1.0.19 的期望集合对账识别冲突（画面=流萤、期望=爱莉希雅）并**替换**成 ``elysia \(honkai impact 3rd\)``；日志同时能看到 ``判定依据=用户点名(最新消息):爱莉希雅`` 与 ``优先于主题跟踪:…``（＝「用户最新说的人 ≠ 画面里的人」）。**档位说明**：冲突本身与 ``warn``/``fix`` 无关（``warn`` 只报不改），本用例按 ``fix`` 写是因为需求要的是"换成用户说的那个角色" |
| C109 | **闸门①（追加语义 / 多人）**：与 C35 同一场景（``还有伊甸，一起画``）→ **不采用**新信号、结果与 C35 一致（本条 + 上一条都补），日志里没有 ``判定依据=用户点名(最新消息)``、走的是主题跟踪的 ``[追加]`` 分支 |
| C110 | **闸门②（疑问语气 / 自我指代）**：A）与 C42 同一句「是爱莉还是你？」→ 结果与 C42 一致（LLM 仲裁说花火就是花火）；B）触发消息含「你自己」→ 不补、内容一字不改，两条都不出现 ``判定依据=用户点名(最新消息)`` |
| C111 | **闸门③（只读清洗后的正文）**：A）与 C43 同一份事故原文（整条都是 ``[回复…][图片(…)]``）→ 清洗后没有正文 → ``判定依据=无(不补)``、内容不改；B）正文 + 引用块混写 → 清洗后的正文照旧点名（``判定依据=用户点名(最新消息):爱莉希雅``），引用块里的花火 / 流萤一个字都不参与 |

``FakeOutfit`` 1.0.30 起同样不用加桩：C107/C108 全部命中内置穿着类型表（0 次模型调用），
断言里 ``stub.calls == []`` 与 ``stub.wear_calls == []`` 就是这条。

**1.0.31 只修两条行为（C112/C113 新增；原 C1~C111 的断言一个字都没改、也没放宽）**：

| 用例 | 钉住什么 |
| --- | --- |
| C112 | **触发消息没点名时照旧读得到前一条的点名**（目标 A）：``那画流萤吧``（2 分钟前）→ ``画爱莉的婚纱～``（1 分钟前）→ 触发 ``都试试？``（既含「都」＝追加语义词、又含「？」，**闸门①②全会命中**）。1.0.30 在触发消息那一条上先撞闸门 → 整条信号让位 → 退回旧粘性（可能仍按流萤 + ``穿着跳过``）；1.0.31 起触发消息**没点名**就跳过它的闸门①②，直接落到"触发前最近 1 条用户消息"上并按**全部**闸门判定 → 判定 = **爱莉希雅**、日志 ``判定依据=用户点名(最新消息):爱莉希雅``、婚纱关键词与 ``（穿着：“婚纱”：…）`` 都在、**无** ``穿着跳过``、0 次模型调用。候选表最多两格（触发消息 + ``_LATEST_NAMED_HISTORY_LIMIT`` = 1 条），所以**不会**往更早找 |
| C113 | **归属句式容忍句末语气词**（目标 B）：「画爱莉的婚纱吧～」与「画爱莉的婚纱呢」**两跑** —— 判定 = **爱莉希雅**、``outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN`` 照样抓到「婚纱」→ 关键词与 ``（穿着：“婚纱”：…）`` 都在、**无** ``穿着跳过``、0 次模型调用。1.0.30 的边界是"候选后面不能再接名字字符"，而 ``吧`` / ``呢`` 也是汉字 → 整条被挡掉（判定对、衣服没穿上）。``会`` **不在** ``outfit._OUTFIT_STOP_CHARS`` 里，所以「我想看你的学生会长制服」照旧拆不出「学生」（C91 语义不变） |

``FakeOutfit`` 1.0.31 起同样不用加桩：C112/C113 走的都是内置穿着类型表（0 次模型调用）。

**1.0.32 只收窄两处（C114/C115 新增；原 C1~C113 的断言一条都没改、也没放宽）**：

| 用例 | 钉住什么 |
| --- | --- |
| C114 | **归属句式的边界只放行句末语气词**（审计 🟠1）：1.0.31 放行的是**整张** ``outfit._OUTFIT_STOP_CHARS``（``的`` / ``是`` / ``我`` / ``你`` / ``都`` … 全在里面），于是「爱莉的**婚纱是**我挑的」「流萤的**校服被**雨淋了」「画的**婚纱的**话」这类**不含穿着意图**的话也会被当成穿着候选（1.0.30 不会）—— 抓错会把衣服画错，与"宁少勿多"相悖，而文档只承诺放行**句末语气词**。1.0.32 起边界只用 ``outfit._OUTFIT_TAIL_PARTICLES``（``吧吗呢啊呀哦嘛呗啦咯喔``）。两跑合一：① **单元级** —— 模式在"虚词尾巴"上不匹配（``是`` / ``被`` / ``的`` / ``我``）、在 C113 的语气词尾巴与 C107 的标点/串尾上照旧匹配，并钉住语气词表是 ``_OUTFIT_STOP_CHARS`` 的**真子集**；② **端到端** —— 四段不含穿着意图的话判定角色照旧注入、但**不注入**穿着（无 ``（穿着：`` / ``已追加穿着`` / ``穿着类型解析`` / ``穿着解析(LLM)``）、0 次模型调用，另加一条 ``画爱莉的婚纱吧～`` 的**正例**（C113 的形状）防收窄过头 |
| C115 | **触发消息是追加指令时不再"只看前一条的单名"**（审计 🟠2）：``画爱莉和流萤一起``（2 分钟前）→ ``流萤这张不错``（1 分钟前）→ 触发 ``一起画吧``。主题跟踪结论 = **流萤** + ``换人还是追加不确定``（间隔 1 分钟 < ``multi.multi_window_minutes``）。1.0.31 的让位会直接按前一条的单名拍板（``判定依据=用户点名(最新消息):流萤``）并 return，**压过**主题跟踪的不确定与 LLM 仲裁 → 该画两人只画一人；1.0.32 起 ``handler._additive_trigger_needs_tracking`` 判定"追加指令 + 原来会生效的结论没定死一个人"→ **不让位**，走跟踪 + LLM 仲裁（假模型给出两人）→ **两人同框**、日志出现 ``用户最新点名不采用（1.0.32）`` / ``换人还是追加不确定`` / ``判定依据=LLM 仲裁``，且**不出现** ``判定依据=用户点名(最新消息)``。反例（同一段上文、触发换成 ``都试试？``）：兜底只认"追加指令"，含 ``？`` 的疑问句照旧让位给 ``流萤这张不错``（``判定依据=用户点名(最新消息):流萤``）、一次模型都不问 —— 反例通过即证明没有"让位过头"。``FakeOutfit`` 在这条里同样不用加桩（本用例不开 ``[outfits]``） |

**1.0.34 只加三条用例（C116~C118 新增；原 C1~C115 的断言一条都没改、也没放宽）**：
这一版把「用户必须手工打框架补丁」这条依赖，改成**插件自己在运行时探测 + 内存里补一层**
（新模块 ``compat``，只改内存、不写任何文件）。三条用例全部用**离线桩**跑：假宿主模块 +
假事件总线（替换 ``compat.host_module`` / ``compat.event_bus`` / ``compat.event_type``
三个接缝），**不真导入** ``src.core.utils.llm_tool_call``，也不需要框架真的在发事件。

| 用例 | 钉住什么 |
| --- | --- |
| C116 | **已打静态补丁就一个字都不改**：假宿主 ``run_tool_call`` 的**源码**里带着补丁标记（事件名 ``BEFORE_ACTION_CALL`` / ``BEFORE_TOOL_CALL``、``.publish(``、写回 ``"args"``）→ ``install()`` 返回 ``already_patched``，模块属性仍是**同一个函数对象**、也**没有**包装标记（"已打补丁的机器一行代码都不执行"） |
| C117 | **没打补丁就补上，而且真的能改写参数**：未打补丁的假宿主 → ``install()`` 返回 ``applied``，``run_tool_call`` 被 ``functools.wraps`` 包装（``__wrapped__`` 指回原函数）。三跑：**A** 有订阅者 → 恰好发一次 ``before_action_call``（带 ``signature`` / ``action_name`` / ``args`` / ``message`` / ``stream_id``）、假订阅者改写 ``args`` → **原函数收到的就是改写后的 calls**（"生图前改提示词"的最小闭环）；**B** 清空订阅者 → **一个事件都不发**、原函数收到原样参数（零开销路径）；**C** ``tool-*`` 名字 → 发 ``before_tool_call``、``tool_name`` 与签名都对（agent 路径两种组件都覆盖） |
| C118 | **三条兜底**：① ``general.runtime_framework_compat=false`` → ``disabled`` 且不替换；② 包装后原函数抛异常 → 异常**照旧向上传播**（不吞；抛之前事件照发）、且**不打乱后续调用**（第二次照常发事件、照常执行）；③ 桩里没有 ``run_tool_call``（以及"有但不是可调用"）→ ``unsupported``、不崩、不改任何东西 |

``compat.install()`` 的返回值共五种（``disabled`` / ``unsupported`` /
``already_patched`` / ``already_installed`` / ``applied``），插件入口按它各打一条
INFO / WARNING（``unsupported`` 是唯一按 WARNING 打的那种）。

**1.0.36 新增六条（C119~C124；原 C1~C118 的断言一条都没改、也没放宽）**：
这一版加的是 **实验性功能**「分区覆盖」（``[region_override]``，**默认关闭**）——
打开后生图效果可能不理想（模型可能把两个角色的特征混在一起、比例异常或细节崩坏），
普通使用完全不受影响。实现拆成「纯字符串解析（``region.py``）」+「最后一步追加
（``handler._apply_region_override``）」，**不碰**角色判定 / 一致性检查 / 幂等 /
归属校验 / 记忆库写回。

| 用例 | 钉住什么 |
| --- | --- |
| C119 | **分区解析 = 三重条件**（分区词 + 角色名 + 归属句式，缺一不认）：三种入口语言各断言一次 —— ``头是流萤的，身子是爱莉的`` → ``{head: 流萤, body: 爱莉希雅}``、``头换成流萤`` → ``{head: 流萤}``、``用爱莉的身子配流萤的头`` → ``{head: 流萤, body: 爱莉希雅}``；别名「爱莉」归一到角色表正式名「爱莉希雅」 |
| C120 | **注入**（两跑）：A nai_artist（natural）→ 正向段含 head 与 body 特征词且 **head 在前**、段落在角色说明之后；``negative_swap=true`` 时"被换掉那个角色在同分区的原特征"（爱莉希雅的头 / 流萤的身子）出现在 DEBUG —— nai_artist 的 Action 没有负向参数；B image_generator_plugin-neo（tags）→ 关键词追加进 ``content_description``，负向词**真的**写进 ``negative_prompt`` 并带上 ``anti_mix`` 的防串味词 |
| C121 | **单分区**：``头换成流萤`` 只动 head（``头=流萤`` 恰好一段），流萤条目里的 body / outfit 特征词**一个都不出现**，且角色仍在分区段之前 |
| C122 | **防误触发**：``流萤的头饰很好看`` / ``头疼`` / ``画流萤`` 三句 → 解析为空、没有分区注入、**没有任何"分区覆盖"相关日志**、没有负向参数被写；另钉**边界样本**（反例：``馒头是流萤的`` / ``石头是爱莉的`` / ``回头是爱莉的``（单字分区词前置边界）、``用流萤的头饰`` / ``配流萤的头像`` / ``和爱莉的头像拍照``（尾边界）、``流萤的头是红色``（角色解析不出）；正例：``我的头是流萤的`` / ``用流萤的头`` / ``配流萤的头吧`` / ``和爱莉的身子`` 照旧命中）与**引用块**（整条触发消息在 ``[回复 …]`` 里 → 清洗后无正文 → 不注入，与 1.0.30 闸门③ 同一 ``strip_quoted_blocks`` 口径） |
| C123 | **开关与降级**：① ``region_override.enabled=false``（默认）→ 零动作（内容与"没配条目"时逐字一致、日志无 ``分区覆盖``）；② 开着但 ``entries`` 里没有该角色 → **不分区** + DEBUG（``分区覆盖=跳过`` / ``entries 里没有该角色的条目``），角色注入照旧 |
| C124 | ``stylized_swap``：true → 正向段出现 ``stitched neck`` / ``visible seam`` / ``head slightly tilted``（``接缝过渡`` 标签）；false（默认）→ 这三句**正向与负向都不出现** |

C119~C124 全部离线（不调模型）：分区条目用**真的配置节类**
（``config.RegionEntrySection``）构造，触发消息里直接写入口语言。

用法（用实例自带 venv）：

    $env:NEO_ROOT="<你的 Neo-MoFox 框架根>"
    & "$env:NEO_ROOT\.venv\Scripts\python.exe" <插件目录>\tests\check_logic.py

默认只跑离线用例（零成本）。加 ``--llm`` 会额外跑两条需要真调小模型的
用例（角色表为空时的兜底提取；提示词已写表内角色时识别表外角色）。
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import inspect
import json
import logging
import os
import sys
import time
import types
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
TAG_FIREFLY = r"firefly \(honkai: star rail\)"

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
    outfits_infer_unnamed: bool = False,
    region_entries: list | None = None,
    region_enabled: bool = False,
    region_regions: list[str] | None = None,
    region_stylized: bool = False,
    region_negative_swap: bool = True,
    region_anti_mix: bool = True,
) -> CharPromptGuardConfig:
    """按用例参数构造配置。

    1.0.18 起 ``[memory]``（外部记忆库）**默认在这里关掉**：C1~C59 是 1.0.17 及
    以前的行为回归，它们不该因为「本机恰好开着 Booku Memory」而变红；需要记忆库
    的用例（C60~C63）显式传 ``memory=True`` 并配 :class:`FakeMemory` 假传输层。
    1.0.19 的 C64~C67 同理（也都是 ``memory=True`` + :class:`FakeMemory`）。
    1.0.22 起 ``[outfits]``（皮肤/时装）**同样默认关掉**：C1~C76 是 1.0.21 及以前的
    行为回归，不该因为本机恰好配了皮肤表或可用模型而变红；需要皮肤功能的用例
    （C77~C106）显式传 ``outfits_enabled=True`` 与 ``outfits=...``。
    1.0.23 的 ``outfits_infer_unnamed``（「没说名字的那一套」）**默认 false**，
    与插件配置类的默认值一致（见 tests/check_import.py 的断言）。
    1.0.28 的穿着类型表 ``outfits.wear_types`` 同样**默认留空**（= 只用内置表），
    用例里没有额外开关要传。
    （插件配置类里的默认值仍是 ``true`` —— 见 tests/check_import.py 的默认值断言。）
    1.0.36 起 ``[region_override]``（**实验性功能**：分区覆盖）**默认关掉**，与配置
    类里的 ``enabled = false`` 一致：C1~C118 是 1.0.34 及以前的行为回归，不该因为
    本机恰好配了分区条目而变红；需要分区覆盖的用例（C119~C124）显式传
    ``region_enabled=True`` 与 ``region_entries=...``。
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
    cfg.outfits.infer_unnamed = outfits_infer_unnamed
    # 1.0.36：分区覆盖（实验性功能，默认关闭）
    cfg.region_override.enabled = region_enabled
    cfg.region_override.entries = list(region_entries or [])
    if region_regions is not None:
        # 默认不覆盖 → 用配置类给的列表（["head", "body", ...]）；
        # 想换顺序 / 只留部分分区时传一个列表进来（`region.parse_regions` 也认字符串写法）
        cfg.region_override.regions = region_regions
    cfg.region_override.stylized_swap = region_stylized
    cfg.region_override.negative_swap = region_negative_swap
    cfg.region_override.anti_mix = region_anti_mix
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

    1.0.23 起同一个替身还负责「没说名字的那一套」那条路：
    ``outfit.infer_outfit_via_llm``（同样两个位置）也换成 :meth:`infer`，
    它用 ``infer_payload`` / ``infer_error`` 决定返回什么 —— **与解析那条路
    完全独立**（各自的 payload 与计数器），因此 C83~C85 能分别断言
    「推断问了几次模型」「解析问了几次模型」。

    1.0.28 起再多一条「穿着模型解析」：``outfit.resolve_wear_via_llm``
    （同样两个位置）换成 :meth:`wear`，用 ``wear_payload`` / ``wear_error`` 决定
    返回什么，调用记在 ``wear_calls`` —— 三条路各自独立，C93/C94 据此断言
    「穿着那条路恰好问了一次」「皮肤那条路一次都没问」。

    退出 with 块时恢复原函数，并清掉解析缓存 —— 免得上一条用例的缓存把下一条
    用例的「该不该问模型」判断带偏。
    """

    def __init__(
        self,
        payload: dict | None = None,
        *,
        error: BaseException | None = None,
        infer_payload: dict | None = None,
        infer_error: BaseException | None = None,
        wear_payload: dict | None = None,
        wear_error: BaseException | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {"unknown": True}
        self.error = error
        self.calls: list[str] = []
        #: 1.0.23「皮肤推断」那条路的假结果与调用记录
        self.infer_payload = (
            infer_payload if infer_payload is not None else {"unknown": True}
        )
        self.infer_error = infer_error
        self.infer_calls: list[str] = []
        #: 1.0.28「穿着模型解析」那条路的假结果与调用记录
        self.wear_payload = (
            wear_payload if wear_payload is not None else {"unknown": True}
        )
        self.wear_error = wear_error
        self.wear_calls: list[str] = []
        self._original_outfit = None
        self._original_handler = None
        self._original_infer_outfit = None
        self._original_infer_handler = None
        self._original_wear_outfit = None
        self._original_wear_handler = None

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

    async def infer(self, character: str, **kwargs):
        """假「皮肤推断」函数（1.0.23）：记录调用，再按 infer_payload 返回。

        校验链与解析那条路**完全相同**（同一个 ``parse_outfit_resolution_payload``），
        只是结果一定带 ``inferred=True`` —— 与 ``outfit.infer_outfit_via_llm``
        的真实行为一致（哪怕模型漏填 ``inferred`` 字段）。
        """
        from ..outfit import ResolvedOutfit, parse_outfit_resolution_payload

        self.infer_calls.append(character)
        if self.infer_error is not None:
            raise self.infer_error
        outfit, reason, confidence = parse_outfit_resolution_payload(self.infer_payload)
        if outfit is None:
            return ResolvedOutfit(
                character=character,
                reason=reason,
                confidence=confidence,
                raw_name=character,
                inferred=True,
            )
        return ResolvedOutfit(
            name=outfit.name or f"{character}的那一套",
            character=character,
            keywords=outfit.keywords,
            description=outfit.description,
            source="模型",
            confidence=confidence,
            raw_name=character,
            inferred=True,
        )

    async def wear(self, name: str, **kwargs):
        """假「穿着模型解析」函数（1.0.28）：记录调用，再按 wear_payload 返回。

        校验链与皮肤解析**完全相同**（同一个 ``parse_outfit_resolution_payload``），
        只是结果一定带 ``wear="模型"``、名字用用户的原词 —— 与
        ``outfit.resolve_wear_via_llm`` 的真实行为一致。
        """
        from ..outfit import (
            OUTFIT_WEAR_MODEL,
            ResolvedOutfit,
            parse_outfit_resolution_payload,
        )

        self.wear_calls.append(name)
        if self.wear_error is not None:
            raise self.wear_error
        outfit, reason, confidence = parse_outfit_resolution_payload(self.wear_payload)
        if outfit is None:
            return ResolvedOutfit(
                name=name,
                reason=reason,
                confidence=confidence,
                raw_name=name,
                wear=OUTFIT_WEAR_MODEL,
            )
        return ResolvedOutfit(
            name=name,
            keywords=outfit.keywords,
            description=outfit.description,
            source="模型",
            confidence=confidence,
            raw_name=name,
            wear=OUTFIT_WEAR_MODEL,
        )

    def __enter__(self) -> Self:
        from .. import handler as handler_module
        from .. import outfit as outfit_module

        self._original_outfit = outfit_module.resolve_outfit_via_llm
        self._original_handler = handler_module.resolve_outfit_via_llm
        outfit_module.resolve_outfit_via_llm = self  # type: ignore[assignment]
        handler_module.resolve_outfit_via_llm = self  # type: ignore[assignment]
        # 1.0.23：推断那条路也一起替换（否则 C84/C85 会真的去问模型）
        self._original_infer_outfit = outfit_module.infer_outfit_via_llm
        self._original_infer_handler = handler_module.infer_outfit_via_llm
        outfit_module.infer_outfit_via_llm = self.infer  # type: ignore[assignment]
        handler_module.infer_outfit_via_llm = self.infer  # type: ignore[assignment]
        # 1.0.28：穿着那条路同样一起替换（否则 C93/C94 会真的去问模型）
        self._original_wear_outfit = outfit_module.resolve_wear_via_llm
        self._original_wear_handler = handler_module.resolve_wear_via_llm
        outfit_module.resolve_wear_via_llm = self.wear  # type: ignore[assignment]
        handler_module.resolve_wear_via_llm = self.wear  # type: ignore[assignment]
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
        if self._original_infer_outfit is not None:
            outfit_module.infer_outfit_via_llm = self._original_infer_outfit
        if self._original_infer_handler is not None:
            handler_module.infer_outfit_via_llm = self._original_infer_handler
        if self._original_wear_outfit is not None:
            outfit_module.resolve_wear_via_llm = self._original_wear_outfit
        if self._original_wear_handler is not None:
            handler_module.resolve_wear_via_llm = self._original_wear_handler
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
#: 「没说名字的那一套」的说法（1.0.23；用户需求里的第二种说法，1.0.22 刻意不做）
UNNAMED_REQUEST = "我想看流萤的那一套皮肤～"
#: 「皮肤推断」的假结果（1.0.23）：结构与 1.0.22 的皮肤解析一致，只多一个
#: ``inferred: true``；关键词必须是小写 ASCII（校验链与 1.0.22 完全相同）
INFER_PAYLOAD = {
    "unknown": False,
    "inferred": True,
    "outfit_cn": "流萤·花嫁",
    "outfit_en": "Firefly Wedding Dress",
    "tag": "firefly's outfit, white dress",
    "keywords": ["firefly's outfit", "white dress"],
    "confidence": 0.8,
}
#: 期望出现在提示词里的「推断出来的」关键词
INFER_KEYWORDS = ("firefly's outfit", "white dress")

# ── 1.0.28：泛指的穿着（内置表 / 穿着模型解析）用例的夹具 ──────────────
#: 「学生装」——命中**内置穿着类型表**的「学生装」（同义写法之一），零模型调用
WEAR_STUDENT_REQUEST = "我想看你的学生装～"
#: 内置表里「学生 / 学生装 / 校服 / JK / 水手服」映射到的那组关键词
STUDENT_KEYWORDS = ("school uniform", "pleated skirt")
#: 「泳衣」/「居家服」——同样是内置表的同义写法（C90 两条）
WEAR_SWIM_REQUEST = "我想看你的泳衣～"
WEAR_HOME_REQUEST = "我想看你的居家服～"
SWIM_KEYWORDS = ("swimsuit",)
HOME_KEYWORDS = ("loungewear", "casual", "home wear", "t-shirt")
#: 「雨衣」——**内置表与皮肤表都没有**，但含穿着特征字「衣」→ 走穿着模型解析（C93/C94）
WEAR_REQUEST = "我想看你的雨衣～"
#: 穿着模型解析的假结果（只给小写 ASCII 服装关键词，与需求里的例子同形）
WEAR_PAYLOAD = {
    "unknown": False,
    "tag": "raincoat, hood",
    "keywords": ["raincoat", "hood"],
    "confidence": 0.9,
}
WEAR_KEYWORDS = ("raincoat", "hood")
#: 「学生会长制服」——**皮肤表里的具体皮肤名**，同时也是穿着特征词（C91 防误判）
COUNCIL_OUTFIT = (
    "学生会长制服 | 爱莉希雅 | student council uniform | 爱莉希雅的学生会长制服"
)
COUNCIL_REQUEST = "我想看你的学生会长制服"

# ── 1.0.29（审计两修）用例的夹具 ──────────────────────────────────────
#: C104：「我想看学生时代的花火～」——句式抓到候选 **学生时代**（前缀含「学生」，
#: 本身不是穿着词）；1.0.28 的"包含匹配"会零模型调用地注入 school uniform。
WEAR_ERA_REQUEST = "我想看学生时代的花火～"
#: C104 的第二个样例（1.0.30 起**已经从"残留"变成"修好了"**）：「我想看学生会～」。
#: 1.0.29 时候选在**句式抽取**那一层被虚词「会」截断成「学生」（`_OUTFIT_STOP_CHARS`），
#: 于是以**相等**命中内置表的「学生」→ 仍然注入 school uniform。
#: 1.0.30 把「会」从**句式抽取**的虚词里摘掉（`_OUTFIT_SENTENCE_STOP_CHARS`），
#: 候选因此是完整的「学生会」→ 不命中任何键 → 走模型兜底 → 不注入。
WEAR_COUNCIL_REQUEST = "我想看学生会～"
#: C105/C106：**同一句**穿着请求（句式「穿X」抓到候选「婚纱」，同句提到爱莉），
#: 只换"本张判定角色是谁"的那句更新的话 —— 一个该拦、一个不该拦（防误杀）。
WEAR_OWNER_SENTENCE = "爱莉穿婚纱的样子"
WEAR_RAIN_OWNER_SENTENCE = "爱莉穿雨衣的样子"
#: 判定角色 = 流萤（与穿着归属「爱莉希雅」冲突 → C105 断言不注入）
WEAR_JUDGE_OTHER_REQUEST = "画流萤吧"
#: 判定角色 = 爱莉希雅（与穿着归属一致 → C106 断言正常注入）
WEAR_JUDGE_SAME_REQUEST = "画爱莉希雅吧"
#: 内置表里「婚纱 / 婚服 / 结婚礼服 / 新娘装」映射到的那组关键词（C105/C106）
WEDDING_KEYWORDS = ("wedding dress", "bridal veil", "white dress")

# ── 1.0.30（C104 端到端改法 + 同义写法 + 用户最新点名）用例的夹具 ──────
#: C104 的单元级样例：**去掉「会」这个虚词之后的句式抽取**（见 C104 的说明）。
#: 「我想看学生」这类**以键结尾**的写法照旧命中（相等 / 后缀口径一个字没改）。
WEAR_COUNCIL_UNIT_CASES = {
    "学生": True,
    "学生装": True,
    "学生服": True,  # 1.0.30 新补的同义写法（本身就是键）
    "学生制服": True,  # 1.0.30 新补
    "校服外套": True,  # 1.0.30 新补
    "婚纱照": True,  # 1.0.30 新补
    "居家服": True,
    "泳衣": True,  # 本身是键
    "泳衣套装": True,  # 1.0.30 新补
    "睡衣套装": True,  # 1.0.30 新补
    "婚服": True,
    "和服": True,
    "和服裙": True,  # 1.0.30 新补
    "女仆装束": True,  # 1.0.30 新补
    "护士服装": True,  # 1.0.30 新补
    "西装外套": True,  # 1.0.30 新补
    "运动服装": True,  # 1.0.30 新补
    "旗袍裙": True,  # 1.0.30 新补
    "居家服装": True,  # 1.0.30 新补
    "婚纱裙": True,  # 1.0.30 新补
    "穿学生装": True,  # 后缀命中
    "我想看你的学生": True,  # 后缀命中
    "男生学生": True,  # 后缀命中（"X + 键"的复合写法保留）
    "学生会": False,  # 只有前缀含「学生」→ 不算命中
    "学生时代": False,  # 同上
    "我想看学生会": False,  # 把整句当候选传进来时同样不命中
    "画花火吧": False,
}
#: C107/C108：用户**最新一条消息**里点名的角色（穿着那句话也提到爱莉，但与角色判定无关）
LATEST_NAMED_OUTFIT_REQUEST = "画爱莉的婚纱～"
#: C107/C108 里"上一条"那句（点的是流萤 —— 1.0.29 的粘性会把它一直按到下一张图）
LATEST_NAMED_STICKY_REQUEST = "那画流萤吧"
#: C108：画面描述里**已经写着流萤**的标签（1.0.19 的期望集合对账要替换掉它）
FIREFLY_SCENE = f"{TAG_FIREFLY}, 银白色长发的少女站在镜头前，一身蓝裙，手里握着长杖"


# ── 1.0.36（分区覆盖：**实验性功能**，默认关闭）用例的夹具 ────────────────
#
# 需求原话：「可以指定「头/头发/眼睛 用 A 的、身子/体型 用 B 的」这类分区拼接；
# 也允许单分区（只换头）」，入口语言至少覆盖下面三句，识别不到就什么都不做。
# 角色表用 RS3（流萤 / 爱莉希雅），分区特征词全部走**独立条目列表**
# ``[region_override] entries``（不是 [roster]、也不是 [outfits]）。

#: C119：一句里两个分区（`头是…的，身子是…的`）
REGION_SWAP_PHRASE = "头是流萤的，身子是爱莉的"
#: C121：单分区（只换头）
REGION_SINGLE_PHRASE = "头换成流萤"
#: C119：「用…的」+「配…的」两个归属句式拼在一句里
REGION_COMPOSE_PHRASE = "用爱莉的身子配流萤的头"
#: C122：三句**都不该触发**（分区词后面不是归属句式 / 没有分区词 / 没有归属句式）
REGION_FALSE_PHRASES = ("流萤的头饰很好看", "头疼", "画流萤")

#: 流萤 / 爱莉希雅在两个分区里的特征词（负向要能看出"被换掉的那个人的原词"）
REGION_FIREFLY_HEAD = ("silver hair", "long hair", "blue eyes", "headband")
REGION_FIREFLY_BODY = ("slender", "flat chest", "pale skin")
REGION_FIREFLY_OUTFIT = ("white dress",)
REGION_ELYSIA_HEAD = ("pink hair", "purple eyes", "elf ears")
REGION_ELYSIA_BODY = ("curvy", "tall", "soft skin")
REGION_ELYSIA_OUTFIT = ("pink elf dress",)

#: C124：`stylized_swap` 打开时才出现的「接缝 / 过渡」类描述词
REGION_STYLIZED_TERMS = ("stitched neck", "visible seam", "head slightly tilted")
#: C120/C124：`anti_mix` 多角色时追加的防串味负向词
REGION_ANTI_MIX_TERMS = ("mixed attributes", "blended eyes")


def region_entries(
    *,
    include_firefly: bool = True,
    include_elysia: bool = True,
) -> list:
    """造 ``[region_override] entries``：流萤 + 爱莉希雅两条（可各关掉一条）。

    用**真的配置节类**（``RegionEntrySection``）而不是字典：这样用例同时钉住
    「配置段 / 条目列表的形状就是插件读的那一份」（字段名、默认值都一致）。
    """
    from ..config import RegionEntrySection

    entries = []
    if include_firefly:
        entries.append(
            RegionEntrySection(
                name="流萤",
                head=", ".join(REGION_FIREFLY_HEAD),
                body=", ".join(REGION_FIREFLY_BODY),
                outfit=", ".join(REGION_FIREFLY_OUTFIT),
            )
        )
    if include_elysia:
        entries.append(
            RegionEntrySection(
                name="爱莉希雅",  # 角色表里的**正式名**；用户写别名「爱莉」也归一到它
                head=", ".join(REGION_ELYSIA_HEAD),
                body=", ".join(REGION_ELYSIA_BODY),
                outfit=", ".join(REGION_ELYSIA_OUTFIT),
            )
        )
    return entries


# ── 1.0.34（框架运行时兼容 shim）用例的离线桩 ───────────────────────────
#
# 三条约束：① **不真导入**宿主模块 ``src.core.utils.llm_tool_call``（``compat``
# 留了 ``host_module`` 接缝，这里换成假模块，取不到真框架也能跑）；② 假模块里的
# ``run_tool_call`` 是**本文件里的真函数**，于是 ``inspect.getsource`` 读得到源码
# —— C116 靠"源码里有没有补丁标记"判定，用 lambda / exec 造的函数读不到源码；
# ③ 事件总线用 :class:`FakeEventBus`（形状对齐真总线的 ``get_subscribers`` /
# ``publish``），只记录"发布了什么、谁改了 args"。

#: 假宿主函数收到的 ``calls``（每次调用一行，按先后顺序；用列表避免写 global）
_STUB_CALLS: list[list] = []
#: C118 ②：让假宿主函数在**补完事件之后**抛异常（验证异常不吞、不打乱后续调用）
_STUB_STATE: dict = {"fail": False}


@dataclasses.dataclass(frozen=True, slots=True)
class FakeToolCall:
    """宿主 ``ToolCall`` 的替身（真实实现就是 frozen dataclass + slots）。"""

    id: str | None
    name: str
    args: dict


class FakeUsableAction:
    """宿主组件类替身（action 分支）：用例只用到 ``get_signature()``。"""

    @classmethod
    def get_signature(cls) -> str:
        """与真实 Action 同形：``插件名:action:动作名``。"""
        return "nai_artist:action:share_photo"


class FakeUsableTool:
    """宿主组件类替身（tool 分支）：用例只用到 ``get_signature()``。"""

    @classmethod
    def get_signature(cls) -> str:
        """与真实 Tool 同形：``插件名:tool:工具名``。"""
        return "image_generator_plugin-neo:tool:draw_image"


class FakeUsableMap:
    """宿主 ``ToolRegistry`` 的替身：只需要 ``get(name)``。"""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    def get(self, name: str):
        """按调用名取组件类（取不到返回 None，与真实注册表一致）。"""
        return self._mapping.get(name)


class FakeEventType:
    """宿主 ``EventType`` 的替身：只用到两个执行前事件名。"""

    BEFORE_ACTION_CALL = "before_action_call"
    BEFORE_TOOL_CALL = "before_tool_call"


class FakeEventBus:
    """宿主事件总线的替身：``get_subscribers`` + 链式 ``publish``。

    - ``received``：每次 publish 的**入参**（用例据它断言事件里到底带了什么）；
    - ``published``：每次 publish 的**最终** params（订阅者改写之后的那一份）。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list] = {}
        self.received: list[tuple[str, dict]] = []
        self.published: list[tuple[str, dict]] = []

    def subscribe(self, event_name: str, handler) -> None:
        """登记一个订阅者（与真总线的 ``subscribe`` 同形，忽略优先级）。"""
        self._subscribers.setdefault(event_name, []).append(handler)

    def clear(self) -> None:
        """清空订阅者（C117 用它验证"没有订阅者时零开销"）。"""
        self._subscribers.clear()

    def get_subscribers(self, event_name: str) -> list:
        """返回订阅者列表（与真总线一致：没有订阅者时返回空列表）。"""
        return list(self._subscribers.get(event_name, ()))

    async def publish(self, event_name: str, params: dict):
        """按订阅顺序链式发布（真总线还做 key 集合校验，这里只保留语义）。

        订阅者**同步 / 异步都支持** —— 与真总线的 ``_execute_handler`` 同一口径
        （插件侧的真实订阅者是 ``async def execute``）。
        """
        self.received.append((str(event_name), dict(params)))
        current = dict(params)
        expected = set(current)
        for handler in self.get_subscribers(event_name):
            outcome = handler(event_name, dict(current))
            if inspect.isawaitable(outcome):
                outcome = await outcome
            _decision, next_params = outcome
            if isinstance(next_params, dict) and set(next_params) == expected:
                current = next_params
        self.published.append((str(event_name), dict(current)))
        return None, current


def _fake_event_type(name: str):
    """假 ``EventType`` 查表（只认两个执行前事件名）；不认识就返回 ``None``。"""
    return getattr(FakeEventType, name, None)


class FakeHost:
    """上下文管理器：把 ``compat`` 的三个接缝换成离线桩（C116~C118 用）。"""

    def __init__(self, module: types.ModuleType) -> None:
        self.module = module
        self.bus = FakeEventBus()
        self._saved: list = []

    def __enter__(self) -> Self:
        from .. import compat

        self._saved = [compat.host_module, compat.event_bus, compat.event_type]

        def fake_host_module():
            return self.module

        def fake_event_bus():
            return self.bus

        compat.host_module = fake_host_module  # type: ignore[assignment]
        compat.event_bus = fake_event_bus  # type: ignore[assignment]
        compat.event_type = _fake_event_type  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info) -> bool:
        from .. import compat

        if self._saved:
            compat.host_module, compat.event_bus, compat.event_type = self._saved
        return False


async def _stub_patched_run_tool_call(**_kwargs):
    """假宿主 ``run_tool_call``：**已经打过静态补丁**（C116：识别到就不再包装）。

    真实补丁的源码里同时出现下面三类标记，本替身原样带着它们 ——
    ``compat.looks_statically_patched`` 就是对 ``inspect.getsource`` 做这三类子串匹配。
    """
    event_names = ("BEFORE_ACTION_CALL", "BEFORE_TOOL_CALL")
    publish_call = "event_bus.publish(event_name, params)"
    return {"events": event_names, "publish": publish_call, "args": dict(_kwargs)}


async def _stub_plain_run_tool_call(
    *,
    calls,
    response=None,
    usable_map=None,
    trigger_msg=None,
    plugin=None,
    stream_id=None,
    resolve_component_plugin=None,
    logger_name="chatter",
    display_name="",
    task_observer=None,
):
    """假宿主 ``run_tool_call``：**未打补丁**（C117/C118：包装后才会有事件）。

    刻意与真实函数同形（全关键字参数 + 协程），并记录收到的 ``calls`` ——
    用例据最后一行断言"订阅者对 args 的修改被写回并传给了原函数"。
    注意：本函数的源码里**不能**出现补丁标记，否则会被判定成"已打补丁"。
    """
    _STUB_CALLS.append(list(calls))
    if _STUB_STATE["fail"]:
        raise RuntimeError("宿主原函数抛出的异常")
    return [(True, True)]


def _noop_subscriber(_event_name: str, params: dict):
    """假订阅者：什么都不改（返回同一份 params，key 集合不变）。"""
    return EventDecision.SUCCESS, params


def _fake_host_module(func=None) -> types.ModuleType:
    """造一个假宿主模块（``src.core.utils.llm_tool_call`` 的替身）。"""
    module = types.ModuleType("cpg_fake_llm_tool_call")
    if func is not None:
        module.run_tool_call = func
    return module


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

    # ── C83 infer_unnamed=false（默认）→「流萤的那一套皮肤」什么都不做（1.0.23）──
    # 需求原话：「『流萤的那一套皮肤』这种没点名字的说法不会注入……做一个（开关），
    # 默认关就行，开了可以让模型去查（皮肤名字）然后注入提示词」。
    # 默认关时必须与 1.0.22 **逐字一致**：皮肤解析与皮肤推断两条路都是 0 次模型调用、
    # 没有皮肤关键词注入、日志里没有 `皮肤推断`，内容与「[outfits] 全关」时一字不差。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        cfg_off = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=False)
        cfg_off.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=UNNAMED_REQUEST)
        params["stream_id"] = "s-c83"
        history = [history_message("我们来聊流萤吧", 1.0)]
        with (
            FakeOutfit(OUTFIT_PAYLOAD, infer_payload=INFER_PAYLOAD) as stub,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        with FakeHistory(history):
            out_off = await fire(cfg_off, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            stub.calls == []  # ★ 解析那条路 0 次
            and stub.infer_calls == []  # ★ 推断那条路也 0 次
            and all(keyword not in c for keyword in (*PINK_KEYWORDS, *INFER_KEYWORDS))
            and "皮肤/时装" not in c
            and "皮肤推断" not in records  # ★ 没有任何推断相关日志
            and "皮肤解析" not in records
            and "建议加入皮肤表" not in records
            and c == out_off["args"]["content"]  # ★ 与 [outfits] 全关时逐字一致
            and "流萤" in c
            and TAG_FIREFLY in c  # 角色注入照旧
        )
        check(
            "C83 infer_unnamed=false → 0 次调用、无皮肤注入、无推断日志、内容一致",
            ok,
            f"content = {c!r}\n        关掉 [outfits] 的 content = "
            f"{out_off['args']['content']!r}\n        解析调用 = {stub.calls!r} / "
            f"推断调用 = {stub.infer_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C83 infer_unnamed=false → 0 次调用、无皮肤注入、无推断日志、内容一致",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C84 infer_unnamed=true → 模型推断出「流萤的那一套」并注入（1.0.23）─────
    # 开了开关才做这件事：先从用户消息里认出「流萤的那一套皮肤」里的角色（**角色表内**），
    # 再让模型给出该角色最知名的一套（假推断返回 inferred=true 的合法 payload，
    # 关键词是小写 ASCII），注入方式与 1.0.22 **完全相同**（角色在前、皮肤在后）；
    # 日志必须标注来源「模型推断」，并照旧给一条「建议加入皮肤表」供用户核对。
    try:
        cfg = make_config(
            roster=RS3,
            outfits=RS_OUTFIT,
            outfits_enabled=True,
            outfits_infer_unnamed=True,
        )
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=UNNAMED_REQUEST)
        params["stream_id"] = "s-c84"
        with (
            FakeOutfit(OUTFIT_PAYLOAD, infer_payload=INFER_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊流萤吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword in c for keyword in INFER_KEYWORDS)  # ★ 推断出的关键词进来了
            and "流萤·花嫁" in c  # 推断出的皮肤名也在（可核对）
            and "皮肤/时装" in c
            and "流萤" in c
            and TAG_FIREFLY in c  # 角色照旧
            and c.index("流萤") < c.index("流萤·花嫁")  # ★ 角色在前、皮肤在后
            and stub.infer_calls == ["流萤"]  # ★ 只问一次「推断」
            and stub.calls == []  # 用户没说皮肤名 → 解析那条路一次都没问
            and "皮肤推断(模型): 流萤 → 流萤·花嫁 → firefly's outfit, white dress"
            "（来源=模型推断，confidence=0.8）" in records  # ★ 来源标注
            and "建议加入皮肤表" in records  # ★ 只打日志的建议行
            and "皮肤解析(模型)" not in records
        )
        check(
            "C84 infer_unnamed=true → 模型推断并注入（日志标注来源=模型推断）",
            ok,
            f"content = {c!r}\n        推断调用 = {stub.infer_calls!r} / "
            f"解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C84 infer_unnamed=true → 模型推断并注入（日志标注来源=模型推断）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C85 infer_unnamed=true + 假推断 unknown=true → 什么都不注入（1.0.23）────
    # 校验口径与 1.0.22 完全一致：模型说不知道（或 JSON 坏 / confidence 低于 0.5 /
    # 没有可用关键词）时**什么都不注入** —— 绝不瞎编一套衣服。
    try:
        cfg = make_config(
            roster=RS3,
            outfits=RS_OUTFIT,
            outfits_enabled=True,
            outfits_infer_unnamed=True,
        )
        cfg.general.history_cache_ttl = 0.0
        cfg_off = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg_off.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=UNNAMED_REQUEST)
        params["stream_id"] = "s-c85"
        history = [history_message("我们来聊流萤吧", 1.0)]
        with (
            FakeOutfit(OUTFIT_PAYLOAD, infer_payload={"unknown": True}) as stub,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        with FakeHistory(history):
            out_off = await fire(cfg_off, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword not in c for keyword in INFER_KEYWORDS)  # ★ 没有推断出的关键词
            and "流萤·花嫁" not in c
            and "皮肤/时装" not in c
            and stub.infer_calls == ["流萤"]  # 问过一次（模型说不知道）
            and stub.calls == []
            and "皮肤推断(LLM)失败" in records  # ★ 失败也标明是「推断」那条路
            and "unknown" in records  # ★ 失败原因写清楚
            and "建议加入皮肤表" not in records
            and c == out_off["args"]["content"]  # 与不开这一维时逐字一致
            and "流萤" in c
            and TAG_FIREFLY in c
        )
        check(
            "C85 假推断 unknown=true → 不注入、日志写明失败原因",
            ok,
            f"content = {c!r}\n        不开这一维的 content = "
            f"{out_off['args']['content']!r}\n        推断调用 = {stub.infer_calls!r}\n"
            f"        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C85 假推断 unknown=true → 不注入、日志写明失败原因",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C86（①的验证）皮肤名不再被当成表外角色名（1.0.23）────────────────────
    # 用户说「画粉色妖精小姐这套」时，「粉色妖精小姐」是**一套皮肤**（皮肤表里有），
    # 不是角色表外的角色名：皮肤照旧注入（表命中、0 次皮肤模型调用），
    # 而角色那一侧**不该**因此做任何解析 —— 1.0.21 会给表外名字多问一次模型，
    # `fix` 模式下还可能把画面里**用户这批消息没提过**的表内角色标签替换 / 删掉
    # （这正是 1.0.23 要修的坑，所以这条用例用 fix 模式 + 0.9 把握的假解析结果）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"  # 最能暴露风险的模式
        raw = f"{TAG_ELYSIA}, 粉色长发的少女站在花海中"  # 画面里的表内角色（用户这次没提过）
        params = natural_params(raw, text="画粉色妖精小姐这套")
        params["stream_id"] = "s-c86"
        with (
            FakeResolve(KLEIDRA_PAYLOAD) as resolve_stub,
            FakeOutfit(OUTFIT_PAYLOAD, infer_payload=INFER_PAYLOAD) as outfit_stub,
            FakeHistory([history_message("我们说点别的吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "pink elf dress" in c  # ★ 皮肤照旧注入（表命中）
            and "fairy wings" in c
            and "粉色妖精小姐" in c
            and "皮肤/时装" in c
            and "皮肤解析(表)" in records
            and outfit_stub.calls == []  # ★ 表命中：0 次皮肤模型调用
            and outfit_stub.infer_calls == []  # 也没走「没说名字」那条推断路
            and resolve_stub.calls == []  # ★ 角色侧一次解析都没问
            and "角色解析(模型)" not in records
            and "角色解析(LLM)" not in records
            and "用户点名「粉色妖精小姐」" not in records  # ★ 没被当成表外角色名
            and TAG_ELYSIA in c  # 画面里的角色标签原样保留（fix 没动它）
            and c.startswith(raw)
        )
        check(
            "C86 皮肤名不被当成表外角色名（表命中、0 调用、角色侧不解析）",
            ok,
            f"content = {c!r}\n        角色解析调用 = {resolve_stub.calls!r} / "
            f"皮肤解析调用 = {outfit_stub.calls!r} / "
            f"推断调用 = {outfit_stub.infer_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C86 皮肤名不被当成表外角色名（表命中、0 调用、角色侧不解析）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C87（②的验证）作品名去重：game_cn 里已有英文名时不再拼第二遍（1.0.23）──
    # 实测（C72 的日志）：模型把英文作品名也塞进了 `game_cn`：
    #   game_cn="崩坏：星穹铁道 (Honkai: Star Rail)"、game_en="Honkai: Star Rail"
    # 1.0.22 照旧拼一次 → 「… (Honkai: Star Rail) (Honkai: Star Rail)」。
    # 1.0.23 起**生成的中文说明与日志**里 `Honkai: Star Rail` 只出现一次；
    # 原始字段（name_cn / name_en / game_cn / game_en）一个字都没动。
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
                    "stream_id": "s-c87",
                },
            )
        c = out["args"]["content"]
        records = logs.records_text()
        # 「生成的中文说明」= natural 模式那条追加句；画面描述原文里本来就有一个
        # `(Honkai: Star Rail)`（那是调用方的旧标签残渣，不是这次拼出来的），
        # 所以只对**生成的那一段**与日志行计数
        clause = c[c.index("（画面角色") :] if "（画面角色" in c else ""
        game_lines = [line for line in records.splitlines() if "作品《" in line]
        ok = (
            "崩坏：星穹铁道 (Honkai: Star Rail)" in c  # 去重后的作品名
            and "(Honkai: Star Rail) (Honkai: Star Rail)" not in c  # ★ 不再拼第二遍
            and clause.count("Honkai: Star Rail") == 1  # ★ 中文说明里只出现一次
            and "出自游戏《崩坏：星穹铁道 (Honkai: Star Rail)》" in c
            and len(game_lines) >= 1  # 日志里那条依据行确实在
            and all(line.count("Honkai: Star Rail") == 1 for line in game_lines)  # ★ 同样只一次
            and TAG_KLEIDRA in c
            and stub.calls == ["刻律德拉"]
        )
        check(
            "C87 作品名去重 → 中文说明与日志里 Honkai: Star Rail 只出现一次",
            ok,
            f"content = {c!r}\n        中文说明 = {clause!r}\n"
            f"        含作品名的日志行 = {game_lines!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C87 作品名去重 → 中文说明与日志里 Honkai: Star Rail 只出现一次",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C88（豁免真的生效）「画「粉色妖精小姐」这套」→ 皮肤注入、角色侧不解析 ──
    # 为什么单独一条：C86 的写法「画粉色妖精小姐这套」在 1.0.22 里其实会被
    # `_SENTENCE_NAME_STOP_CHARS` 的「这」拦掉（抓到的候选是「粉色妖精小姐这套」），
    # 所以那条用例**证明不了**豁免真的在起作用。这里换一个 1.0.20 确实会抓到的写法
    # （引号模式），它同时命中皮肤表（皮肤照旧注入，0 次调用）—— 于是「要不要为这个
    # 表外名字解析一次」这条路上只有豁免能拦住它：不豁免就会多问一次模型，
    # fix 模式下还会把画面里的 elysia 标签换成解析出来的 kleidra。
    # 单元级顺带钉住：同一个函数**不传** exclude 时确实抓得到（否则整条用例是空转）。
    try:
        from ..character import extract_outside_names, parse_roster

        request = "画「粉色妖精小姐」这套"
        raw = f"{TAG_ELYSIA}, 粉色长发的少女站在花海中"
        extracted = extract_outside_names(request, parse_roster(RS3))
        exempted = extract_outside_names(
            request, parse_roster(RS3), exclude=["粉色妖精小姐"]
        )
        # 豁免**不该**把真的表外角色名一起吃掉：同一条消息里另有一个真表外名字时，
        # 它必须照旧被抓到（C68~C71 那类场景依赖它）
        mixed = extract_outside_names(
            "画「粉色妖精小姐」这套，顺便画「刻律德拉」",
            parse_roster(RS3),
            exclude=["粉色妖精小姐"],
        )
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        params = natural_params(raw, text=request)
        params["stream_id"] = "s-c88"
        with (
            FakeResolve(KLEIDRA_PAYLOAD) as resolve_stub,
            FakeOutfit(OUTFIT_PAYLOAD) as outfit_stub,
            FakeHistory([history_message("我们说点别的吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            extracted == ["粉色妖精小姐"]  # ★ 提取链真的会抓到它（不是空转）
            and exempted == []  # ★ 传了 exclude 就抓不到
            and mixed == ["刻律德拉"]  # ★ 真表外角色名不会被一起豁免
            and "pink elf dress" in c  # 皮肤照旧注入
            and "粉色妖精小姐" in c
            and outfit_stub.calls == []
            and outfit_stub.infer_calls == []
            and resolve_stub.calls == []  # ★ 没有把它当角色去解析
            and "角色解析(模型)" not in records
            and "角色解析(LLM)" not in records
            and "用户点名「粉色妖精小姐」" not in records
            and "皮肤名豁免" in records  # ★ 日志里能看到"确实豁免了"
            and TAG_ELYSIA in c  # 画面里的角色标签没被动过
        )
        check(
            "C88 引号写法的皮肤名同样被豁免（豁免真的生效，皮肤照常注入）",
            ok,
            f"提取(不豁免) = {extracted!r} / 提取(豁免) = {exempted!r} / "
            f"混合消息(豁免后) = {mixed!r}\n"
            f"        content = {c!r}\n        角色解析调用 = {resolve_stub.calls!r}\n"
            f"        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C88 引号写法的皮肤名同样被豁免（豁免真的生效，皮肤照常注入）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C89 泛指的穿着类型（内置表命中）→ 零模型调用，注入穿着关键词（1.0.28）──
    # 需求原话：「当说的是『我想看你的 xxx』（如**学生**、**居家**、**泳衣**），
    # 判断**不是皮肤**，转成"穿对应的衣服"；否则衣服这个产出有时相当于黑箱，
    # 只能由 bot 决定 —— 要能**看到注入了什么样的衣服**。」
    # 「学生装」不在皮肤表里（皮肤表里只有「粉色妖精小姐」），但命中**内置穿着类型
    # 表** → 直接用表里的关键词，一次模型都不问；日志必须能看出"命中哪张表、
    # 命中哪个词、注入了什么"，注入措辞必须是「穿着」而不是「皮肤/时装」。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=WEAR_STUDENT_REQUEST)
        params["stream_id"] = "s-c89"
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword in c for keyword in STUDENT_KEYWORDS)  # ★ 穿着关键词进来了
            and "（穿着：“学生装”：school uniform, pleated skirt）" in c  # ★ 措辞是"穿着"
            and "皮肤/时装" not in c  # ★ 不与皮肤说明混淆
            and all(keyword not in c for keyword in PINK_KEYWORDS)  # 皮肤表内容一个字没进来
            and "粉色妖精小姐" not in c
            and "爱莉希雅" in c  # ★ 角色注入照旧
            and TAG_ELYSIA in c
            and stub.calls == []  # ★ 皮肤模型 0 次
            and stub.wear_calls == []  # ★ 穿着模型 0 次（内置表命中）
            and stub.infer_calls == []
            and "穿着类型解析(内置表): 学生装 → school uniform, pleated skirt" in records
            and "已追加穿着（share_visual / natural）: 学生装 → school uniform, pleated skirt"
            in records
            and "皮肤解析" not in records
        )
        check(
            "C89 学生装 → 内置穿着类型表命中，零模型调用并注入",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r} / 穿着调用 = "
            f"{stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C89 学生装 → 内置穿着类型表命中，零模型调用并注入",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C90 内置表的同义写法（泳衣 / 居家服）→ 各自命中并注入（1.0.28）────────
    # 需求追加：「内置穿着类型表要覆盖同义词，避免常见词白跑一次模型」——
    # 「泳衣」「居家服」都不在皮肤表里，但分别命中内置表的「泳衣」「居家服」
    # （1.0.28 靠包含匹配；**1.0.29 起是相等命中** —— 表里本来就有这两个键，
    #   所以这条用例的口径没变，变的只是匹配规则）。
    # 断言一个字都没动（1.0.29 只改了上面这行注释里的规则说明）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        runs: list[tuple[str, bool, str]] = []
        for label, request, keywords in (
            ("泳衣", WEAR_SWIM_REQUEST, SWIM_KEYWORDS),
            ("居家服", WEAR_HOME_REQUEST, HOME_KEYWORDS),
        ):
            params = natural_params(PINK_SCENE, text=request)
            params["stream_id"] = f"s-c90-{label}"
            with (
                FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
                FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
                LogCapture() as logs,
            ):
                out = await fire(cfg, params)
            content = out["args"]["content"]
            records = logs.records_text()
            hit = (
                all(keyword in content for keyword in keywords)  # ★ 关键词进来了
                and f"穿着类型解析(内置表): {label} → {', '.join(keywords)}" in records
                and f"（穿着：“{label}”" in content
                and "爱莉希雅" in content  # 角色注入照旧
                and "穿着解析(模型)" not in records  # 没有白跑一次模型
                and all(keyword not in content for keyword in PINK_KEYWORDS)
                and stub.calls == []
                and stub.wear_calls == []
            )
            runs.append((label, hit, f"content={content!r} 日志={records!r}"))
        check(
            "C90 泳衣 / 居家服 → 内置表同义词各自命中并注入（0 次模型调用）",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C90 泳衣 / 居家服 → 内置表同义词各自命中并注入（0 次模型调用）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C91（防误判）皮肤表里的「学生会长制服」→ 仍按**皮肤名**走（1.0.28）────
    # 冲突矩阵第 1 条：**皮肤表命中优先**。皮肤表里的「学生会长制服」本身也含穿着
    # 特征字（学生 / 制服），若优先级反了就会被拆成"学生装"、用错关键词。
    # 单元级先钉住"候选确实同时是穿着特征词"（否则这条用例是空转）。
    try:
        from ..outfit import find_wear_type, looks_like_wear

        cfg = make_config(roster=RS3, outfits=COUNCIL_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=COUNCIL_REQUEST)
        params["stream_id"] = "s-c91"
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            find_wear_type("学生会长制服") is not None  # ★ 它确实"像穿着"（不是空转）
            and looks_like_wear("学生会长制服")
            and "student council uniform" in c  # ★ 走的是皮肤表的关键词
            and "学生会长制服" in c
            and "school uniform" not in c  # ★ 没有被拆成"学生装"
            and "（穿着：" not in c
            and "皮肤解析(表)" in records
            and "穿着类型解析" not in records  # ★ 没有走穿着那条路
            and stub.calls == []
            and stub.wear_calls == []
        )
        check(
            "C91 皮肤表里的「学生会长制服」仍按皮肤名走（含穿着特征字也不拆）",
            ok,
            f"content = {c!r}\n        穿着调用 = {stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C91 皮肤表里的「学生会长制服」仍按皮肤名走（含穿着特征字也不拆）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C92 [outfits] enabled=false → 穿着整条链路零开销（1.0.28）────────────
    # 冲突矩阵第 10 条：关掉时**不注入、不调模型、日志里没有 `穿着类型解析`**，
    # 但 1.0.26 那条**纯字符串识别**（只为把皮肤名从"表外角色名"里豁免掉）仍在。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=False)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=WEAR_STUDENT_REQUEST)
        params["stream_id"] = "s-c92"
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            stub.calls == []  # ★ 0 次模型调用
            and stub.wear_calls == []
            and stub.infer_calls == []
            and "穿着类型解析" not in records  # ★ 没有穿着日志
            and "已追加穿着" not in records
            and "（穿着：" not in c  # ★ 没有注入
            and all(keyword not in c for keyword in STUDENT_KEYWORDS)
            and all(keyword not in c for keyword in PINK_KEYWORDS)
            and "爱莉希雅" in c  # 角色注入照旧
            and "皮肤/时装=仅豁免识别（outfits.enabled=false）" in records  # ★ 豁免识别仍在
        )
        check(
            "C92 outfits.enabled=false → 穿着零开销、不注入、豁免识别仍在",
            ok,
            f"content = {c!r}\n        解析调用 = {stub.calls!r} / 穿着调用 = "
            f"{stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C92 outfits.enabled=false → 穿着零开销、不注入、豁免识别仍在",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C93 内置表与皮肤表都没有的穿着词（雨衣）→ 穿着模型解析（1.0.28 第 ④ 档）──
    # 需求追加：「内置词表和皮肤表都没有的'穿着词'，也要自动用大模型找出提示词并注入」。
    # 「雨衣」含穿着特征字「衣」→ 走**穿着专用提示词**（假模型返回 raincoat / hood），
    # 恰好问一次模型；皮肤那条路一次都不问，日志是 `穿着解析(模型)` 而不是皮肤解析。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=WEAR_REQUEST)
        params["stream_id"] = "s-c93"
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword in c for keyword in WEAR_KEYWORDS)  # ★ 模型给的穿着关键词进来了
            and "（穿着：“雨衣”：raincoat, hood）" in c
            and "皮肤/时装" not in c
            and all(keyword not in c for keyword in PINK_KEYWORDS)
            and "爱莉希雅" in c
            and stub.wear_calls == ["雨衣"]  # ★ 穿着那条路恰好问一次
            and stub.calls == []  # ★ 皮肤那条路一次都没问
            and stub.infer_calls == []
            and "穿着解析(模型): 雨衣 → raincoat, hood" in records
            and "建议加入穿着类型表：雨衣 | raincoat, hood" in records
            and "皮肤解析" not in records
            and "已追加穿着（share_visual / natural）: 雨衣 → raincoat, hood" in records
        )
        check(
            "C93 雨衣（两张表都没有）→ 穿着模型解析注入、恰好 1 次调用",
            ok,
            f"content = {c!r}\n        穿着调用 = {stub.wear_calls!r} / 皮肤调用 = "
            f"{stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C93 雨衣（两张表都没有）→ 穿着模型解析注入、恰好 1 次调用",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C94 穿着模型解析返回 unknown=true → 什么都不注入（1.0.28 第 ④ 档降级）──
    # 校验口径与皮肤解析完全相同：模型说"这不是穿着 / 我不知道"（或 JSON 坏 /
    # confidence 低于 0.5 / 没有可用关键词）时**什么都不注入** —— 绝不瞎编一套衣服；
    # 内容与关掉 [outfits] 时**逐字一致**。
    try:
        cfg_on = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg_on.general.history_cache_ttl = 0.0
        cfg_off = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=False)
        cfg_off.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=WEAR_REQUEST)
        params["stream_id"] = "s-c94"
        history = [history_message("我们来聊爱莉希雅吧", 1.0)]
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload={"unknown": True}) as stub,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out_on = await fire(cfg_on, params)
        with FakeHistory(history):
            out_off = await fire(cfg_off, params)
        c = out_on["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword not in c for keyword in WEAR_KEYWORDS)  # ★ 没有穿着关键词
            and "（穿着：" not in c
            and "皮肤/时装" not in c
            and all(keyword not in c for keyword in PINK_KEYWORDS)
            and stub.wear_calls == ["雨衣"]  # 问过一次（模型说不知道）
            and stub.calls == []
            and "穿着解析(LLM)失败" in records  # ★ 失败也标明是「穿着」那条路
            and "unknown" in records  # ★ 失败原因写清楚
            and "建议加入穿着类型表" not in records
            and c == out_off["args"]["content"]  # ★ 与关掉 [outfits] 时逐字一致
            and "爱莉希雅" in c
            and TAG_ELYSIA in c
        )
        check(
            "C94 穿着解析 unknown=true → 不注入、日志写明失败原因、内容与关掉时一致",
            ok,
            f"content = {c!r}\n        关掉 [outfits] 的 content = "
            f"{out_off['args']['content']!r}\n        穿着调用 = {stub.wear_calls!r}\n"
            f"        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C94 穿着解析 unknown=true → 不注入、日志写明失败原因、内容与关掉时一致",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C95（冲突矩阵第 2 条）同一句里既有穿着词又有皮肤名 → 只处理一个 ────────
    # 规则：保持"一图一套"语义 —— 只处理**一个**（取最新一条消息里最长的候选，
    # 与 1.0.22 的皮肤选择规则一致），皮肤表命中仍然优先；**被忽略的候选写进日志**
    # （否则用户从画面里根本看不出"这次为什么加了这套衣服"）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        request = "穿学生装，再看粉色妖精小姐这套"
        params = natural_params(PINK_SCENE, text=request)
        params["stream_id"] = "s-c95"
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "pink elf dress" in c  # ★ 皮肤表命中 → 走 ①
            and "粉色妖精小姐" in c
            and all(keyword not in c for keyword in STUDENT_KEYWORDS)  # ★ 穿着词被忽略
            and "（穿着：" not in c
            and "皮肤解析(表)" in records
            and "穿着类型解析" not in records
            and "皮肤/穿着候选: 本次只处理「粉色妖精小姐」" in records  # ★ 一图一套
            and "被忽略的候选：学生装" in records  # ★ 被忽略的那个写明
            and stub.calls == []
            and stub.wear_calls == []
        )
        check(
            "C95 同句皮肤名 + 穿着词 → 只处理一个（皮肤优先），日志写明被忽略的候选",
            ok,
            f"content = {c!r}\n        穿着调用 = {stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C95 同句皮肤名 + 穿着词 → 只处理一个（皮肤优先），日志写明被忽略的候选",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C96（冲突矩阵第 3 条）穿着名与皮肤名一样进**豁免集** → 不被当成表外角色名 ──
    # 「画「雨衣」这套」的引号写法一定会被 1.0.20 的"表外点名"提取链抓到；若穿着名
    # 不进豁免集，1.0.21 的角色解析就会为此多问一次模型，`fix` 时还会去动画面里
    # 那个"用户这批消息没提过"的表内标签。这里断言：角色侧 0 次解析、日志有
    # `皮肤名豁免`、画面里的 elysia 标签原样保留，而穿着照常注入。
    try:
        from ..character import extract_outside_names, parse_roster

        request = "画「雨衣」这套"
        raw = f"{TAG_ELYSIA}, 粉色长发的少女站在雨中"
        extracted = extract_outside_names(request, parse_roster(RS3))
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"  # 最能暴露风险的模式
        params = natural_params(raw, text=request)
        params["stream_id"] = "s-c96"
        with (
            FakeResolve(KLEIDRA_PAYLOAD) as resolve_stub,
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as outfit_stub,
            FakeHistory([history_message("我们说点别的吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            extracted == ["雨衣"]  # ★ 提取链真的会抓到它（不是空转）
            and all(keyword in c for keyword in WEAR_KEYWORDS)  # ★ 穿着照常注入
            and outfit_stub.wear_calls == ["雨衣"]
            and resolve_stub.calls == []  # ★ 角色侧一次解析都没问
            and "角色解析(模型)" not in records
            and "角色解析(LLM)" not in records
            and "用户点名「雨衣」" not in records  # ★ 没被当成表外角色名
            and "皮肤名豁免" in records  # ★ 日志里能看到"确实豁免了"
            and TAG_ELYSIA in c  # 画面里的角色标签原样保留（fix 没动它）
            and c.startswith(raw)
        )
        check(
            "C96「画「雨衣」这套」→ 穿着名进豁免集（角色侧 0 次解析、穿着照常注入）",
            ok,
            f"提取(不豁免) = {extracted!r}\n        content = {c!r}\n"
            f"        角色解析调用 = {resolve_stub.calls!r} / 穿着调用 = "
            f"{outfit_stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C96「画「雨衣」这套」→ 穿着名进豁免集（角色侧 0 次解析、穿着照常注入）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C97（冲突矩阵第 4 条）候选命中**角色表** → 一律按角色处理，绝不当穿着 ────
    # 角色表里加一行「雨衣」：用户说「我想看你的雨衣」时那是**角色名**，
    # 不该被解析成穿着关键词（`extract_outfit_names` 的"不在角色表里"那道过滤）。
    try:
        roster_rain = (
            RS3
            + "\n"
            + r"雨衣 | 崩坏三 (Honkai Impact 3rd) | ame,雨衣 | ame \(honkai impact 3rd\)"
        )
        cfg = make_config(roster=roster_rain, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=WEAR_REQUEST)
        params["stream_id"] = "s-c97"
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "雨衣" in c  # ★ 作为**角色**补进来
            and all(keyword not in c for keyword in WEAR_KEYWORDS)  # ★ 没有穿着关键词
            and "（穿着：" not in c
            and "穿着类型解析" not in records
            and "穿着解析" not in records
            and stub.calls == []
            and stub.wear_calls == []
        )
        check(
            "C97 候选命中角色表（雨衣是角色名）→ 不注入任何穿着关键词",
            ok,
            f"content = {c!r}\n        穿着调用 = {stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C97 候选命中角色表（雨衣是角色名）→ 不注入任何穿着关键词",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C98（冲突矩阵第 5 条）穿着 ↔ 拆单：角色照旧按拆单规则，穿着照样注入 ─────
    # 与 C54 完全同一个拆单场景（历史里那条「本尊看看，然后穿着爱莉来看看～」），
    # 触发消息换成本次要穿的「学生装」：角色仍然由**拆单 → LLM 按画面判定**给出
    # （补花火、不补爱莉希雅），而穿着关键词**不参与角色判定**、照常追加。
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(
            roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True, use_llm=True
        )
        cfg.general.history_cache_ttl = 0.0
        cfg.general.description_first = False  # 旧条件全不成立 → 只能靠拆单强制问
        cfg.multi.llm_mode = "fallback"
        raw = "清晨室内明亮柔和的光线。白发少女刚起床，站在镜头前，粉色眼睛看向镜头"
        calls = {"n": 0}

        async def fake_extract(**kwargs):
            calls["n"] += 1
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
            history = [
                history_message("本尊看看，然后穿着爱莉来看看～", 1.0),
                history_message("穿学生装～", 0.5),
            ]
            with (
                FakeHistory(history),
                FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
                LogCapture() as logs,
            ):
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_photo",
                        "action_name": "share_photo",
                        "args": {"content": raw},
                        "message": FakeMessage("嗯～"),
                        "stream_id": "s-c98",
                    },
                )
        finally:
            handler_module.extract_characters_via_llm = original
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "花火" in c  # ★ 角色仍按拆单规则（LLM 按画面判定）
            and "爱莉希雅" not in c  # 拆单时绝不回退到上文那个爱莉希雅
            and all(keyword in c for keyword in STUDENT_KEYWORDS)  # ★ 穿着照常注入
            and "（穿着：“学生装”" in c
            and "拆单" in debugs
            and calls["n"] == 1  # 拆单强制走 LLM
            and stub.calls == []  # 穿着走内置表，一次模型都没问
            and stub.wear_calls == []
        )
        check(
            "C98 穿着 ↔ 拆单：角色按拆单规则（补花火），穿着关键词仍追加",
            ok,
            f"content = {c!r}\n        LLM 调用次数 = {calls['n']} / 穿着调用 = "
            f"{stub.wear_calls!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C98 穿着 ↔ 拆单：角色按拆单规则（补花火），穿着关键词仍追加",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C99（冲突矩阵第 6 条）fix 替换角色标签时**不得**动穿着段 ──────────────
    # 穿着关键词只过 `validate_outfit_keyword`（含 1.0.26 那条"拒收 \\(…\\)"），
    # 所以它不可能被当成角色标签；而且注入本来就排在两条一致性检查之后。
    # 这里用最狠的场景：画面里**已经有**一段穿着说明，用户点名了一个表外角色，
    # fix 把画面里的 hanabi 标签替换掉 —— 穿着段必须**原样保留**。
    try:
        cfg = make_config(roster=RS3, outfits="", outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        clause = "（穿着：“学生装”：school uniform, pleated skirt）"
        raw = f"{TAG_HANABI}, 银白色长发的少女站在镜头前，一身蓝裙。{clause}"
        params = natural_params(raw, text="扮演刻律德拉")
        params["stream_id"] = "s-c99"
        with (
            FakeResolve(KLEIDRA_PAYLOAD) as stub,
            FakeOutfit({"unknown": True}) as outfit_stub,
            FakeHistory([history_message("我们说点别的吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            TAG_HANABI not in c  # ★ 画面里的错误角色标签被替换掉
            and TAG_KLEIDRA in c
            and clause in c  # ★ 穿着段原样保留（一个字没动）
            and c.count("（穿着：“") == 1
            and "已追加穿着" not in records  # 已经有一段 → 同类幂等，不重复追加
            and "一致性检查=冲突已修正（fix）" in records
            and outfit_stub.wear_calls == []
        )
        check(
            "C99 fix 替换角色标签时穿着段原样保留（两者互不干扰）",
            ok,
            f"content = {c!r}\n        角色解析调用 = {stub.calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C99 fix 替换角色标签时穿着段原样保留（两者互不干扰）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C100（冲突矩阵第 7 条）幂等：同类不重复追加、**不同类可以共存** ────────
    # ① 画面里已经有**穿着**说明 + 这次又要穿「学生装」→ 不再追加第二段（多图轮
    #    的第 2、3 张就是这种入参）；
    # ② 画面里已经有**皮肤**说明 + 这次要穿「学生装」→ 穿着说明**照样追加**
    #    （两类各有各的标记，互不吞并）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        wear_clause = "（穿着：“学生装”：school uniform, pleated skirt）"
        skin_clause = "（皮肤/时装：“粉色妖精小姐”：pink elf dress, fairy wings）"
        runs: list[tuple[str, str, str, list[str], list[str]]] = []
        for label, raw in (
            ("同类", f"帮我画一张她战斗的样子。{wear_clause}"),
            ("不同类", f"帮我画一张她战斗的样子。{skin_clause}"),
        ):
            params = natural_params(raw, text=WEAR_STUDENT_REQUEST)
            params["stream_id"] = f"s-c100-{label}"
            with (
                FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
                FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
                LogCapture() as logs,
            ):
                out = await fire(cfg, params)
            runs.append(
                (
                    label,
                    out["args"]["content"],
                    logs.records_text(),
                    list(stub.calls),
                    list(stub.wear_calls),
                )
            )
        same_content, same_records = runs[0][1], runs[0][2]
        diff_content, diff_records = runs[1][1], runs[1][2]
        ok = (
            same_content.count("（穿着：“") == 1  # ★ 同类：没有追加第二段
            and "已追加穿着" not in same_records
            and same_content.startswith("帮我画一张她战斗的样子。")  # 原文没被改写
            and wear_clause in same_content  # 那一段还在原处
            and runs[0][3] == []  # 内置表命中：0 次模型调用
            and runs[0][4] == []
            and diff_content.count("（穿着：“") == 1  # ★ 不同类：穿着段照样追加
            and diff_content.count("（皮肤/时装：“") == 1  # 皮肤段原样留着
            and "已追加穿着" in diff_records
            and "school uniform" in diff_content
            and runs[1][3] == []
            and runs[1][4] == []
        )
        check(
            "C100 幂等：同类（穿着）不重复追加，不同类（皮肤+穿着）可以共存",
            ok,
            f"同类 content = {same_content!r}\n        不同类 content = {diff_content!r}\n"
            f"        同类日志 = {same_records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C100 幂等：同类（穿着）不重复追加，不同类（皮肤+穿着）可以共存",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C101（冲突矩阵第 8 条）穿着解析结果**不写回记忆库** ────────────────────
    # 记忆库写回那条通道写的是"某一套皮肤属于谁"（标题「皮肤指向：…」）；穿着关键词
    # 是**通用服装词**（雨衣 → raincoat），记下来既没有复用价值，也不该被当成"用户
    # 确认过的身份事实"。对照组：同一个配置下皮肤解析**照旧写回 1 次**（证明假传输层
    # 真的挂上了、写回通道是通的 —— 否则"0 次"毫无意义）。
    try:
        cfg = make_config(roster=RS3, outfits="", outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.memory.enabled = True
        cfg.memory.write_back = True
        cfg.outfits.write_back = True
        history = [history_message("我们来聊爱莉希雅吧", 1.0)]
        params_skin = natural_params(PINK_SCENE, text=PINK_REQUEST)
        params_skin["stream_id"] = "s-c101-skin"
        params_wear = natural_params(PINK_SCENE, text=WEAR_REQUEST)
        params_wear["stream_id"] = "s-c101-wear"
        with (
            FakeMemory() as mem,
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out_skin = await fire(cfg, params_skin)
            skin_posts = len(mem.post)
            out_wear = await fire(cfg, params_wear)
            wear_posts = len(mem.post) - skin_posts
        c_skin = out_skin["args"]["content"]
        c_wear = out_wear["args"]["content"]
        records = logs.records_text()
        skin_title = str(mem.post[0][1].get("title") or "") if mem.post else ""
        ok = (
            skin_posts == 1  # ★ 皮肤那条路写回 1 次（通道是通的）
            and "皮肤指向" in skin_title
            and "粉色妖精小姐" in skin_title
            and wear_posts == 0  # ★ 穿着那条路 0 次
            and "pink elf dress" in c_skin
            and all(keyword in c_wear for keyword in WEAR_KEYWORDS)
            and stub.calls == ["粉色妖精小姐"]
            and stub.wear_calls == ["雨衣"]
            and "皮肤解析结果已写回记忆库" in records
        )
        check(
            "C101 穿着解析结果不写回记忆库（皮肤那条路照旧写 1 次）",
            ok,
            f"皮肤 POST = {skin_posts}（title={skin_title!r}）/ 穿着 POST = {wear_posts}\n"
            f"        content(皮肤) = {c_skin!r}\n        content(穿着) = {c_wear!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C101 穿着解析结果不写回记忆库（皮肤那条路照旧写 1 次）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C102（冲突矩阵第 9 条）穿着 ↔ infer_unnamed（默认关）：两者互斥 ────────
    # 「没说名字的那一套」只在**一个候选都没识别到**时才走；这次既说了穿着
    # （学生装）、又说了「流萤的那一套皮肤」，规则是：穿着照常注入，
    # **推断那条路一次都不问**（否则同一次生图会同时塞进两套衣服）。
    try:
        cfg = make_config(
            roster=RS3,
            outfits=RS_OUTFIT,
            outfits_enabled=True,
            outfits_infer_unnamed=True,  # 推断开关**开着**，仍然不该被走到
        )
        cfg.general.history_cache_ttl = 0.0
        request = "穿学生装，然后来一张流萤的那一套皮肤"
        params = natural_params(PINK_SCENE, text=request)
        params["stream_id"] = "s-c102"
        with (
            FakeOutfit(
                OUTFIT_PAYLOAD,
                infer_payload=INFER_PAYLOAD,
                wear_payload=WEAR_PAYLOAD,
            ) as stub,
            FakeHistory([history_message("我们来聊流萤吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword in c for keyword in STUDENT_KEYWORDS)  # ★ 穿着照常注入
            and "（穿着：“学生装”" in c
            and all(keyword not in c for keyword in INFER_KEYWORDS)  # ★ 推断那条路没走
            and "流萤·花嫁" not in c
            and stub.infer_calls == []  # ★ 推断调用 0 次
            and stub.wear_calls == []  # 穿着走内置表
            and stub.calls == []
            and "皮肤推断" not in records
            and "穿着类型解析(内置表): 学生装" in records
        )
        check(
            "C102 穿着 ↔ infer_unnamed：识别到穿着时绝不进推断（推断调用 0 次）",
            ok,
            f"content = {c!r}\n        推断调用 = {stub.infer_calls!r} / 穿着调用 = "
            f"{stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C102 穿着 ↔ infer_unnamed：识别到穿着时绝不进推断（推断调用 0 次）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C103（缓存）穿着模型解析命中缓存 → 第二次不再问模型（1.0.28）──────────
    # 冲突矩阵没单列这条，但它是同一类"架"：单次多图（「画三张」）/ 用户连问两次时，
    # 同一个穿着词**不该问两遍模型**。缓存键 ``<原词>|__wear__``（成功 24 小时），
    # 命中日志 ``穿着解析(缓存)``，两次注入的内容逐字一致。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=WEAR_REQUEST)
        params["stream_id"] = "s-c103"
        history = [history_message("我们来聊爱莉希雅吧", 1.0)]
        with (
            FakeOutfit(OUTFIT_PAYLOAD, wear_payload=WEAR_PAYLOAD) as stub,
            FakeHistory(history),
            LogCapture() as logs,
        ):
            out_first = await fire(cfg, params)
            out_second = await fire(cfg, params)
        c_first = out_first["args"]["content"]
        c_second = out_second["args"]["content"]
        records = logs.records_text()
        ok = (
            stub.wear_calls == ["雨衣"]  # ★ 只问了一次（第二次命中缓存）
            and all(keyword in c_first for keyword in WEAR_KEYWORDS)
            and c_first == c_second  # ★ 两次注入逐字一致
            and "穿着解析(缓存): 雨衣 → raincoat, hood" in records
            and "穿着解析(模型): 雨衣 → raincoat, hood" in records
        )
        check(
            "C103 穿着解析命中缓存 → 第二次 0 次模型调用、注入一致",
            ok,
            f"穿着调用 = {stub.wear_calls!r}\n        content(第一次) = {c_first!r}\n"
            f"        content(第二次) = {c_second!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C103 穿着解析命中缓存 → 第二次 0 次模型调用、注入一致",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C104（1.0.30 重写）「学生会 / 学生时代」端到端不再注入校服 ──────────────
    #
    # 这条用例的历史（**1.0.30 的改法是抽取侧，不是放宽断言**）：
    #   · 1.0.29 把 `find_wear_type` 的匹配口径从"包含"收紧成"相等或后缀"，
    #     修好了「学生时代」；但「我想看学生会～」还是红 —— 候选在**句式抽取**那一步
    #     就被虚词「会」截断成「学生」，而「学生」**恰好是内置表的键** → 相等命中 →
    #     零模型调用注入 `school uniform, pleated skirt`。
    #   · 1.0.29 的用例把这条**当成残留事实钉住**（`council_candidate == ["学生"]`）。
    #   · 1.0.30 改的是**抽取侧**：句式抽取不再把「会」当虚词
    #     （`outfit._OUTFIT_SENTENCE_STOP_CHARS`），候选因此是完整的「学生会」→
    #     不等于任何键、也不以任何键结尾 → 不命中穿着类型表 → 走模型兜底
    #     （假模型答 unknown）→ **什么都不注入**。
    #   · 因此 `council_candidate` 的断言**按真实行为重写**为 `["学生会"]`，
    #     其余断言（不注入 / 无穿着日志 / 走模型兜底 / 角色照旧补花火）一个字没改；
    #     单元级样例表反而**加严**了（1.0.30 新补的 15 条同义写法全部要求命中）。
    #     ⚠️ 这是"按最终真实可达到的行为重写"，**不是放宽**：改动前它断言的是
    #     「仍然注入校服」这一 bug 的存在，改动后断言的是 bug 已被修掉。
    try:
        from ..character import parse_roster
        from ..outfit import extract_outfit_names, find_wear_type, wear_type_table

        # ★ 单元级：**本身是键**（相等）与**后缀**照旧命中，前缀 / 中缀不再命中
        #   （find_wear_type 的第 2 参是 ``dict[str, WearTypeMatch]`` 的"条目表"；
        #   省略时它自己按内置表 :data:`outfit.WEAR_TYPE_KEYWORDS` 拼同一张表 ——
        #   这里显式传 ``wear_type_table("")`` = 与 handler._wear_types 同一形状）
        wear_table = wear_type_table("")
        strict = WEAR_COUNCIL_UNIT_CASES
        strict_hits = {
            word: find_wear_type(word, wear_table) is not None for word in strict
        }
        wrong = {word: hit for word, hit in strict_hits.items() if hit != strict[word]}

        # ★ extract_outfit_names 的真实签名是 ``(text, entries, roster)``：第 2 / 3 参
        #   要的是 **parse_outfits / parse_roster 解析出来的条目列表**，不是配置原文。
        #   把配置原文（``RS3`` 是 str）当 roster 传进去 → ``_outfit_like`` → ``_in_roster``
        #   去 ``for entry in roster`` 迭代字符串、对每个字符取 ``.name``
        #   → ``AttributeError: 'str' object has no attribute 'name'``（C104 原来红在这里）。
        #   handler._scan_outfit_names 传的就是解析后的角色表，这里与它对齐。
        roster = parse_roster(RS3)

        # ★ 端到端：候选是「学生时代」（**前缀**含「学生」，不是以它结尾）
        #    → 不再命中内置表 → 按"两张表都没有"继续走模型兜底（假模型默认
        #    unknown）→ 什么都不注入；日志里**没有** `穿着类型解析(内置表)`。
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text=WEAR_ERA_REQUEST)
        params["stream_id"] = "s-c104"
        with (
            FakeOutfit() as stub,  # 默认 payload = {"unknown": True}
            FakeHistory([history_message("我们来聊爱莉希雅吧", 1.0)]),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        # 候选确实**包含**「学生」（不是空转）——这正是 1.0.28 会被吞掉的那个形状
        # （第 2 参 ``[]`` = 不带皮肤表 → 拿**原始候选集**，与 handler 的
        # ``extract_outfit_names(text, [], roster)`` 同一次调用）
        era_candidate = extract_outfit_names(WEAR_ERA_REQUEST, [], roster)
        # 1.0.30：抽取侧去掉虚词「会」之后，候选是**完整的**「学生会」
        # （1.0.29 时这里是被截断的「学生」→ 相等命中 → 仍然注入）
        council_candidate = extract_outfit_names(WEAR_COUNCIL_REQUEST, [], roster)
        ok = (
            not wrong  # ★ 相等 / 后缀命中，前缀 / 中缀不命中
            and era_candidate == ["学生时代"]  # ★ 候选真的是「学生时代」
            and all(keyword not in c for keyword in STUDENT_KEYWORDS)  # ★ 不注入
            and "（穿着：" not in c
            and "已追加穿着" not in records
            and "穿着类型解析(内置表)" not in records  # ★ 没有零模型调用注入
            and "穿着类型解析(内置表): 学生" not in records
            and "穿着类型解析(内置表): 学生会" not in records  # 与上面两条同一件事
            and stub.calls == ["学生时代"]  # 走了模型兜底（皮肤那条路）
            and stub.wear_calls == []
            and "皮肤解析(LLM)失败" in records  # 假模型默认 unknown → 什么都不注入
            and "花火" in c  # 角色注入照旧（触发消息里点的是花火 → decided=[花火]）
            # 1.0.30：句式抽取与候选过滤**共用同一张虚词表**（``_OUTFIT_STOP_CHARS`` /
            # ``_OUTFIT_SENTENCE_STOP_CHARS``，两边都不把「会」当边界），所以候选是
            # **完整的**「学生会」—— 与上面 era_candidate 同一形状，**不是**被过滤阶段
            # 整条丢掉的 ``[]``（``[]`` 是"只改句式抽取那一侧"时的中间态，见 README
            # 「两张表必须一起改」与 docs 的 1.0.30 ①）。断言按真实可达行为对齐，
            # **不是放宽**：改动前钉的是"候选被截断成 学生 → 误命中 → 误注入"，
            # 改动后钉的是"候选是完整的 学生会 → 不等于任何键、也不以任何键结尾
            # → 不命中穿着类型表 → 不误注入"；真正要守的安全属性由上面几条端到端断言
            # （不注入 / 无穿着类型日志 / 走模型兜底）覆盖。
            and council_candidate == ["学生会"]
        )
        check(
            "C104 学生会 / 学生时代都不再被当成穿着（抽取侧修掉虚词截断）",
            ok,
            f"单元命中 = {strict_hits!r}（不符 = {wrong!r}）\n"
            f"        候选(学生时代) = {era_candidate!r} / 候选(学生会) = "
            f"{council_candidate!r}\n        content = {c!r}\n"
            f"        调用 = 皮肤{stub.calls!r} / 穿着{stub.wear_calls!r}\n"
            f"        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C104 学生会 / 学生时代都不再被当成穿着（抽取侧修掉虚词截断）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C105（1.0.29 审计修 2）穿着也做归属校验：爱莉的婚纱贴不到流萤身上 ───────
    # 独立只读审计：用户说「画爱莉的婚纱」而本张判定角色是**流萤**时，② / ④ 产出的
    # `ResolvedOutfit.character` 恒为空 → `_apply_outfit` 的归属校验（1.0.26 起只对
    # 皮肤那条路生效）不触发 → 婚纱照旧贴到流萤身上（1.0.27 的皮肤那条路会因
    # hint=爱莉被拦）。1.0.29 起：线索由 `_wear_character_hint` 从**说这句话的那条
    # 消息**里取（唯一才填），归属校验对穿着同样生效 → 不注入 + WARNING。
    #
    # 两句用户话的分工（这样才真的复现审计场景）：「爱莉穿婚纱的样子」是**穿着**那句
    # 话（句式「穿X」抓到候选「婚纱」，同句提到爱莉），判定角色由**更新**的那句给出
    # （「画流萤吧」→ 主题跟踪 = 流萤 → decided=[流萤]）。
    # ② 与 ④ 两条路各跑一遍：婚纱是内置表的键（零模型调用），雨衣要问一次模型。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        runs: list[tuple[str, bool, str]] = []
        for (
            label,
            sentence,
            wear_payload,
            keywords,
            wear_calls,
            resolve_log,
            skip_log,
        ) in (
            (
                "② 穿着类型表（婚纱）",
                WEAR_OWNER_SENTENCE,
                None,
                WEDDING_KEYWORDS,
                [],
                "穿着类型解析(内置表): 婚纱 → wedding dress, bridal veil, white dress",
                "穿着跳过：穿着「婚纱」属于「爱莉希雅」，但本张判定角色是「流萤」",
            ),
            (
                "④ 穿着模型解析（雨衣）",
                WEAR_RAIN_OWNER_SENTENCE,
                WEAR_PAYLOAD,
                WEAR_KEYWORDS,
                ["雨衣"],
                "穿着解析(模型): 雨衣 → raincoat, hood",
                "穿着跳过：穿着「雨衣」属于「爱莉希雅」，但本张判定角色是「流萤」",
            ),
        ):
            params = natural_params(PINK_SCENE, text="嗯～")
            params["stream_id"] = f"s-c105-{label}"
            with (
                FakeOutfit(wear_payload=wear_payload) as stub,
                FakeHistory(
                    [
                        history_message(sentence, 20.0),
                        history_message(WEAR_JUDGE_OTHER_REQUEST, 1.0),
                    ]
                ),
                LogCapture() as logs,
            ):
                out = await fire(cfg, params)
            content = out["args"]["content"]
            records = logs.records_text()
            hit = (
                all(keyword not in content for keyword in keywords)  # ★ 没有注入
                and "（穿着：" not in content
                and "已追加穿着" not in records
                and resolve_log in records  # ★ 穿着**确实解析出来了**（不是空转）
                and skip_log in records  # ★ 归属冲突 → 告警写明"属于谁 / 判定是谁"
                and "流萤" in content  # ★ 判定角色照旧注入（decided 真是流萤）
                and "爱莉希雅" not in content
                and stub.calls == []
                and stub.wear_calls == wear_calls
            )
            runs.append(
                (
                    label,
                    hit,
                    (
                        f"content={content!r} 调用=皮肤{stub.calls!r}/穿着"
                        f"{stub.wear_calls!r} 日志={records!r}"
                    ),
                )
            )
        check(
            "C105 穿着归属校验：属于爱莉希雅的婚纱 / 雨衣在判定=流萤时不注入并告警",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C105 穿着归属校验：属于爱莉希雅的婚纱 / 雨衣在判定=流萤时不注入并告警",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C106（防误杀）同一句穿着请求 + 判定角色与归属一致 → 照常注入 ────────────
    # 与 C105 只差"更新那句话里的名字"：判定角色 = 爱莉希雅 = 穿着归属 →
    # 归属校验**不该**触发（否则 1.0.29 会把"本人穿自己的婚纱"也一起拦掉）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text="嗯～")
        params["stream_id"] = "s-c106"
        with (
            FakeOutfit() as stub,
            FakeHistory(
                [
                    history_message(WEAR_OWNER_SENTENCE, 20.0),
                    history_message(WEAR_JUDGE_SAME_REQUEST, 1.0),
                ]
            ),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            all(keyword in c for keyword in WEDDING_KEYWORDS)  # ★ 照常注入
            and "（穿着：“婚纱”：wedding dress, bridal veil, white dress）" in c
            and "穿着跳过" not in records  # ★ 归属一致 → 不拦
            and "已追加穿着（share_visual / natural）: 婚纱 → wedding dress, "
            "bridal veil, white dress" in records
            and "爱莉希雅" in c  # 判定角色照旧注入
            and stub.calls == []  # ② 命中内置表：0 次模型调用
            and stub.wear_calls == []
        )
        check(
            "C106 防误杀：判定角色与穿着归属一致（爱莉希雅）→ 照常注入婚纱",
            ok,
            f"content = {c!r}\n        调用 = 皮肤{stub.calls!r} / 穿着"
            f"{stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C106 防误杀：判定角色与穿着归属一致（爱莉希雅）→ 照常注入婚纱",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C107（1.0.30 产品需求）「最新一条消息里点名的角色」= 最高优先 ──────────
    # 用户场景（需求原话）：
    #     你：那画流萤吧
    #     你：画爱莉的婚纱～
    # 用户要的是**按他最新说的角色来画** —— 1.0.30 新增信号
    # `handler._latest_named_entry`：触发消息没点名时，取**触发前最近 1 条用户消息**
    # 里明确写出的角色表内角色（恰好 1 个才采用），作为**最高优先**压过粘性与代词回指。
    # 本用例断言：判定角色=爱莉希雅、穿着走内置表零模型调用、**正常注入**婚纱关键词。
    #
    # 1.0.30 修这条用例时查清的两件事（都是**代码侧**修的，断言一条都没放宽）：
    #   ① 穿着那一层原本**识别不到**「画爱莉的婚纱～」里的「婚纱」（七条句式里没有
    #      「X 的 Y」这种归属写法）→ 判定改对了、衣服却还是没注入。修法：新增
    #      `outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN`（词表 = 内置穿着类型表的键 +
    #      "后面不再接名字字符"的边界，见那一处的说明）。
    #   ② 断言里的 `优先于主题跟踪:流萤` 与实现不符：**括号里写的是"原来会生效的那个
    #      结论"，而不是写死的名字**（`handler._overtaken_conclusion` 用同一套代码算
    #      出来，只进日志）。本 fixture 的窗口里 `画爱莉的婚纱～` 就是**最新**那条点名
    #      消息，主题跟踪自己也落在爱莉希雅（`换人还是追加不确定`）—— 所以这里断言
    #      `优先于主题跟踪:爱莉希雅` 与 `换人还是追加不确定`：语义没放宽（判定仍是
    #      爱莉希雅、婚纱照旧正常注入、无 `穿着跳过`），只是把日志文案对齐实现。
    #      真实事故里被压过的确实是上一条粘住的流萤（C105/C106 守的就是那个场景）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text="嗯～")
        params["stream_id"] = "s-c107"
        with (
            FakeOutfit() as stub,
            FakeHistory(
                [
                    history_message(LATEST_NAMED_STICKY_REQUEST, 2.0),
                    history_message(LATEST_NAMED_OUTFIT_REQUEST, 1.0),
                ]
            ),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        wear_line = (
            "穿着类型解析(内置表): 婚纱 → wedding dress, bridal veil, white dress"
        )
        ok = (
            all(keyword in c for keyword in WEDDING_KEYWORDS)  # ★ 正常注入（不再跳过）
            and "（穿着：“婚纱”：wedding dress, bridal veil, white dress）" in c
            and "穿着跳过" not in records  # ★ 归属校验没有拦它
            and "已追加穿着（share_visual / natural）: 婚纱" in records
            and "爱莉希雅" in c  # ★ 判定角色 = 最新那条消息里点的爱莉希雅
            and "流萤" not in c  # 不再是上一条的流萤
            and "判定依据=用户点名(最新消息):爱莉希雅" in records  # ★ 依据写明来源
            # ★ 日志同时写明"被压过的那个结论"（README 里那一行的形状：
            # `判定依据=用户点名(最新消息):爱莉希雅（优先于主题跟踪:…）`）。
            # **名字按本次 fixture 的真实结论写**：本 fixture 的窗口里
            # `画爱莉的婚纱～`（1 分钟前）是**最新**那条点名消息，主题跟踪自己也
            # 落在爱莉希雅（`换人还是追加不确定`）；真实事故里被压过的是上一条
            # 粘住的流萤 —— 括号里写的永远是"原来会生效的那个结论"，不是写死的名字。
            and "优先于主题跟踪:爱莉希雅" in records  # ★ 日志能看出压过了谁
            and "换人还是追加不确定" in records  # ★ 写明了压过的是什么状态
            and wear_line in records
            and stub.calls == []  # ① 皮肤表未命中、② 内置表命中 → 0 次模型调用
            and stub.wear_calls == []
        )
        check(
            "C107 用户最新点名优先：画爱莉的婚纱 → 判定爱莉希雅并正常注入婚纱",
            ok,
            f"content = {c!r}\n        调用 = 皮肤{stub.calls!r} / 穿着"
            f"{stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C107 用户最新点名优先：画爱莉的婚纱 → 判定爱莉希雅并正常注入婚纱",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C108（1.0.30 产品需求）同一场景 + 画面已点名别的角色 → fix 替换 ─────────
    # 与 C107 同一个判定（爱莉希雅），但**画面描述里已经写着流萤的标签**
    # （`firefly \(honkai: star rail\)`，就是调用方那份过期备忘的典型形状）。
    # 判定链走到「描述点名」那一档之后，1.0.19 的**期望集合对账**发现：
    # 画面里的流萤**不在**期望集合（本次判定=爱莉希雅）里 → 冲突；
    # `consistency_check="fix"` 且两侧都唯一 → 把画面里的流萤标签**替换**成爱莉希雅。
    # 日志一眼能看出「用户最新说的人 ≠ 画面里的人」：判定依据行带
    # `优先于主题跟踪:<原来会生效的那个结论>`（本 fixture 里同样是爱莉希雅，
    # 见 C107 的说明），告警行写的是
    # `画面标签=流萤…但上下文/记忆指向=爱莉希雅`。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        cfg.general.consistency_check = "fix"
        # 与 C38/C39/C64/C65 同一口径：隔离"标签补全"，只看一致性检测本身
        cfg.general.tag_completion = False
        params = natural_params(FIREFLY_SCENE, text="嗯～")
        params["stream_id"] = "s-c108"
        with (
            FakeOutfit() as stub,
            FakeHistory(
                [
                    history_message(LATEST_NAMED_STICKY_REQUEST, 2.0),
                    history_message(LATEST_NAMED_OUTFIT_REQUEST, 1.0),
                ]
            ),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            TAG_FIREFLY not in c  # ★ 画面里那个过期标签被替换掉
            and TAG_ELYSIA in c  # ★ 换成本次判定角色的标准标签
            and "爱莉希雅" in c
            and "一致性检查=冲突已修正（fix）" in records  # ★ 冲突被认出来并修正
            and "判定依据=用户点名(最新消息):爱莉希雅" in records
            # ★ 与 C107 同一处口径：括号里写的是"原来会生效的那个结论"
            # （本 fixture 的主题跟踪结论也是爱莉希雅 —— 见 C107 的说明）
            and "优先于主题跟踪:爱莉希雅" in records
            and all(keyword in c for keyword in WEDDING_KEYWORDS)  # 穿着照旧注入
            and "穿着跳过" not in records
            and stub.calls == []
            and stub.wear_calls == []
        )
        check(
            "C108 用户最新点名优先：画面已点名流萤时 fix 替换为爱莉希雅",
            ok,
            f"content = {c!r}\n        调用 = 皮肤{stub.calls!r} / 穿着"
            f"{stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C108 用户最新点名优先：画面已点名流萤时 fix 替换为爱莉希雅",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C109（1.0.30 闸门①）追加语义不抢：新信号不采用，退回「本条 + 上一条」──
    # 与 C35 **同一个场景、同一个结果**（「还有伊甸，一起画」→ 伊甸 + 上一条的爱莉希雅），
    # 只是把"新信号会不会抢走它"也钉住：日志里**不出现** `判定依据=用户点名(最新消息)`，
    # 走的是主题跟踪的追加分支（轨迹里有 `[追加]`）。
    # 为什么必须单独一条：闸门①没加时，这条信号会认为"最新一条里恰好 1 个角色 = 伊甸"，
    # 于是**只补伊甸**、把上一条点名的爱莉希雅丢掉（C35 就是这么红的）。
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
                    "stream_id": "s-c109",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        ok = (
            "伊甸" in c and "爱莉希雅" in c  # ★ 与 C35 同结果：本条 + 上一条都补
            and "判定依据=用户点名(最新消息)" not in debugs  # ★ 新信号没有接管
            and "[追加]" in debugs  # ★ 交回主题跟踪的"追加语义"分支
        )
        check(
            "C109 追加语义（还有/一起）→ 用户最新点名不采用，退回「本条 + 上一条」",
            ok,
            f"content = {c!r}\n        日志 = {debugs!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C109 追加语义（还有/一起）→ 用户最新点名不采用，退回「本条 + 上一条」",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C110（1.0.30 闸门②）疑问句 / 自我指代不抢：交回原判定链 ──────────────
    # 两跑：
    #   A. 疑问语气（与 C42 同一个场景「是爱莉还是你？」）→ 结果与 C42 一致：
    #      LLM 仲裁说花火就是花火，新信号不接管；
    #   B. 自我指代（「你自己也来一张～」）→ 新信号不接管，交给"本尊 vs 壳"那条链路
    #      （这里取不到本尊身份 → 不补，内容保持原样）。
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

        runs: list[tuple[str, bool, str]] = []
        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            # A. 疑问句：与 C42 同一句、同一条上文（1 分钟前点过爱莉希雅）
            with FakeHistory([history_message("画爱莉希雅", 1.0)]), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_drawing",
                        "action_name": "share_drawing",
                        "args": {"content": "画一张战斗场景"},
                        "message": FakeMessage("是爱莉还是你？"),
                        "stream_id": "s-c110-a",
                    },
                )
            c = out["args"]["content"]
            debugs = logs.debugs()
            runs.append(
                (
                    "A 疑问句",
                    "花火" in c
                    and "爱莉希雅" not in c
                    and "LLM 仲裁" in debugs  # ★ 与 C42 同结果
                    and "判定依据=用户点名(最新消息)" not in debugs,  # ★ 新信号没有接管
                    f"content={c!r} 日志={debugs!r}",
                )
            )

            # B. 自我指代：新信号不接管 → 走「本尊 vs 壳」那条链路（取不到本尊身份
            #    就不补，也不问 LLM —— 与 1.0.13 起的行为一致）
            raw = "画一张战斗场景"
            with FakeHistory([history_message("画爱莉希雅", 1.0)]), LogCapture() as logs:
                out = await fire(
                    cfg,
                    {
                        "signature": "nai_artist:action:share_drawing",
                        "action_name": "share_drawing",
                        "args": {"content": raw},
                        "message": FakeMessage("你自己也来一张～"),
                        "stream_id": "s-c110-b",
                    },
                )
            c = out["args"]["content"]
            debugs = logs.debugs()
            runs.append(
                (
                    "B 自我指代",
                    c == raw
                    and "爱莉希雅" not in c
                    and "判定依据=用户点名(最新消息)" not in debugs,  # ★ 新信号没有接管
                    f"content={c!r} 日志={debugs!r}",
                )
            )
        finally:
            handler_module.extract_characters_via_llm = original

        check(
            "C110 疑问句 / 自我指代 → 用户最新点名不采用（疑问句交 LLM 仲裁）",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C110 疑问句 / 自我指代 → 用户最新点名不采用（疑问句交 LLM 仲裁）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C111（1.0.30 闸门③）引用块 / 媒体块里的文字不算「用户最新点名」──────
    # 两跑（与 C43 同一份事故原文）：
    #   A. 整条消息就是引用块 + 图片块 → 清洗后**没有正文** → 判定依据=无(不补)，
    #      内容一字不改（这就是 C43 的场景，新信号原本会从引用块里抢走「爱莉」）；
    #   B. 正文 + 引用块混在一起：「画爱莉希雅吧[回复<…>：是花火还是流萤？][图片(xxx)]」
    #      → 清洗后正文仍是「画爱莉希雅吧」→ 新信号照常生效（爱莉希雅），
    #      引用块里的花火 / 流萤一个字都不参与（证明清洗只剥块、不剥正文）。
    try:
        cfg = make_config(roster=RS3, use_llm=False)
        cfg.general.history_cache_ttl = 0.0
        runs: list[tuple[str, bool, str]] = []
        raw = "白发少女站在镜头前"

        # A. 与 C43 完全同一份事故原文
        trigger_a = (
            "[回复<的饮水思源(2224****93)>：那你告诉我，是爱莉还是你？][图片(xxx)]"
        )
        with FakeHistory([]), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage(trigger_a),
                    "stream_id": "s-c111-a",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        runs.append(
            (
                "A 整条都是引用/图片块",
                c == raw
                and "爱莉希雅" not in c
                and "判定依据=无(不补)" in debugs  # ★ 与 C43 同结果
                and "判定依据=用户点名(最新消息)" not in debugs,  # ★ 新信号没有接管
                f"content={c!r} 日志={debugs!r}",
            )
        )

        # B. 正文 + 引用块：清洗只剥块，正文照旧算点名
        trigger_b = "画爱莉希雅吧[回复<某人>：是花火还是流萤？][图片(xxx)]"
        with FakeHistory([]), LogCapture() as logs:
            out = await fire(
                cfg,
                {
                    "signature": "nai_artist:action:share_photo",
                    "action_name": "share_photo",
                    "args": {"content": raw},
                    "message": FakeMessage(trigger_b),
                    "stream_id": "s-c111-b",
                },
            )
        c = out["args"]["content"]
        debugs = logs.debugs()
        runs.append(
            (
                "B 正文 + 引用块",
                "爱莉希雅" in c  # ★ 清洗后的正文照常点名
                and "判定依据=用户点名(最新消息):爱莉希雅" in debugs
                and "花火" not in c
                and "流萤" not in c,  # ★ 引用块里的名字不参与
                f"content={c!r} 日志={debugs!r}",
            )
        )
        check(
            "C111 引用/图片块里的名字不算用户最新点名（正文照旧算）",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C111 引用/图片块里的名字不算用户最新点名（正文照旧算）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C112（1.0.31 目标 A）触发消息没点名时，照旧读得到前一条的点名 ────────────
    # 需求场景：
    #     你：那画流萤吧
    #     你：画爱莉的婚纱～
    #     你：都试试？                     ← 触发这次生图
    # 触发消息**一个角色表内角色都没有**，却同时命中闸门①（「都」是追加语义词）与
    # 闸门②（含「？」）—— 1.0.30 会在触发消息那一条上先撞闸门 → 整条信号让位 →
    # 退回旧粘性（判定可能仍是流萤，婚纱再因归属冲突被 `穿着跳过` 拦下）。
    # 1.0.31 的前置判断：触发消息没点名时**跳过它的闸门①②**，直接落到"触发前最近
    # 1 条用户消息"（= `画爱莉的婚纱～`）上并按**全部**闸门判定 → 判定 = 爱莉希雅、
    # 婚纱正常注入。候选表最多两格（触发消息 + `_LATEST_NAMED_HISTORY_LIMIT` = 1），
    # 所以 `那画流萤吧`（2 分钟前）**根本不在候选里**（不会往更早找）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        params = natural_params(PINK_SCENE, text="都试试？")
        params["stream_id"] = "s-c112"
        with (
            FakeOutfit() as stub,
            FakeHistory(
                [
                    history_message(LATEST_NAMED_STICKY_REQUEST, 2.0),
                    history_message(LATEST_NAMED_OUTFIT_REQUEST, 1.0),
                ]
            ),
            LogCapture() as logs,
        ):
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "爱莉希雅" in c  # ★ 判定 = 触发前最近 1 条用户消息里点名的爱莉希雅
            and "流萤" not in c  # ★ 不是 2 分钟前那句 `那画流萤吧` 的粘性
            and "判定依据=用户点名(最新消息):爱莉希雅" in records  # ★ 依据写明来源
            and all(keyword in c for keyword in WEDDING_KEYWORDS)  # ★ 婚纱照旧注入
            and "（穿着：“婚纱”：wedding dress, bridal veil, white dress）" in c
            and "穿着跳过" not in records  # ★ 归属没有冲突
            and "已追加穿着（share_visual / natural）: 婚纱" in records
            and stub.calls == []  # ① 皮肤表未命中、② 内置表命中 → 0 次模型调用
            and stub.wear_calls == []
        )
        check(
            "C112 触发消息没点名（都试试？）→ 仍按前一条的点名判定并注入婚纱",
            ok,
            f"content = {c!r}\n        调用 = 皮肤{stub.calls!r} / 穿着"
            f"{stub.wear_calls!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C112 触发消息没点名（都试试？）→ 仍按前一条的点名判定并注入婚纱",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C113（1.0.31 目标 B）归属句式的边界容忍句末语气词 ─────────────────────
    # 「画爱莉的婚纱吧～」/「画爱莉的婚纱呢」（两跑）：判定侧一直是**爱莉希雅**
    # （触发消息里就点了名），但 1.0.30 的归属句式 `…的<穿着词>`
    # （`outfit._OUTFIT_POSSESSIVE_WEAR_PATTERN`）边界是"候选后面不能再接名字字符"
    # —— 「婚纱」后面紧跟的「吧」/「呢」也是汉字，于是整条句式被挡掉、**穿着什么都
    # 不注入**（判定对、衣服没穿上）。1.0.31 把边界放宽为"后面**不是名字字符**，
    # 或者**是虚词字符**"（`吧` / `呢` / `啊` … 都在 `outfit._OUTFIT_STOP_CHARS` 里）。
    # `会` **不在**那张虚词表里（1.0.30 起就不在），所以
    # 「我想看你的学生会长制服」照旧拆不出「学生」（C91 的语义一个字没变）。
    try:
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        runs: list[tuple[str, bool, str]] = []
        for label, trigger in (
            ("吧～", "画爱莉的婚纱吧～"),
            ("呢", "画爱莉的婚纱呢"),
        ):
            params = natural_params(PINK_SCENE, text=trigger)
            params["stream_id"] = f"s-c113-{label}"
            with (
                FakeOutfit() as stub,
                # 上一条点的是**别人**：归属校验因此真的会被考验一次
                # （穿着这条线索来自触发消息本身 → 爱莉希雅，与判定一致）
                FakeHistory([history_message(LATEST_NAMED_STICKY_REQUEST, 2.0)]),
                LogCapture() as logs,
            ):
                out = await fire(cfg, params)
            c = out["args"]["content"]
            records = logs.records_text()
            runs.append(
                (
                    label,
                    "爱莉希雅" in c
                    and all(keyword in c for keyword in WEDDING_KEYWORDS)
                    and "（穿着：“婚纱”：wedding dress, bridal veil, white dress）" in c
                    and "穿着跳过" not in records
                    and stub.calls == []
                    and stub.wear_calls == [],
                    (
                        f"content={c!r} 调用=皮肤{stub.calls!r}/穿着{stub.wear_calls!r}"
                        f" 日志={records!r}"
                    ),
                )
            )
        check(
            "C113 归属句式容忍句末语气词（婚纱吧～ / 婚纱呢）→ 判定爱莉希雅并注入婚纱",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C113 归属句式容忍句末语气词（婚纱吧～ / 婚纱呢）→ 判定爱莉希雅并注入婚纱",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C114（1.0.32 收窄 🟠1）归属句式的边界**只**放行句末语气词 ────────────────
    # 审计原文：1.0.31 的边界放行的是**整张** `outfit._OUTFIT_STOP_CHARS`
    # （的 / 了 / 是 / 我 / 你 / 和 / 都 / 还 / 再 / 个 / 点 / 们 …），于是
    #     爱莉的婚纱是我挑的   → 「的婚纱」后面是「是」（虚词）→ 注入 wedding dress
    #     流萤的校服被雨淋了   → 「的校服」后面是「被」（虚词）→ 注入校服关键词
    #     画爱莉的婚纱的话     → 「的婚纱」后面是「的」（虚词）→ 注入 wedding dress
    # 这三句都**不含穿着意图**（1.0.30 的硬边界不会命中），抓错会把衣服画错，
    # 与「宁少勿多」相悖 —— 而文档只承诺放行**句末语气词**。1.0.32 起边界只用
    # `outfit._OUTFIT_TAIL_PARTICLES`（吧吗呢啊呀哦嘛呗啦咯喔）。
    #
    # 两跑（合成**一条**用例，与 C112/C113 一样只占一个 check）：
    #   A. **单元级**：模式在"虚词尾巴"上**不匹配**，在 C113 的语气词尾巴、
    #      C107 的标点/串尾上照旧匹配（防收窄过头）；顺带钉住那张语气词表是
    #      `_OUTFIT_STOP_CHARS` 的**真子集**（1.0.32 的收窄就是这一步）。
    #   B. **端到端**：三段不含穿着意图的话 → 判定角色照旧注入（证明不是空转），
    #      但**不注入**任何穿着关键词（无 `（穿着：`、无 `已追加穿着`、
    #      无 `穿着类型解析`、无 `穿着解析(LLM)`）、0 次模型调用；
    #      再补一条 `画爱莉的婚纱吧～` 的**正例**（C113 的形状）证明没有收窄过头。
    #
    # 注：审计第三条 `婚纱的话` 是"假设句"形状 —— `的` 在穿着词**后面**，
    # 所以连 1.0.31 也不会命中（它守的是"别把边界放宽成任意后缀"）；
    # 审计说的那个形状的**完整句子**（`画的婚纱的话`）另列一条 `画爱莉的婚纱的话`，
    # 那一条 1.0.31 **会**注入、1.0.32 不注入。
    try:
        from .. import outfit as outfit_module

        pattern = outfit_module._OUTFIT_POSSESSIVE_WEAR_PATTERN
        unit_cases: list[tuple[str, str | None]] = [
            # 语气词尾巴 / 标点尾巴 / 串尾 → 照旧命中（C113 / C107 的形状）
            ("画爱莉的婚纱吧～", "婚纱"),
            ("画爱莉的婚纱呢", "婚纱"),
            ("画爱莉的婚纱～", "婚纱"),
            ("画爱莉的婚纱", "婚纱"),
            ("画爱莉的居家服，然后出门", "居家服"),
            # 虚词尾巴 → 1.0.32 起**不再**命中（1.0.31 会）
            ("爱莉的婚纱是我挑的", None),  # 是（判断词）
            ("流萤的校服被雨淋了", None),  # 被（介词）
            ("画爱莉的婚纱的话", None),  # 的（结构助词）
            ("爱莉的婚纱我买过一件", None),  # 我（代词）
            ("婚纱的话", None),  # 穿着词在 `的` **前面**：任何版本都不命中
            # C91 的形状：后面是「会」（汉字，且不在任何放行表里）
            ("我想看你的学生会长制服", None),
        ]
        unit_runs: list[tuple[str, bool, str]] = []
        for unit_text, expected in unit_cases:
            match = pattern.search(unit_text)
            got = match.group(1) if match else None
            unit_runs.append((unit_text, got == expected, f"期望={expected!r} 实际={got!r}"))
        tail_is_subset = set(outfit_module._OUTFIT_TAIL_PARTICLES) < set(
            outfit_module._OUTFIT_STOP_CHARS
        )

        # B. 端到端：（触发消息, 期望判定角色, 上一条用户消息）
        cfg = make_config(roster=RS3, outfits=RS_OUTFIT, outfits_enabled=True)
        cfg.general.history_cache_ttl = 0.0
        runs: list[tuple[str, bool, str]] = []
        negative_samples: list[tuple[str, str, str]] = [
            ("爱莉的婚纱是我挑的", "爱莉希雅", ""),
            ("流萤的校服被雨淋了", "流萤", ""),
            ("婚纱的话", "爱莉希雅", "画爱莉希雅吧"),
            ("画爱莉的婚纱的话", "爱莉希雅", ""),
        ]
        for index, (trigger, who, previous) in enumerate(negative_samples):
            history = [history_message(previous, 1.0)] if previous else []
            params = natural_params(PINK_SCENE, text=trigger)
            params["stream_id"] = f"s-c114-{index}"
            with FakeOutfit() as stub, FakeHistory(history), LogCapture() as logs:
                out = await fire(cfg, params)
            c = out["args"]["content"]
            records = logs.records_text()
            runs.append(
                (
                    trigger,
                    who in c  # ★ 角色照旧注入（证明这条用例不是空转）
                    and "（穿着：" not in c  # ★ 没有穿着说明
                    and "已追加穿着" not in records  # ★ 没有穿着注入日志
                    and "穿着类型解析" not in records  # ★ ② 内置表没有命中
                    and "穿着解析" not in records  # ★ ③/④ 也没走模型那条路
                    and stub.calls == []
                    and stub.wear_calls == [],
                    (f"content={c!r} 调用=皮肤{stub.calls!r}/穿着{stub.wear_calls!r}"
                    f" 日志={records!r}"),
                )
            )

        # 正例（防收窄过头）：C113 的第一跑原样再来一次
        params = natural_params(PINK_SCENE, text="画爱莉的婚纱吧～")
        params["stream_id"] = "s-c114-positive"
        with FakeOutfit() as stub, FakeHistory([]), LogCapture() as logs:
            out = await fire(cfg, params)
        c = out["args"]["content"]
        records = logs.records_text()
        runs.append(
            (
                "正例：画爱莉的婚纱吧～",
                "爱莉希雅" in c
                and all(keyword in c for keyword in WEDDING_KEYWORDS)
                and "（穿着：“婚纱”：wedding dress, bridal veil, white dress）" in c
                and "穿着跳过" not in records
                and stub.calls == []
                and stub.wear_calls == [],
                (f"content={c!r} 调用=皮肤{stub.calls!r}/穿着{stub.wear_calls!r}"
                f" 日志={records!r}"),
            )
        )

        check(
            "C114 归属句式边界只放行句末语气词（婚纱是… / 校服被… / 婚纱的话 → 不注入穿着）",
            tail_is_subset
            and all(hit for _, hit, _ in unit_runs)
            and all(hit for _, hit, _ in runs),
            (
                f"语气词表是真子集={tail_is_subset}\n        "
                + "\n        ".join(f"单元[{t}] {d}" for t, _, d in unit_runs)
                + "\n        "
                + "\n        ".join(f"端到端[{t}] {d}" for t, _, d in runs)
            ),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C114 归属句式边界只放行句末语气词（婚纱是… / 校服被… / 婚纱的话 → 不注入穿着）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C115（1.0.32 收窄 🟠2）触发消息是追加指令时，不再"只看前一条的单名" ─────
    # 审计给的可达场景：
    #     你：画爱莉和流萤一起
    #     你：流萤这张不错
    #     你：一起画吧                  ← 触发这次生图
    # 主题跟踪的结论是**流萤**、状态是「换人还是追加不确定」（与上一条点名相差 1 分钟
    # < `multi.multi_window_minutes` = 5）。1.0.31：触发消息没点名就跳过它的闸门①②
    # → 落到 `流萤这张不错`（恰好 1 个角色、无追加/疑问）→ 直接
    # `判定依据=用户点名(最新消息):流萤` 并 return，**压过**主题跟踪的"不确定"与
    # LLM 仲裁 → 只画流萤（该画两人只画一人）。1.0.32：触发消息含追加语义词（一起）、
    # 且"原来会生效的结论"没定死一个人（主题跟踪 = 流萤 + 换人还是追加不确定）→
    # **不让位**（`handler._additive_trigger_needs_tracking`）→ 走主题跟踪 +
    # LLM 仲裁（假模型给出两人）→ 两人同框。
    #
    # 反例（同一段上文、触发换成 `都试试？`）：兜底**只**管"追加指令"，含「？」
    # 的疑问句照旧让位给 `流萤这张不错` 的点名（`判定依据=用户点名(最新消息):流萤`，
    # 这就是 1.0.31 的 C112 口径），并且一次模型都不问 —— 反例通过才说明这次没有
    # "让位过头"。（注：`都试试？` 里的「都」本身也在 `ADDITIVE_WORDS` 里，
    # 所以真正的判别点是**问号**，不是"非追加词"。）
    try:
        from .. import handler as handler_module
        from ..character import CharacterEntry, CharacterExtraction

        cfg = make_config(roster=RS3, use_llm=True)
        cfg.general.history_cache_ttl = 0.0
        history = [
            history_message("画爱莉和流萤一起", 2.0),
            history_message("流萤这张不错", 1.0),
        ]
        calls: list[str] = []

        async def fake_extract(**kwargs):
            # 模拟"LLM 读了画面描述与上文后给出的仲裁结论"：这次要画的是**两人**
            calls.append(str(kwargs.get("description", "")))
            return CharacterExtraction(
                characters=[
                    CharacterEntry(name="爱莉希雅", game="崩坏三 (Honkai Impact 3rd)"),
                    CharacterEntry(
                        name="流萤", game="崩坏：星穹铁道 (Honkai: Star Rail)"
                    ),
                ],
                multi=True,
            )

        runs: list[tuple[str, bool, str]] = []
        original = handler_module.extract_characters_via_llm
        handler_module.extract_characters_via_llm = fake_extract
        try:
            # A. 追加指令（一起画吧）→ 不让位：交回主题跟踪 + LLM 仲裁
            calls.clear()
            params = natural_params("舞台上的双人合影", text="一起画吧")
            params["stream_id"] = "s-c115-a"
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(cfg, params)
            c = out["args"]["content"]
            records = logs.records_text()
            runs.append(
                (
                    "A 追加指令（一起画吧）不让位",
                    "爱莉希雅" in c
                    and "流萤" in c
                    and c.count("画面角色") == 2  # ★ 两人同框（1.0.31 只会画流萤）
                    and "判定依据=LLM 仲裁" in records  # ★ 走的是跟踪 + LLM 仲裁那条路
                    and "换人还是追加不确定" in records  # ★ 原来会生效的正是"不确定"
                    and "用户最新点名不采用（1.0.32）" in records  # ★ 1.0.32 的兜底生效
                    # ★ 没有按前一条的单名拍板（这正是 1.0.31 的行为）
                    and "判定依据=用户点名(最新消息)" not in records
                    and bool(calls),  # ★ 确实问了模型（仲裁）
                    f"content={c!r} 调用={calls!r} 日志={records!r}",
                )
            )

            # B. 反例：疑问句（都试试？）照旧让位给"最新点名"（1.0.31 的 C112 不变）
            calls.clear()
            params = natural_params("舞台上的双人合影", text="都试试？")
            params["stream_id"] = "s-c115-b"
            with FakeHistory(history), LogCapture() as logs:
                out = await fire(cfg, params)
            c = out["args"]["content"]
            records = logs.records_text()
            runs.append(
                (
                    "B 反例：疑问句（都试试？）照旧让位",
                    "流萤" in c
                    and "爱莉希雅" not in c
                    # ★ 判定 = 触发前最近 1 条用户消息里点名的流萤（让位生效）
                    and "判定依据=用户点名(最新消息):流萤" in records
                    # ★ 1.0.32 的兜底没有误伤疑问句
                    and "用户最新点名不采用（1.0.32）" not in records
                    and "判定依据=LLM 仲裁" not in records
                    and not calls,  # ★ 让位了就不问模型
                    f"content={c!r} 调用={calls!r} 日志={records!r}",
                )
            )
        finally:
            handler_module.extract_characters_via_llm = original

        check(
            "C115 触发消息是追加指令（一起画吧）→ 不让位，交回主题跟踪 + LLM 仲裁",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C115 触发消息是追加指令（一起画吧）→ 不让位，交回主题跟踪 + LLM 仲裁",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C116（1.0.34）已打静态补丁 → 认出来，且一个字都不改 ──────────────────
    # 「已打补丁的机器一行代码都不执行」是这一版的硬约束：检测口径是「事件名 +
    # .publish( + 写回 "args"」三类标记同时出现在宿主源码里（见
    # ``compat.looks_statically_patched``）。桩的 run_tool_call **源码**里带着
    # 这三类串，于是 install() 必须返回 already_patched，且模块属性仍是**同一个
    # 函数对象**（没被包装、也没打包装标记）。
    try:
        from .. import compat

        module = _fake_host_module(_stub_patched_run_tool_call)
        original = module.run_tool_call
        with FakeHost(module):
            status = compat.install(logging.getLogger("char_prompt_guard"))
        unchanged = module.run_tool_call is original
        flagged = bool(getattr(module.run_tool_call, "__cpg_runtime_wrapped__", False))
        check(
            "C116 已打静态补丁 → already_patched（函数对象未被替换、未打包装标记）",
            status == "already_patched" and unchanged and not flagged,
            f"status={status!r} 同一对象={unchanged} 包装标记={flagged}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C116 已打静态补丁 → already_patched（函数对象未被替换、未打包装标记）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C117（1.0.34）未打补丁 → applied，事件真的发布 / args 真的写回 ─────────
    # 三跑（合成一条用例，与 C111/C113 一样只占一个 check）：
    #   A. 有订阅者：run_tool_call 被包装 → 事件带 signature / action_name / args /
    #      message / stream_id 发到 before_action_call → 订阅者改写 args →
    #      **原函数收到的就是改写后的 calls**（"插件真的生效"的最小闭环）；
    #   B. 清空订阅者：**连事件都不发**（零开销路径），原函数收到原样的参数；
    #   C. tool 分支：``tool-*`` 名字 → 发 before_tool_call，tool_name / 签名都对。
    try:
        from .. import compat

        _STUB_CALLS.clear()
        _STUB_STATE["fail"] = False
        module = _fake_host_module(_stub_plain_run_tool_call)
        original = module.run_tool_call
        raw_content = "帮我画一张她战斗的样子"
        message = FakeMessage("画爱莉希雅吧", stream_id="s-c117")
        runs: list[tuple[str, bool, str]] = []

        def subscriber(_event_name: str, params: dict):
            """假订阅者：模拟插件在生图前把角色补进提示词。"""
            params["args"] = {
                **params["args"],
                "content": f"{params['args']['content']}，已补角色",
            }
            return EventDecision.SUCCESS, params

        def make_calls(call_id: str) -> list:
            """一次 tool call 的替身（与 agent 聊天器给的名字同形：action- 前缀）。"""
            return [
                FakeToolCall(
                    id=call_id,
                    name="action-share_photo",
                    args={"content": raw_content},
                )
            ]

        with FakeHost(module) as host:
            host.bus.subscribe("before_action_call", subscriber)
            status = compat.install(logging.getLogger("char_prompt_guard"))
            await module.run_tool_call(
                calls=make_calls("call-1"),
                response=None,
                usable_map=FakeUsableMap({"action-share_photo": FakeUsableAction}),
                trigger_msg=message,
                plugin=None,
                stream_id="s-c117",
            )
            event_name, received = (
                host.bus.received[-1] if host.bus.received else ("", {})
            )
            delivered = _STUB_CALLS[-1][0].args.get("content", "") if _STUB_CALLS else ""
            runs.append(
                (
                    "A 有订阅者：发布执行前事件 + 回写 args",
                    status == "applied"
                    and module.run_tool_call is not original
                    and getattr(module.run_tool_call, "__cpg_runtime_wrapped__", False)
                    is True
                    and getattr(module.run_tool_call, "__wrapped__", None) is original
                    and len(host.bus.received) == 1
                    and event_name == "before_action_call"
                    and received.get("signature") == "nai_artist:action:share_photo"
                    and received.get("action_name") == "share_photo"
                    and received.get("args") == {"content": raw_content}
                    and received.get("message") is message
                    and received.get("stream_id") == "s-c117"
                    and delivered == f"{raw_content}，已补角色",
                    (f"status={status!r} 发布={host.bus.received!r} "
                    f"原函数收到={delivered!r}"),
                )
            )

            # B. 没有订阅者 → 零开销路径：不发事件、原样调用原函数
            host.bus.clear()
            await module.run_tool_call(
                calls=make_calls("call-2"),
                response=None,
                usable_map=FakeUsableMap({"action-share_photo": FakeUsableAction}),
                trigger_msg=message,
                stream_id="s-c117",
            )
            runs.append(
                (
                    "B 无订阅者：不发事件、原样调用（零开销）",
                    len(host.bus.received) == 1  # ★ 没有新增发布
                    and _STUB_CALLS[-1][0].args.get("content") == raw_content,
                    (f"发布次数={len(host.bus.received)} "
                    f"原函数收到={_STUB_CALLS[-1][0].args!r}"),
                )
            )

            # C. tool 分支：注册表里是 ``tool-*`` 名字 → 发 before_tool_call（带 tool_name）
            host.bus.clear()
            host.bus.subscribe("before_tool_call", _noop_subscriber)
            await module.run_tool_call(
                calls=[
                    FakeToolCall(
                        id="call-3",
                        name="tool-draw_image",
                        args={"content": "画一张"},
                    )
                ],
                response=None,
                usable_map=FakeUsableMap({"tool-draw_image": FakeUsableTool}),
                trigger_msg=message,
                stream_id="s-c117",
            )
            tool_event, tool_received = (
                host.bus.received[-1] if host.bus.received else ("", {})
            )
            runs.append(
                (
                    "C tool 分支：发 before_tool_call（tool_name / signature 都对）",
                    tool_event == "before_tool_call"
                    and tool_received.get("tool_name") == "draw_image"
                    and tool_received.get("signature")
                    == "image_generator_plugin-neo:tool:draw_image"
                    and tool_received.get("stream_id") == "s-c117",
                    f"事件={tool_event!r} 参数={tool_received!r}",
                )
            )

        check(
            "C117 未打补丁 → applied（发布执行前事件，订阅者改写的 args 传给原函数）",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C117 未打补丁 → applied（发布执行前事件，订阅者改写的 args 传给原函数）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C118（1.0.34）三条兜底：开关关掉 / 原函数抛异常 / 认不出结构 ──────────
    # ① ``general.runtime_framework_compat=false`` → disabled，函数对象不动；
    # ② 包装之后原函数抛异常 → **异常照旧向上传播**（不吞），且**不打乱后续调用**
    #    （第二次照常发布事件、照常执行 —— 一次失败不该把包装层搞坏）；
    # ③ 桩里没有 run_tool_call（以及"有但不是可调用"）→ unsupported，不崩、不改。
    try:
        from .. import compat

        runs: list[tuple[str, bool, str]] = []

        # ① 配置关掉
        off_module = _fake_host_module(_stub_plain_run_tool_call)
        off_original = off_module.run_tool_call
        cfg = CharPromptGuardConfig()
        cfg.general.runtime_framework_compat = False
        with FakeHost(off_module):
            off_status = compat.install(logging.getLogger("char_prompt_guard"), cfg)
        runs.append(
            (
                "① 配置关掉 → disabled 且未替换",
                off_status == "disabled" and off_module.run_tool_call is off_original,
                (f"status={off_status!r} "
                f"同一对象={off_module.run_tool_call is off_original}"),
            )
        )

        # ② 包装后原函数抛异常 → 异常向上传播；第二次调用照常工作
        _STUB_CALLS.clear()
        _STUB_STATE["fail"] = False
        fail_module = _fake_host_module(_stub_plain_run_tool_call)
        message = FakeMessage("画爱莉希雅吧", stream_id="s-c118")
        raised = ""
        with FakeHost(fail_module) as host:
            host.bus.subscribe("before_action_call", _noop_subscriber)
            compat.install(logging.getLogger("char_prompt_guard"))
            _STUB_STATE["fail"] = True
            try:
                await fail_module.run_tool_call(
                    calls=[
                        FakeToolCall(
                            id="call-1",
                            name="action-share_photo",
                            args={"content": "画一张"},
                        )
                    ],
                    response=None,
                    usable_map=FakeUsableMap({"action-share_photo": FakeUsableAction}),
                    trigger_msg=message,
                )
            except RuntimeError as e:
                raised = str(e)
            first_events = len(host.bus.received)
            _STUB_STATE["fail"] = False
            result = await fail_module.run_tool_call(
                calls=[
                    FakeToolCall(
                        id="call-2",
                        name="action-share_photo",
                        args={"content": "画一张"},
                    )
                ],
                response=None,
                usable_map=FakeUsableMap({"action-share_photo": FakeUsableAction}),
                trigger_msg=message,
            )
        runs.append(
            (
                "② 原函数抛异常 → 向上传播且不影响后续调用",
                raised == "宿主原函数抛出的异常"
                and first_events == 1  # ★ 抛异常之前事件照发（"执行前"）
                and result == [(True, True)]
                and len(_STUB_CALLS) == 2
                and len(host.bus.received) == 2,
                (f"raised={raised!r} 事件={len(host.bus.received)} "
                f"第二次={result!r} 收到次数={len(_STUB_CALLS)}"),
            )
        )

        # ③ 桩里没有 run_tool_call / 有但不是可调用 → unsupported（不崩）
        empty_module = _fake_host_module(None)
        broken_module = _fake_host_module(None)
        broken_module.run_tool_call = 42
        with FakeHost(empty_module):
            empty_status = compat.install(logging.getLogger("char_prompt_guard"))
        with FakeHost(broken_module):
            broken_status = compat.install(logging.getLogger("char_prompt_guard"))
        runs.append(
            (
                "③ 没有 run_tool_call → unsupported（不崩、不改）",
                empty_status == "unsupported"
                and broken_status == "unsupported"
                and broken_module.run_tool_call == 42,
                f"没有={empty_status!r} 不可调用={broken_status!r}",
            )
        )

        check(
            "C118 开关关掉 / 原函数抛异常 / 没有 run_tool_call 三条兜底",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C118 开关关掉 / 原函数抛异常 / 没有 run_tool_call 三条兜底",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C119（1.0.36）分区解析：三种归属句式 → {分区: 角色} ─────────────────
    # 1.0.36 的「分区覆盖」是**实验性功能、默认关闭**（见 [region_override]）。
    # 解析侧是**三重条件**（分区词 + 角色名 + 归属句式），缺一不认 —— 这里只钉
    # "三种入口语言都能解析出正确的分区归属"，且角色一律归一到**角色表里的正式名**
    # （用户写别名「爱莉」→「爱莉希雅」）。三种句式各断言一次：
    #   ① ``头是流萤的，身子是爱莉的``（「是…的」两句连写）
    #   ② ``头换成流萤``（「换成」，单分区）
    #   ③ ``用爱莉的身子配流萤的头``（「用…的」+「配…的」）
    try:
        from ..character import parse_roster
        from ..region import parse_region_entries, parse_region_overrides

        roster = parse_roster(RS3)
        entries = parse_region_entries(region_entries())
        runs = [
            (REGION_SWAP_PHRASE, {"head": "流萤", "body": "爱莉希雅"}),
            (REGION_SINGLE_PHRASE, {"head": "流萤"}),
            (REGION_COMPOSE_PHRASE, {"head": "流萤", "body": "爱莉希雅"}),
        ]
        parsed = [
            (phrase, parse_region_overrides(phrase, roster, entries), expected)
            for phrase, expected in runs
        ]
        check(
            "C119 分区解析：三种归属句式各解析一次（角色归一到正式名）",
            all(got == expected for _, got, expected in parsed),
            "\n        ".join(
                f"{phrase!r} → {got!r}（期望 {expected!r}）"
                for phrase, got, expected in parsed
            ),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C119 分区解析：三种归属句式各解析一次（角色归一到正式名）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C120（1.0.36）注入：正向拼装（head 靠前）+ negative_swap 负向 ────────
    # 两跑：
    #   A. nai_artist（natural）→ 角色说明之后追加
    #      ``（分区覆盖：头=流萤：…；身子=爱莉希雅：…）``，head 特征词**在 body 之前**；
    #      nai_artist 的 Action **没有**负向参数 → negative_swap 只进 DEBUG
    #      （"负向词无槽位可写"那一行里能看到"被换掉那个人"的原词），画面一字不改；
    #   B. image_generator_plugin-neo（tags）→ 关键词追加进 content_description，
    #      负向词**真的**写进 negative_prompt（动作本身有这个参数，见 target.negative_arg），
    #      并带上 anti_mix 的防串味词。
    try:
        entries = region_entries()
        runs: list[tuple[str, bool, str]] = []

        cfg_natural = make_config(
            roster=RS3, region_enabled=True, region_entries=entries
        )
        cfg_natural.general.history_cache_ttl = 0.0
        params = natural_params("帮我画一张她战斗的样子", text=REGION_SWAP_PHRASE)
        with LogCapture() as logs:
            out = await fire(cfg_natural, params)
        content = out["args"]["content"]
        records = logs.records_text()
        runs.append(
            (
                "A natural：正向 head/body 都在且 head 靠前，负向进 DEBUG",
                all(word in content for word in REGION_FIREFLY_HEAD)
                and all(word in content for word in REGION_ELYSIA_BODY)
                and content.index("silver hair") < content.index("curvy")
                and "分区覆盖" in content
                and "负向词无槽位可写" in records
                # 被换掉的「头」= 爱莉希雅的头；被换掉的「身子」= 流萤的身子
                and all(word in records for word in REGION_ELYSIA_HEAD)
                and all(word in records for word in REGION_FIREFLY_BODY)
                and "negative_prompt" not in out["args"],
                f"content = {content!r}\n        日志 = {records!r}",
            )
        )

        cfg_tags = make_config(
            roster=RS3, region_enabled=True, region_entries=entries
        )
        cfg_tags.general.history_cache_ttl = 0.0
        params = tags_params("1girl, solo, standing", text=REGION_SWAP_PHRASE)
        with LogCapture() as logs:
            out = await fire(cfg_tags, params)
        content = out["args"]["content_description"]
        negative = str(out["args"].get("negative_prompt") or "")
        runs.append(
            (
                "B tags：正向关键词追加 + 负向写进 negative_prompt",
                all(word in content for word in REGION_FIREFLY_HEAD)
                and all(word in content for word in REGION_ELYSIA_BODY)
                and content.index("silver hair") < content.index("curvy")
                and all(word in negative for word in REGION_ELYSIA_HEAD)
                and all(word in negative for word in REGION_FIREFLY_BODY)
                and all(word in negative for word in REGION_ANTI_MIX_TERMS),
                f"content = {content!r}\n        负向 = {negative!r}",
            )
        )
        check(
            "C120 分区注入：正向按 head→body 拼装、negative_swap 写入被换掉角色的特征词",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C120 分区注入：正向按 head→body 拼装、negative_swap 写入被换掉角色的特征词",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C121（1.0.36）单分区：`头换成流萤` 只动 head，body / outfit 不变 ─────
    # 需求原话「也允许单分区（只换头）」：正向段里只出现一个分区（头），
    # 条目里流萤的 body / outfit 特征词**一个都不许出现**（它们没被指定）。
    try:
        cfg = make_config(
            roster=RS3, region_enabled=True, region_entries=region_entries()
        )
        cfg.general.history_cache_ttl = 0.0
        params = natural_params("帮我画一张她战斗的样子", text=REGION_SINGLE_PHRASE)
        with LogCapture() as logs:
            out = await fire(cfg, params)
        content = out["args"]["content"]
        records = logs.records_text()
        ok = (
            "silver hair" in content
            and "blue eyes" in content
            and "分区覆盖" in content
            and content.count("头=流萤") == 1
            and "身子=" not in content  # ★ 单分区：没有 body 段
            and "衣服=" not in content  # ★ 单分区：没有 outfit 段
            and all(word not in content for word in REGION_FIREFLY_BODY)
            and all(word not in content for word in REGION_FIREFLY_OUTFIT)
            # 角色在前、分区覆盖在后（走的还是既有注入通道）
            and content.index("流萤") < content.index("silver hair")
            and "分区覆盖: 头=流萤" in records
        )
        check(
            "C121 单分区（头换成流萤）：只动 head，body / outfit 不变",
            ok,
            f"content = {content!r}\n        日志 = {records!r}",
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C121 单分区（头换成流萤）：只动 head，body / outfit 不变",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C122（1.0.36）防误触发：三句都**不该**命中 ───────────────────────────
    # 三重条件缺一不认，所以：
    #   ① ``流萤的头饰很好看`` —— 有角色名、也"看起来有分区词"，但「头」后面是「饰」
    #      不是归属句式 → 不命中（尾边界挡住 `用…的<分区词>` 那一族）；
    #   ② ``头疼`` —— 只有分区词，没有角色、没有归属句式；
    #   ③ ``画流萤`` —— 只有角色名，没有分区词。
    # 三条都要求：**没有分区注入、没有任何"分区覆盖"相关日志**（识别不到 = 零动作）。
    #
    # 1.0.36 定稿时把**边界**一起钉住（同一处 check，仍是 C122 一条）：
    #   · 单元级**反例**（必须解析为空）：`馒头是流萤的` / `石头是爱莉的` /
    #     `回头是爱莉的`（单字分区词的前置边界：复合词里的「头」不是分区词）、
    #     `用流萤的头饰` / `配流萤的头像` / `和爱莉的头像拍照`（尾边界：后面是实词汉字）、
    #     `流萤的头是红色`（角色解析不出 → 不认）；
    #   · 单元级**正例**（必须照旧命中，防止收得太狠）：`我的头是流萤的` /
    #     `用流萤的头` / `配流萤的头吧` / `和爱莉的身子`；
    #   · 端到端**引用块**：整条触发消息都在 `[回复 …]` 里 → 清洗后没有正文 → 不注入
    #     （与 1.0.30 闸门③ 同一个 `strip_quoted_blocks` 口径）。
    try:
        from ..character import parse_roster
        from ..region import parse_region_entries, parse_region_overrides

        roster = parse_roster(RS3)
        entries = parse_region_entries(region_entries())
        cfg = make_config(
            roster=RS3, region_enabled=True, region_entries=region_entries()
        )
        cfg.general.history_cache_ttl = 0.0
        runs: list[tuple[str, bool, str]] = []
        for phrase in REGION_FALSE_PHRASES:
            parsed = parse_region_overrides(phrase, roster, entries)
            params = natural_params("帮我画一张她战斗的样子", text=phrase)
            with LogCapture() as logs:
                out = await fire(cfg, params)
            content = out["args"]["content"]
            records = logs.records_text()
            runs.append(
                (
                    phrase,
                    parsed == {}
                    and "分区覆盖" not in content
                    and "分区覆盖" not in records
                    and "negative_prompt" not in out["args"],
                    f"解析={parsed!r} content={content!r}",
                )
            )

        # 单元级边界样本：反例必须 `{}`，正例必须照旧命中（两条一起看，防"收得过头"）
        boundary_negatives = (
            "馒头是流萤的",
            "石头是爱莉的",
            "回头是爱莉的",
            "用流萤的头饰",
            "配流萤的头像",
            "和爱莉的头像拍照",
            "流萤的头是红色",
        )
        boundary_positives = (
            ("我的头是流萤的", {"head": "流萤"}),
            ("用流萤的头", {"head": "流萤"}),
            ("配流萤的头吧", {"head": "流萤"}),
            ("和爱莉的身子", {"body": "爱莉希雅"}),
        )
        bad = [
            f"{phrase}→{parse_region_overrides(phrase, roster, entries)!r}"
            for phrase in boundary_negatives
            if parse_region_overrides(phrase, roster, entries) != {}
        ]
        weak = [
            f"{phrase}→{parse_region_overrides(phrase, roster, entries)!r}"
            f"（期望 {expected!r}）"
            for phrase, expected in boundary_positives
            if parse_region_overrides(phrase, roster, entries) != expected
        ]
        runs.append(
            (
                "边界样本：复合词 / 头像 不命中，助词前缀照旧命中",
                not bad and not weak,
                f"反例误命中={bad!r} 正例漏={weak!r}",
            )
        )

        # 端到端：整条触发消息都在引用块里 → 清洗后没有正文 → 零动作
        quoted = f"[回复 {REGION_SWAP_PHRASE}]"
        params = natural_params("帮我画一张她战斗的样子", text=quoted)
        with LogCapture() as logs:
            out = await fire(cfg, params)
        content = out["args"]["content"]
        records = logs.records_text()
        runs.append(
            (
                "引用块里的分区说法不参与（strip_quoted_blocks 口径）",
                "分区覆盖" not in content and "分区覆盖" not in records,
                f"content={content!r} 日志={records!r}",
            )
        )
        check(
            "C122 防误触发：头饰 / 头疼 / 画流萤 + 边界样本都不命中（无注入、无日志）",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C122 防误触发：头饰 / 头疼 / 画流萤 + 边界样本都不命中（无注入、无日志）",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C123（1.0.36）开关与降级：enabled=false 零动作 / 条目缺角色→DEBUG ────
    # A. ``region_override.enabled = false``（默认）→ **零动作**：内容与"没配条目"
    #    时逐字一致，日志里连"分区覆盖"都没有；
    # B. 开着但 ``entries`` 里没有该角色（只配了爱莉希雅，说的是流萤）→
    #    **退回不分区**、打一条 DEBUG、角色注入照旧（流萤照常补进来）。
    try:
        runs: list[tuple[str, bool, str]] = []

        scene = "帮我画一张她战斗的样子"
        cfg_off = make_config(
            roster=RS3, region_enabled=False, region_entries=region_entries()
        )
        cfg_off.general.history_cache_ttl = 0.0
        cfg_bare = make_config(roster=RS3, region_enabled=False)
        cfg_bare.general.history_cache_ttl = 0.0
        params = natural_params(scene, text=REGION_SWAP_PHRASE)
        with LogCapture() as logs_off:
            out_off = await fire(cfg_off, params)
        with LogCapture() as logs_bare:
            out_bare = await fire(cfg_bare, params)
        content_off = out_off["args"]["content"]
        records_off = logs_off.records_text()
        runs.append(
            (
                "A enabled=false → 零动作",
                content_off == out_bare["args"]["content"]
                and "分区覆盖" not in content_off
                and "分区覆盖" not in records_off
                and "分区覆盖" not in logs_bare.records_text(),
                f"content = {content_off!r}\n        日志 = {records_off!r}",
            )
        )

        cfg_missing = make_config(
            roster=RS3,
            region_enabled=True,
            region_entries=region_entries(include_firefly=False),
        )
        cfg_missing.general.history_cache_ttl = 0.0
        params = natural_params(scene, text=REGION_SINGLE_PHRASE)
        with LogCapture() as logs_missing:
            out_missing = await fire(cfg_missing, params)
        content_missing = out_missing["args"]["content"]
        debugs_missing = logs_missing.debugs()
        runs.append(
            (
                "B 条目里没有该角色 → 不分区、打 DEBUG、其它注入照旧",
                "流萤" in content_missing  # 角色注入照旧
                and "分区覆盖" not in content_missing  # 没有分区段
                and all(word not in content_missing for word in REGION_FIREFLY_HEAD)
                and "分区覆盖=跳过" in debugs_missing
                and "entries 里没有该角色的条目" in debugs_missing,
                f"content = {content_missing!r}\n        DEBUG = {debugs_missing!r}",
            )
        )
        check(
            "C123 开关与降级：enabled=false 零动作 / 条目缺角色 → 不分区 + DEBUG",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C123 开关与降级：enabled=false 零动作 / 条目缺角色 → 不分区 + DEBUG",
            False,
            f"{type(e).__name__}: {e}",
        )

    # ── C124（1.0.36）stylized_swap：开 → 出现「接缝/过渡」词，关 → 一个都不出现 ──
    # 两跑同一句话：`stylized_swap=true` 时正向段末尾多一段
    # ``接缝过渡：stitched neck, visible seam, head slightly tilted``；
    # `false`（默认）时这三句**正向与负向都不许出现**（关掉就是逐字不加）。
    try:
        runs: list[tuple[str, bool, str]] = []
        for stylized in (True, False):
            cfg = make_config(
                roster=RS3,
                region_enabled=True,
                region_entries=region_entries(),
                region_stylized=stylized,
            )
            cfg.general.history_cache_ttl = 0.0
            params = natural_params(
                "帮我画一张她战斗的样子", text=REGION_SWAP_PHRASE
            )
            with LogCapture() as logs:
                out = await fire(cfg, params)
            content = out["args"]["content"]
            records = logs.records_text()
            present = [term for term in REGION_STYLIZED_TERMS if term in content]
            in_logs = [term for term in REGION_STYLIZED_TERMS if term in records]
            if stylized:
                hit = (
                    len(present) == len(REGION_STYLIZED_TERMS)
                    and "接缝过渡" in content
                    # 这些词是**正向**的（画面里就该有那道接缝），不是负向
                    and "分区覆盖" in content
                )
            else:
                hit = not present and not in_logs
            runs.append(
                (
                    f"stylized_swap={stylized}",
                    hit,
                    f"content = {content!r}\n        日志 = {records!r}",
                )
            )
        check(
            "C124 stylized_swap：true → 接缝/过渡词出现；false → 一个都不出现",
            all(hit for _, hit, _ in runs),
            "\n        ".join(f"{label}: {detail}" for label, _, detail in runs),
        )
    except Exception as e:  # noqa: BLE001
        check(
            "C124 stylized_swap：true → 接缝/过渡词出现；false → 一个都不出现",
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
