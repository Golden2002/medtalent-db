# -*- coding: utf-8 -*-
"""
code/demo/_portal_shared.py —— 门户与可视化/开发者模式共享的**异常类型**

存在的唯一理由：PortalError 必须是**同一个类对象**。

踩过的坑（真实发生过，且表现极具误导性）：
    `python code/demo/portal.py --serve`     ← 这种启动方式下 portal.py 是 `__main__`
    portal_viz.py 里 `import portal as P`     ← 于是又加载出**第二个** portal 模块

两个模块对象各自定义一份 `class PortalError`，`P.PortalError is not
__main__.PortalError`。结果：`portal_viz` 抛出的 PortalError 在 `do_GET` 的
`except PortalError` 里**匹配不上**，异常穿透到 socketserver —— 客户端看到的是
"连接被重置"（连 404 都拿不到），服务端只留一段 traceback。

所以异常类型必须抽到这个**谁都不依赖、也没有任何依赖**的小模块里：
    · `python portal.py`            → portal 是 __main__，但共享模块仍是同一个
    · `import portal`（测试/其他）    → portal 是普通模块
两条路都指向 `_portal_shared.PortalError`，类对象唯一。
"""
from __future__ import annotations


class PortalError(Exception):
    """带 HTTP 状态码的业务错误（未知表名、非法 SQL、参数不合法……）。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message
