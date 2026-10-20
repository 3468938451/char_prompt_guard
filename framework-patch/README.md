# 框架补丁：让 agent 聊天器路径也发布「执行前事件」

> 本目录是 `char_prompt_guard` 的**框架侧依赖**。不打补丁，插件在 agent 聊天器
> （`default_chatter` / `neo_default_chatter`）下收不到任何通知，表现是「装上毫无反应」。
>
> **1.0.34 起补丁变成可选的**：插件加载时会**自己探测**这条路径有没有在发执行前事件 ——
> 没发就在**内存里**给 `run_tool_call` 补一层包装（等价本目录的补丁**二版**，不写任何文件），
> 所以绝大多数部署**不再需要**手工打补丁；本补丁仍然保留，用于「要覆盖所有执行路径」
> （例如 `BaseAgent` 的 `exec_llm_usable`）或「探测认不出」的场合。两者**自动二选一**：
> 打过补丁 → 插件日志 `检测到框架已含执行前事件（静态补丁），未重复包装`，一行代码都不执行。
> 详见插件 README 的
> 「框架兼容：静态补丁 或 运行时兼容（1.0.34 起，二选一、自动检测）」一节。

## 为什么必须打（已在本地上游源码核实）

`src/core/utils/llm_tool_call.py` 的 `create_llm_usable_execution()`，在
`should_strip_auto_reason_argument` 之后**直接**执行组件，中间没有任何事件发布：

```python
# 上游原始代码（本机核对位置：src/core/utils/llm_tool_call.py:170）
execution = instance._wrap_execute(**call_kwargs)
```

三条路径的对照：

| 执行路径 | 发出的事件 |
| --- | --- |
| `ActionManager.execute_action` | ✅ `before_action_call` |
| `ToolManager.execute_tool` | ✅ `before_tool_call` |
| **agent 聊天器**：`run_tool_call` → `create_llm_usable_execution` → `_wrap_execute` | ❌ **什么都不发** |

`plugins/default_chatter/plugin.py` 确实走 `run_tool_call`（本机核实在
`plugins/default_chatter/plugin.py:218`），所以在最常见的用法下，靠「执行前改写参数」
的插件永远不会被触发。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `llm_tool_call-pre-events.patch` | **推荐使用**：修正版，动作名以新版 `name` 解析（见下节） |
| `llm_tool_call-pre-events.orig.patch` | 原交付补丁，保留用于对照；**对 image_generator_plugin-neo 无效** |

两者都只改一个文件 `src/core/utils/llm_tool_call.py`：

- `BaseAction` → 补发 `before_action_call`（字段：`signature` / `action_name` / `action_description` / `args` / `message`）
- `BaseTool` → 补发 `before_tool_call`（字段：`signature` / `tool_name` / `tool_description` / `args` / `message`）

设计上与既有发布点一致：先 `get_subscribers()` 判断（没订阅者直接跳过，零开销）、
发布异常静默降级不阻断执行、插件对 `args` 的改写会回写到真实执行参数。

## 修正版改了什么（原补丁的缺口）

原补丁用 `getattr(instance, "action_name", "")` 取动作名。但 `action_name` 是**旧属性**：

- nai_artist 的 `ShareVisualAction` 用旧属性声明（`action_name = "share_visual"`）→ 有值，原补丁可用；
- **image_generator_plugin-neo 的 `DrawAction` 用新版 `name = "draw_image"` 声明** →
  `action_name` 是空串 → 事件里签名退化成 `image_generator_plugin-neo:action:`、
  `action_name` 也是空 → **插件认不出这个动作，neo 全程静默失效**。

修正版改成以新版属性优先：

```python
_action_name = str(
    getattr(instance, "name", "") or getattr(instance, "action_name", "")
)
"signature": f"{getattr(plugin, 'plugin_name', '')}:action:{_action_name}",
"action_name": _action_name,
```

`BaseTool` 分支同样处理（`name` → `tool_name`），描述字段也做了
`description` → 旧属性 的回退。

### 实测对照（同一事件形态，只换插件版本）

```
场景：旧补丁形态（动作名为空）
  修订版插件(D盘交付) : 未接管（静默失效）
  本工作区插件(+兜底) : 补入成功
场景：修正补丁形态（动作名写全）
  两者均补入成功
```

> 插件侧另外做了「动作名为空时按提示词参数名兜底匹配」的自保逻辑
> （有歧义时不处理），所以**即使你只打了原补丁**，neo 也不会静默失效了；
> 但修正补丁才是根因修复，两个一起用最稳。

## 怎么打

```bash
cd <你的 Neo-MoFox 框架根>

# 推荐：修正版
git apply framework-patch/llm_tool_call-pre-events.patch

# 若 git apply 失败，可加 --3way；仍失败就照 patch 内容手工改同名文件的同名位置
git apply --3way framework-patch/llm_tool_call-pre-events.patch
```

打完重启 bot，日志里应能看到：

```
正在按顺序向 N 个处理器发布事件 'EventType.BEFORE_ACTION_CALL'
char_prompt_guard | INFO | 已补入角色（share_visual / natural）: ...
```

## 上游状态

原交付方已提 PR：MoFox-Studio/Neo-MoFox **#171**
（https://github.com/MoFox-Studio/Neo-MoFox/pull/171）

> 上游对外部 PR 响应较慢，别把它当可依赖的时间表；先自己留一份补丁。

## 本机验证记录

| 项 | 结果 |
| --- | --- |
| 原补丁 `git apply --check`（上游 master 副本） | ✅ 可应用 |
| 修正版 `git apply --check` | ✅ 可应用 |
| 修正版补丁应用后 `py_compile` | ✅ 通过 |
| 补丁文件编码 | 无 BOM，UTF-8 |
