from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 .env 读取配置，字段名与 .env 中变量名大小写不敏感对应。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库连接
    db_url: str = "sqlite:///data/bot.db"

    # 每日运动排行播报（默认每晚 23:00，本机本地时间即北京时间）
    broadcast_hour: int = 23
    broadcast_minute: int = 0
    # 原始训练明细保留天数（365 = 保留最近一年，超过自动清除）。历史明细供个人复盘/历史查询。
    retention_days: int = 365
    # 显式指定播报群号列表（JSON 数组）；留空 = 自动播报到机器人出现过的所有群
    broadcast_groups: list[int] = []
    # 群使用白名单（群号列表）：仅这些群的成员可触发命令 / 被每日播报。
    # 默认放行「甲壳虫跑团」与「test 群聊」；留空 = 不限制（全群可用）。
    allowed_groups: list[int] = [595366460, 1062117913]

    # 打卡计数起点（本学期开学日，YYYY-MM-DD）：此日期之前的记录不统计打卡天数。
    semester_start: str = "2026-09-22"

    # Garmin 佳明（每用户凭据绑定，此处仅区域开关；中国区账号 is_cn=true）
    garmin_is_cn: bool = True

    # COROS 高驰（官方 MCP + OAuth 授权，无需账号密码；region 决定接口区域）
    coros_region: str = "cn"  # cn / us

    # 大模型（Agnes AI，OpenAI 兼容中转站；可选增强层：数据解读/总结/问答/点评；
    # 留空 = 自动禁用，机器人照常工作）
    llm_api_key: str = ""
    # 用 3.0-flash：非推理对话模型，快且稳定。勿换 2.5-flash——它是推理/agent 模型，
    # 慢（易超时）且会把 max_tokens 花在思考上导致 content 为空。
    llm_model: str = "agnes-3.0-flash"
    # 大模型接口地址（OpenAI 兼容 chat/completions 全路径）
    llm_base_url: str = "https://apihub.agnes-ai.com/v1/chat/completions"


settings = Settings()
