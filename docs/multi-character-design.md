# 多角色同框补全 — 设计方案（已实现）

> ⚠️ 本文是改造过程的**设计稿**，部分取值与最终实现不同：`multi.enabled` 最终
> **默认开启**；坐标按 `1/(n+1)` 均匀分散（2 人 0.333/0.667）；`strategy_neo`
> 只有 `characters` / `append_tags` 两个取值；另外实现里补上了「候选过滤要在
> 问 LLM 之前」与「`multi_only` 用上文角色数判断」两条修正。
> **以 README 和 config.py 为准。**

> 目标：把原来「只补 1 个角色」升级为「按需补多个角色」。

## 0. 最终落地的决策（与初版方案的差异）

| 决策点 | 方案初稿 | 最终实现 |
| --- | --- | --- |
| 默认开关 | 默认关闭 | **默认开启**（`multi.enabled = true`） |
| 检测范围 | 打算提供 `always` | **`scope = multi_only`**：仅当候选 ≥2 时才启用集合式检测，单人场景 100% 沿用旧逻辑 |
| neo 多人注入 | 待定 | **写入 `characters` 独立角色槽位**（`strategy_neo = characters`），单人仍追加标签 |
| 上限 | 2 或 6 待定 | **6**（`max_characters = 6`） |
| 自动切换 | 未涉及 | **新增「由 bot 决定」**：主模型标记（`#multi#`/`#solo#`/`#tags#`/`#chars#`/`#nochar#`）+ LLM `multi` 判定 + 候选数量兜底；并通过 system reminder 告知主模型标记用法 |

实现文件：`character.py`（集合化与提取）、`handler.py`（判定与注入）、`config.py`（`[multi]` 配置）、`plugin.py`（reminder 注入）。

## 1. 改造前的现状回顾

原逻辑本质是**单角色、开关式**的：

| 环节 | 原实现 | 多角色场景下的问题 |
| --- | --- | --- |
| 检测 | `find_character_in_text()` 命中**任一**角色即整体放行 | 2 人只写了 1 人 → 直接放行，另 1 人不补 |
| 上下文提取 | `find_latest_character()` 只返回**最近提及的一个** | 双人场景只拿到 1 个 |
| LLM 兜底 | 输出单个 `{name, game, tag}` | 同上 |
| 注入 | 单条文本 | 无法表达多人 |

**结论**：多角色场景目前只保证"被讨论得最热的那个"被补上。

## 2. 生图插件的硬上限（来自源码实测）

| 生图插件 | 多角色机制 | 上限 | 说明 |
| --- | --- | --- | --- |
| nai_artist | ❌ 无（`characterPrompts: []`、`char_captions: []`、`use_coords: False`） | 可靠 1~2 人 | 只能把多人写进单个 `base_caption`，≥3 人特征互串 |
| image_generator_plugin-neo | ✅ `characters` JSON 参数（映射到 `characterPrompts`/`char_captions` + `use_coords`） | V4.5 **6 人**；V5 **32 人**（原创角色建议 ≤6） | 坐标 `x/y` 为 0~1 浮点；支持 `source#/target#/mutual#` 互动标签 |

**因此本插件的设计上限**：对 neo 最多 6（V4.5）/32（V5）→ 默认按 **6** 设防；对 nai_artist 默认按 **2** 设防。

## 3. 核心概念：改为「角色集合」比对

```
present     = 提示词里已经出现的角色集合
candidates  = 最近上文中被提及的角色（按最近一次提及时间倒序）
missing     = candidates - present，截断到 max_characters
if missing 非空 → 注入 missing
```

与现状的差别：**从"命中一个就整体放行"变成"逐个角色判断缺失"**。

### 3.1 检测（present 集合）

对角色表每一项：

- 关键词命中（角色名 + 别名，沿用现有英文单词边界规则）；
- `tags` 模式额外比对标准角色标签（沿用 `content_contains_tag` 归一化）；
- 额外扫描目标声明的 `detect_args`（neo 的 `characters`）。

### 3.2 候选提取（candidates 列表）

两级来源，保持"零成本优先"：

1. **角色表扫描**：新增 `find_characters_in_history()`，返回去重后的列表（按最近提及倒序）；
2. **LLM 兜底 / 补全**：输出结构改为数组
   ```json
   {"characters": [{"name": "爱莉希雅", "game": "崩坏三 (Honkai Impact 3rd)",
                    "tag": "elysia \\(honkai impact 3rd\\)"}]}
   ```
   指令明确：按"当前请求的相关度"排序；只列真正被指代的角色；不确定就不要列。

`llm_mode` 控制何时问 LLM：

| 取值 | 行为 | 代价 |
| --- | --- | --- |
| `fallback`（默认） | 角色表完全没命中时才问 | 最省 token |
| `always` | 每次都问，用于补全"角色表外的人" | 每次生图多一次小模型调用 |

### 3.3 注入（按目标策略）

| 目标插件 | 缺失角色数 | 注入方式 |
| --- | --- | --- |
| nai_artist（natural） | 1 | 追加 1 条中文说明（同现状） |
| nai_artist（natural） | ≥2 | 追加多段中文说明，超过 2 时截断并 WARN（其无多角色机制） |
| neo（tags） | 1 | 追加标签到 `content_description`（同现状） |
| neo（tags） | ≥2（`strategy_neo=auto`） | 写入 `characters` JSON 参数（真正的多角色通道），坐标按下节处理 |
| neo（tags） | ≥2（`strategy_neo=append_tags`） | 全部追加进 `content_description`，并 WARN"可能导致角色互串" |

