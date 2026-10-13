# 框架补丁：让 agent 聊天器路径也发布执行前事件

## 为什么需要它

`char_prompt_guard` 靠订阅 `before_action_call` 事件，在生图 Action 执行前改写提示词。
但上游框架在 **agent 聊天器路径**下不发布这个事件：

```
default_chatter / neo_default_chatter
  → run_tool_call
    → llm_tool_call.create_llm_usable_execution
      → instance._wrap_execute(**kwargs)      ← 直接执行，不发任何事件
```

对照：`ActionManager.execute_action`（发 `before_action_call`）和
`ToolManager.execute_tool`（发 `before_tool_call`）才是既有发布点，但 agent 聊天器
不经过它们。结果就是插件装上后**毫无反应**，日志里连一条记录都没有。

## 它做什么

只改一个文件 `src/core/utils/llm_tool_call.py`（+51 行）：

在 `create_llm_usable_execution` 调用 `_wrap_execute` 之前，按组件类型补发执行前事件：

| 组件类型 | 事件 | 字段 |
| --- | --- | --- |
| `BaseAction` | `before_action_call` | `signature` / `action_name` / `action_description` / `args` / `message` |
| `BaseTool` | `before_tool_call` | `signature` / `tool_name` / `tool_description` / `args` / `message` |

设计上与既有发布点保持一致：

- 先 `get_subscribers()` 判断，没有订阅者直接跳过（对没装相关插件的部署零开销）；
- 事件发布异常静默降级，不阻断组件执行；
- 插件对 `args` 的改写回写到实际执行参数。

## 怎么打

```bash
cd <你的 Neo-MoFox 框架根>
git apply framework-patch/llm_tool_call-pre-events.patch
```

失败时可以：

```bash
git apply --3way framework-patch/llm_tool_call-pre-events.patch
# 或者直接看 patch 内容，手工改 src/core/utils/llm_tool_call.py 同名位置
```

打完重启 bot，日志里应能看到：

```
正在按顺序向 N 个处理器发布事件 'EventType.BEFORE_ACTION_CALL'
```

## 上游状态

已提 PR：MoFox-Studio/Neo-MoFox **#171**
https://github.com/MoFox-Studio/Neo-MoFox/pull/171

> 截至交付时上游对外部 PR 响应很慢（同期外部 PR 已挂数周至两个多月，只有 AI 机器人
> 自动评论）。建议自己留着这份 patch，别等。
