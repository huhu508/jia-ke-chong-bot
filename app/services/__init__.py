# 本包按需 import 各子模块（sync / providers / retention / crypto），
# 避免在插件加载阶段因 garminconnect 等依赖引发不必要的 eager import。