**顺序**：按"最近提及"倒序写入——NAI 提示词越靠前权重越高，把当前最相关的角色放前面。

### 3.4 坐标问题（多角色最大的不确定性）

`characters` 参数的 `x/y` 无法准确推断。三个可选方案：

| 方案 | 做法 | 评价 |
| --- | --- | --- |
| A（默认） | 不写 `x/y`，让 neo 走默认值 0.5/0.5 | ❌ 两人重叠在画面中心，多人不可用 |
| B（推荐） | 按下标均匀分散：2 人 `0.35/0.65`，3 人 `0.25/0.5/0.75`，y 统一 0.5 | ✅ 稳定可预期，但构图死板 |
| C（实验） | 让 LLM 结合画面描述猜坐标 | ⚠️ 可能猜错、可能重叠，需大量实测 |

**建议**：默认 B，坐标可配置（`coord_plan = "spread" | "llm" | "none"`）。

### 3.5 精确性问题：不要"擅自加人"

上文可能提到过 3 个角色，但用户这次只要画其中 2 个。控制手段：

- `recent_only = true`（默认）：候选只取**最近 N 条消息**（`recent_messages = 6`）内的提及，而不是整个 `history_hours` 窗口；
- 优先级：触发消息（`message.processed_plain_text`）里提到的角色排最前；
- `max_characters` 截断。

## 4. 新增配置（全部有默认值，向后兼容）

```toml
[multi]
enabled = false          # 总开关；false 时行为与 1.0.0 完全一致
scope = "multi_only"     # multi_only=仅当候选≥2 时才启用集合逻辑（单人场景行为完全不变）；always=所有场景都逐角色比对
max_characters = 2       # 单次最多补入几个角色（1~6，超出截断并 WARN）
llm_mode = "fallback"    # fallback | always
recent_only = true       # 候选只取最近 N 条消息
recent_messages = 6      # recent_only 的窗口大小
strategy_neo = "auto"    # auto | append_tags | characters_only
coord_plan = "spread"    # spread | llm | none
```

`[targets]` 无需改动；新增的可选段（如第 6 段）暂不需要。

### 4.1 `scope` 为什么重要（把行为变化关进多人场景）

集合式检测与现状最大的差别在**单人场景**：

| 场景 | 现状（命中即放行） | `scope="always"` | `scope="multi_only"`（推荐） |
| --- | --- | --- | --- |
| 提示词写了 A，上文聊 A | 放行 | 放行 | 放行 |
| 提示词没写角色，上文聊 A | 补 A | 补 A | 补 A |
| 提示词写了 A，上文聊 B | 放行 | 补 B（行为变了） | **放行（同现状）** |
| 候选 ≥2（A、B 都被聊到） | 命中即放行 / 只补 1 个 | 逐角色补缺失 | 逐角色补缺失 |
| 提示词写 A，上文聊 A+B | 放行 | 补 B | 补 B |

`multi_only` 的判定规则：先算候选集合，**只有候选 ≥2 时才切换到集合式检测**；候选 ≤1 时完全沿用旧规则。这样"单人场景零变化"，新逻辑只作用于它真正要解决的多人场景。

## 5. 兼容与回滚

- **`multi.enabled = false`** 时回到单角色行为（命中任一角色即放行、只补最近讨论的那一个），
  可逐字节回归验证；
- 默认值是 `true`（按需求确认结果），出问题只需把开关关回或把 `scope` 改成配置项之外的值；
- 版本：**1.0.0**（首发版）。注意：交付前存在一份同名 `1.0.0` 的预发布构建，**已废弃**，
  两者 sha256 不同，请以交付说明里登记的 sha256 为准。

## 6. 验证方式（已从"测试计划"落实为脚本）

| 脚本 | 覆盖 |
| --- | --- |
| `tests/check_import.py` | 真实导入、manifest 字段、api_version 模块存在性、组件声明一致、配置默认值 |
| `tests/check_logic.py` | 19 条用例：natural/tags 模式、多角色、运行时标记、自定义目标表、工具调用路径、动作名缺失兜底 |
| `tests/check_patch.py` | 框架补丁状态（未打 / 原版 / 修正版）判定 |

## 6. 测试计划

| 类型 | 用例 |
| --- | --- |
| 回归 | `multi.enabled=false` 时，现有 16 个小节全部通过且行为不变 |
| 检测 | 0/1/2/3 个角色出现在提示词；标签转义差异；`characters` 参数命中 |
| 候选 | 最近提及倒序、去重、`recent_only` 窗口、上限截断 |
| 注入 | natural 单/多段；tags 单标签；≥2 走 `characters` JSON；坐标分布正确 |
| 边界 | 角色无标签 → 跳过；`characters` 非法 JSON → 不破坏原值；未知目标 → 不处理 |
| 失败 | LLM 超时/返回非 JSON → 退化为角色表结果 |

## 7. 工作量与风险

- 工作量：约「1 个模块 + 2 处配置 + 6 组用例」，改动集中在 `character.py`（集合化）与 `handler.py`（注入策略）；
- 主要风险：**坐标**（构图死板或重叠）与**多标签互串**（NAI 本身的限制，非本插件引入）；
- 缓解：默认关闭 + 坐标方案可切换 + 超限 WARN + 单角色行为完全不变。

## 8. 待你确认的三个决策点

1. 是否按"默认关闭、可选开启"交付？（推荐）
2. neo 的 ≥2 角色默认走 `characters` 参数（方案 B 分散坐标），还是先只做"追加标签"？
3. `max_characters` 默认取 2 还是 6？
