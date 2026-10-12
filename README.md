# char_prompt_guard — 角色提示词守卫

> 文档版本：1.0.0 · 作者：思源（siyuan）· 许可：MIT
> 同时适配 **nai_artist** 与 **image_generator_plugin-neo** 两个生图插件。
> 本插件由 **AI 辅助编写**（作者审阅与整合）：缺陷、遗漏或无意残留的第三方内容请见
> [AI_USAGE.md](./AI_USAGE.md)（无担保 / 责任限制 / 第三方内容通知即处理 / 反馈渠道）。

为生图插件增加一道「角色提示词」检测：bot 生成图片前先检查提示词里**是否已包含角色**——已含则放行，未含则根据最近对话上文找出当前指代的角色，自动补入「角色名 + 游戏名」；**多角色同框会一并补齐**（默认最多 6 个），并且可以由 bot 在运行时决定单人还是多人。

## 兼容的生图插件（下列两个都已适配）

本插件对下面**两个**生图插件都做了适配，可同时安装（各自生效），也可只装其一（插件不硬依赖任何一个，未安装时无任何影响）：

| 生图插件 | 兼容状态 | 接管动作 | 提示词参数 | 追加模式 | 补入内容 |
| --- | --- | --- | --- | --- | --- |
| [nai_artist](https://github.com/bingyv92/nai_artist) | ✅ 已适配 | `share_visual` | `content`（中文自然语言） | `natural` | `（画面角色：“爱莉希雅”，出自游戏《崩坏三 (Honkai Impact 3rd)》）`，由 nai_artist 的翻译模型转成 `elysia \(honkai impact 3rd\)` |
| [image_generator_plugin-neo](https://github.com/tt-P607/image_generator_plugin-neo) | ✅ 已适配 | `draw_image` | `content_description`（**本身就是 NAI 标签**） | `tags` | 单人：直接追加标准角色标签，如 `elysia \(honkai impact 3rd\)`；**多人：写入 `characters` 独立角色槽位**（含分散坐标） |

> 两者的提示词语义完全相反：nai_artist 的参数是**中文自然语言**，由它自己再翻译成语言标签；image_generator_plugin-neo 的参数**已经是最终 NAI 标签串**，没有翻译步骤。因此插件按目标 Action 区分追加格式（`natural` / `tags`），不能一刀切。
>
> 其它生图插件未内置适配，但可通过 `[targets]` 配置自行接入，见下文「目标 Action 表」。

## 工作原理

插件注册了一个 `BaseEventHandler`，订阅内置事件 `before_action_call`：

1. 按配置的「目标 Action 表」判断当前调用是否要接管（默认内置上表两个动作，可用配置覆盖/扩展）；
2. `nai_artist:share_visual` 还要通过 `apply_modes` 过滤（默认仅 `drawing` 手绘画作模式；`photo` 自拍模式 nai_artist 本身会吸收角色人设，默认不动）；
3. 检测提示词是否已命中角色表（角色名/别名）；**标签模式**下还会比对标准角色标签，并顺带检查 neo 的 `characters` 多人物参数——命中即放行；
4. 未命中时按两步从「上文」中找角色：
   - **角色表快速匹配**（零成本、确定性）：扫描最近 `history_hours` 小时内的消息，取最近一次提及的角色；
   - **LLM 兜底提取**（`use_llm=true` 时）：可识别角色表之外的角色，输出 `{"name", "game", "tag"}`；
5. 找到角色后按目标模式补全提示词（自然语言说明 / 标准标签）；对 `args` 的修改会被 ActionManager 回写，在翻译与生图之前生效；
6. 任何情况下都返回 `SUCCESS`，绝不拦截生图；标签模式下若角色表未提供标签，**宁可跳过注入也不污染提示词**。

## 安装

1. 安装至少一个受支持的生图插件（nai_artist 或 image_generator_plugin-neo）；
2. 将本插件目录（或 `char_prompt_guard-1.0.0.mfp` 包）放入 Neo-MoFox 的 `plugins/` 目录；
3. 重启 bot，插件会自动生成默认配置：`config/plugins/char_prompt_guard/config.toml`。

## 配置说明

```toml
[general]
enabled = true            # 总开关
apply_modes = "drawing"   # nai_artist 的生效模式：drawing / photo（逗号分隔）
history_hours = 2.0       # 回溯多少小时的上文
history_limit = 40        # 最多读多少条上文
history_cache_ttl = 5.0   # 上文消息的进程内缓存秒数（0=关闭；触发消息永远实时读）
use_llm = true            # 角色表未命中时用 LLM 兜底提取
translate_model = ""      # 提取用模型名（留空用 UTILS_SMALL）
llm_timeout = 20.0        # LLM 提取超时秒数（0=不限时；防模型挂起卡住生图）
append_format = "（画面角色：“{name}”，出自游戏《{game}》）"
append_format_no_game = "（画面角色：“{name}”）"

[roster]
enabled = true            # 角色表快速匹配开关
characters = """
# 每段：角色名 | 游戏名 | 别名1,别名2 | 角色标签
爱莉希雅 | 崩坏三 (Honkai Impact 3rd) | elysia | elysia \(honkai impact 3rd\)
伊甸 | 崩坏三 (Honkai Impact 3rd) | eden | eden \(honkai impact 3rd\)
"""

[targets]
enabled = true            # 目标 Action 接管开关
list = ""                 # 留空使用内置默认（nai_artist + image_generator_plugin-neo）

[multi]
enabled = true            # 多角色同框补全（默认开启）
scope = "multi_only"      # 仅当上文出现 ≥2 个角色时才启用集合式检测
max_characters = 6        # 单次最多补入几个角色
natural_max = 2           # natural 模式（nai_artist）单次最多补几个（超出截断并 WARN）
decision = "bot"          # 多人判定：bot=主模型标记 + LLM 判定；always / off 可强制
llm_mode = "fallback"     # fallback=角色表没命中才问 LLM；always=每次都问
recent_only = true        # 候选只取最近 N 条消息
recent_messages = 6       # 上面那个 N
strategy_neo = "characters"  # ≥2 人写入 characters 角色槽位（单人仍追加标签）
coord_plan = "spread"     # 槽位坐标按顺序均匀分散
bot_control = true        # 允许主模型用 #multi# 等标记临时切换
notify_bot = true         # 注入 system reminder 告知主模型标记用法
```

### 角色表（`[roster] characters`）

每个条目：`角色名 | 游戏名 | 别名1,别名2 | 角色标签`，除角色名外都可省略。

| 字段 | 作用 |
| --- | --- |
| 角色名（第 1 段） | **参与检测匹配**（必需） |
| 游戏名（第 2 段） | 拼进 `natural` 模式的追加文本，作为翻译模型的出处线索 |
| 别名（第 3 段，逗号分隔） | **参与检测匹配**：英文名务必写上，避免主模型写英文时漏判 |
| 角色标签（第 4 段） | `tags` 模式**直接写入提示词**的标准标签，格式 `角色名 (作品名)` |

三种等价书写方式（按编辑环境挑一种）：

| 写法 | 示例 |
| --- | --- |
| 多行（推荐，直接编辑 `config.toml`） | `爱莉希雅 \| 崩坏三 (Honkai Impact 3rd) \| elysia \| elysia \(honkai impact 3rd\)` 换行 `伊甸 \| 崩坏三 \| eden` |
| 单行分号分隔（WebUI 单行输入框用这个） | `爱莉希雅 \| 崩坏三 \| elysia ; 伊甸 \| 崩坏三 \| eden` |
| 字面量 `\n`（从 JSON 复制粘贴时） | `爱莉希雅 \| 崩坏三 \| elysia\n伊甸 \| 崩坏三 \| eden` |

规则：

- 条目之间用**换行或分号（`;` / `；`）**分隔，全角竖线 `｜`、全角逗号 `，` 也能识别；
- **整行**以 `#` 开头是注释，整行忽略（注释里写分隔符不会有副作用）；
- **角色表留空照样能用**：每次都走 LLM 从上文识别角色（多一次小模型调用）；填了角色表则优先走零成本精确匹配；
- **只装 nai_artist 时第 4 列可以留空**；用 image_generator_plugin-neo（`tags` 模式）时**第 4 列必填**，否则插件会跳过注入并打日志提示；
- 注：WebUI 的角色表输入框是单行输入，网页上编辑请用**分号**分隔；多行写法请直接编辑 `config.toml`。

#### 为什么别名和第 4 列都要填

| 情况 | 后果 |
| --- | --- |
| 别名只写中文（如 `伊甸 \| 崩坏三`） | 主模型写英文 `eden` 时检测不到 → 误判成"没有角色" → 重复追加 |
| 第 4 列留空但使用 `tags` 模式 | 无法产出合法标签，插件跳过注入（不会把中文塞进标签串，画面不会被污染） |
| 只给中文作品名 | `natural` 模式下依赖翻译模型自行映射英文作品名，冷门角色可能失准 |

### 目标 Action 表（`[targets] list`）

留空使用内置默认。需要接管其它动作（例如 neo 的 `edit_image` / `inpaint_image`）时按此格式填写，**填写后覆盖内置默认**：

```
插件名:action:动作名 | 提示词参数名 | 追加模式 | 模式参数 | 额外检测参数 | 角色槽位参数
```

| 段 | 说明 |
| --- | --- |
| 1 | 组件签名，可只写动作名（如 `draw_image`）；只写动作名时不限制所属插件 |
| 2 | 承载提示词的参数名（必填） |
| 3 | `natural`（追加中文说明，生图插件会再翻译）或 `tags`（直接追加 NAI 标签） |
| 4 | 需要按 `apply_modes` 过滤的参数名，如 nai_artist 的 `mode`；留空不过滤 |
| 5 | 额外参与"是否已含角色"检测的参数名，逗号分隔，如 neo 的 `characters` |
| 6 | 独立角色槽位参数名（多人同框时写入），如 neo 的 `characters`；留空则不用槽位 |

示例（在保留 nai_artist 的同时，额外接管 neo 的图生图）：

```
nai_artist:action:share_visual | content | natural | mode ; image_generator_plugin-neo:action:draw_image | content_description | tags | | characters | characters ; image_generator_plugin-neo:action:edit_image | content_description | tags
```

### 多角色同框（`[multi]`）

**默认开启**。行为规则：

| 场景 | 处理 |
| --- | --- |
| 上文只提到 1 个角色（`scope=multi_only`） | 完全沿用单角色逻辑：提示词写了角色就放行，没写就补这 1 个 |
| 上文提到 ≥2 个角色，提示词没写角色 | 按最近提及顺序补齐，最多 `max_characters`（默认 6）个 |
| 上文提到 ≥2 个角色，提示词写了其中 1 个 | 只补缺失的那几个 |
| neo 且需要补 ≥2 个 | 写入 `characters` 独立角色槽位（`strategy_neo=characters`），坐标默认按顺序分散：2 人 `0.333/0.667`、3 人 `0.25/0.5/0.75` |
| nai_artist（无多角色机制） | 追加多段中文说明，超过 2 人自动截断并打 WARN |

> ⚠️ 两个限制要知道：
> 1. **坐标是否生效取决于 neo**：它只在自身配置启用 `always_use_coords` 时才把坐标传给 NAI；没启用时坐标会被忽略（但多人提示词仍然生效）。
> 2. **nai_artist 可靠上限约 2 人**：它不使用 NAI 的多角色槽位，≥3 人会出现发色/服装互串。

#### 由 bot 决定的自动切换

多人判定默认 `decision = "bot"`，按以下顺序决定：

1. **主模型标记**（最高优先，需 `bot_control = true`）；
2. **LLM 判定**：提取调用会一并返回 `multi` 字段，判断"本次是否需要多人同框"；
3. **规则兜底**：候选角色 ≥2 且提示词里没写角色 → 按多人处理。

主模型可用标记（会被自动移除，不会进入画面）：

| 标记 | 作用 |
| --- | --- |
| `#multi#` | 强制本次按多人同框补齐 |
| `#solo#` | 强制本次只补一个角色 |
| `#chars#` | 强制用角色的独立槽位参数（neo 的 `characters`） |
| `#tags#` | 强制把角色标签直接追加进提示词 |
| `#nochar#` | 本次完全不要自动补角色 |

为了让主模型知道这些标记可用，插件加载时会向 `actor` bucket 注入一条简短的 system reminder（`notify_bot = false` 可关闭）。它**不会**改变主模型写画面的习惯，只是提供了"需要时能显式控制"的手段。

如果想完全不做多人判断，把 `decision` 改成 `off`（总是单人）或 `always`（总是多人）。

### 示例

**nai_artist（natural 模式）**：上文「我们来聊爱莉希雅吧」→ 用户「帮我画一张她战斗的样子」，画面描述不含角色名，插件补入：

> （画面角色：“爱莉希雅”，出自游戏《崩坏三 (Honkai Impact 3rd)》）

再由 nai_artist 的翻译模型转成 `elysia \(honkai impact 3rd\)`。

**image_generator_plugin-neo（tags 模式）**：主模型给出的提示词是
`1girl, solo, standing on rooftop, night, city lights`，插件检测到没有角色后补成：

> `1girl, solo, standing on rooftop, night, city lights, elysia \(honkai impact 3rd\)`

若提示词里已经有 `elysia (honkai impact 3rd)`（转义写法差异也能识别），或 `characters` 参数里已声明该角色，则直接放行。

## 开发

```bash
# 用官方工具检查 / 打包（⚠️ 不要用 -o 换输出目录，见下方"打包两个坑"）
mpdt plugin check
mpdt plugin build --with-docs
```

### 打包两个坑（踩过，记一下）

1. **不要用 `-o <其它目录>`**：MPDT 只把「传入的输出目录名」当输出目录跳过，换了名字之后
   根目录的 `dist/` 就不再被识别，**上一次构建出来的 `.mfp` 会被整个打进新包**（实测包内出现
   `dist/char_prompt_guard-1.0.0.mfp`）。要么用默认输出，要么每次先清空 `dist/`。
2. **`.mfpignore` 对 MPDT 无效**：MPDT 0.6.6 不读 `.mfpignore`，只用硬编码规则排除
   `__pycache__` / `.git` / `.venv` / `node_modules` / `*.pyc` / 以 `.` 开头的文件。
   所以 `config/`、`data/`、`logs/` 一旦出现在插件目录里，**会被原样打进包** ——
   这正是"API key / 请求 URL 被存进插件"的传染路径。`.gitignore` 只能管 git。

   → 打包前跑一次守卫脚本，命中即 FAIL：

   ```bash
   python tests\check_privacy.py
   ```

### 自带自检脚本

三个脚本都用**实例自带的 venv** 跑（别用系统 python），不启动 bot、不改实例任何文件：

```bash
# ① 真实导入冒烟 + manifest 校验（证明「框架能加载」，而不只是「语法对」）
$env:NEO_ROOT="<你的 Neo-MoFox 框架根>"
& "$env:NEO_ROOT\.venv\Scripts\python.exe" tests\check_import.py

# ② 逻辑自检：检测 → 候选提取 → 注入 → 回写 args（零成本，不调模型）
& "$env:NEO_ROOT\.venv\Scripts\python.exe" tests\check_logic.py

# ③ 加 --llm 会多跑两条真调小模型的用例（角色表为空时的兜底提取等）
& "$env:NEO_ROOT\.venv\Scripts\python.exe" tests\check_logic.py --llm

# ④ 框架补丁状态（未打 / 原版 / 修正版）
& "$env:NEO_ROOT\.venv\Scripts\python.exe" tests\check_patch.py

# ⑤ 打包前守卫：插件目录里有没有 config/、data/、凭据或敏感文本（用系统 python 即可）
python tests\check_privacy.py
```

`NEO_ROOT` 不设时，按「插件装在 `<框架根>\plugins\<插件名>`」倒推两级。

> **测试脚本两种跑法都支持**（`mpdt plugin check --fix` 会把脚本里的绝对导入改成相对导入 ——
> 那是 ImportValidator 要求的规范写法，但会让 `python tests\xxx.py` 直接失败，所以脚本里加了
> `__package__` 兼容）：
>
> ```bash
> # ① 脚本方式
> python <插件目录>\tests\check_logic.py
> # ② 模块方式（cwd 必须在插件目录的上一级）
> python -m char_prompt_guard.tests.check_logic
> ```

## 框架依赖（重要，不打补丁在 agent 聊天器下不会生效）

插件靠「执行前事件」改写提示词，而**上游框架在 agent 聊天器路径下不发布这个事件**：

| 执行路径 | 发出的事件 |
| --- | --- |
| `ActionManager.execute_action` | ✅ `before_action_call` |
| `ToolManager.execute_tool` | ✅ `before_tool_call` |
| **agent 聊天器**（`default_chatter` / `neo_default_chatter`）→ `run_tool_call` → `create_llm_usable_execution` → `_wrap_execute` | ❌ **什么都不发** |

也就是说：在 agent 聊天器下插件收不到任何通知，表现是「装上去毫无反应、日志里连一条记录都没有」。
**这不是插件坏了，是钩子没来。** 解决办法是打框架补丁：

> ⚠️ **补丁不在插件包里**。`char_prompt_guard-1.0.0.mfp` / 插件目录只含插件自身；
> 补丁属于框架侧改动，随**交付包**的 `framework-patch/` 目录单独提供，请先把它拷到本机（例如
> 放到框架根旁边的任意位置），再执行下面命令；路径按你实际存放位置替换。

```bash
cd <你的 Neo-MoFox 框架根>
git apply <交付包>/framework-patch/llm_tool_call-pre-events.patch
# 失败时可加 --3way；仍失败就照 patch 内容手工改 src/core/utils/llm_tool_call.py
```

补丁只改 `src/core/utils/llm_tool_call.py`，在 `_wrap_execute` 之前补发对应事件；
没订阅者时直接跳过（对其它部署零开销），发布失败静默降级、不阻断组件执行。

### 打完补丁怎么确认生效

1. 启动后日志里应能看到插件的能力自检行：

   ```
   char_prompt_guard | INFO | char_prompt_guard 运行环境自检：核心提供 before_tool_call，已同时订阅 …
   ```

   若这行是 `WARNING`（核心未提供 `before_tool_call`），说明核心偏旧，先更新核心。

2. 触发一次生图，日志中应出现事件发布记录与补入记录：

   ```
   正在按顺序向 N 个处理器发布事件 'EventType.BEFORE_ACTION_CALL'
   char_prompt_guard | INFO | 已补入角色（share_visual / natural）: …
   ```

   只有第 1 条自检、触发生图后**没有任何** `已补入角色` 记录 → 补丁没生效或没打上。

3. 也可以用自检脚本直接判定补丁是否已打：

   ```bash
   $env:NEO_ROOT="<你的 Neo-MoFox 框架根>"
   & "$env:NEO_ROOT\.venv\Scripts\python.exe" tests\check_patch.py
   ```

### 插件侧已经做的兼容

- **同时订阅 `before_action_call` 与 `before_tool_call`**，并兼容 agent 侧传来的
  工具名格式（`action-share_visual` 这种带前缀的写法）；
- **动作名为空时按提示词参数名兜底匹配**：某些发布方只写旧的 `action_name` 属性，
  而组件是用新版 `name` 声明的（image_generator_plugin-neo 的 `draw_image` 就是这种），
  此时签名会退化成 `插件名:action:`。插件会退一步用「谁的提示词参数真的出现在 args 里」
  唯一命中；**有歧义（多个目标同参数名）时宁可不处理**，避免误改别的动作。
- **加载时打一条能力自检日志**（见上）：核心缺 `before_tool_call` 时直接 WARNING 提示，
  不用再靠猜。

### `min_core_version` 说明

manifest 声明的是 `min_core_version = "1.0.0"`，**故意保持宽松**：核心缺少
`before_tool_call` 时插件会静默降级（只走 `before_action_call`），不会崩、也不该被拒绝加载 ——
把下限抬到 `1.2.0` 反而会让老核心直接拒绝注册这个插件。所以这一项的准确表达方式
是上面的「能力自检日志 + `tests/check_patch.py`」，而不是抬高版本下限。

## 依赖

- Neo-MoFox `>= 1.0.0`（实测基于 `CORE_VERSION = 1.2.0`）
- **框架补丁**：见上节（不打补丁则 agent 聊天器下不生效）。补丁随交付包提供，不在插件包内
- 生图插件（至少一个）：[nai_artist](https://github.com/bingyv92/nai_artist) 或 [image_generator_plugin-neo](https://github.com/tt-P607/image_generator_plugin-neo)

> **本插件仓库/插件包不包含任何上游生图插件的源码**。上面两个插件请从各自仓库自行安装，
> 本插件只通过事件钩子与之协作，不 import 它们的任何模块。这样做既避免版权与维护问题，
> 也避免把上游源码里的示例密钥、网关地址、默认路径等隐私内容一起带进本仓库。

