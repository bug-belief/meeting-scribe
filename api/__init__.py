"""API 分包（O-11）。

每个子模块暴露 `register(app)`，把一组路由挂到 app 上。
`server.create_app()` 只做装配：建 app、放 state、按顺序调 register。

拆分顺序（C-2 → C-5）：pages / health → meetings / speakers → assistant / kb / voice → live。
"""
