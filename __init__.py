"""char_prompt_guard — 角色提示词守卫插件包。

插件入口在 :mod:`plugin` 模块中由 ``@register_plugin`` 装饰的
``CharPromptGuardPlugin`` 类注册，通过插件加载器自动发现。

注意：此文件不要 re-export 子模块符号。插件加载器以
``spec_from_file_location`` 直接加载 ``char_prompt_guard.plugin``，
此时父包尚未完成初始化；若本文件反向 import ``.plugin`` 会形成
循环导入，导致 ``cannot import name 'CharPromptGuardPlugin'``。
"""

__all__: list[str] = []
