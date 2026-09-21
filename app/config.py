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

    # Garmin 佳明（每用户凭据绑定，此处仅区域开关；中国区账号 is_cn=true）
    garmin_is_cn: bool = True

    # COROS 高驰（官方 MCP + OAuth 授权，无需账号密码；region 决定接口区域）
    coros_region: str = "cn"  # cn / us

    # 智谱 GLM 大模型（可选增强层：数据解读/总结；留空 = 自动禁用，机器人照常工作）
    # 免费模型用 glm-4-flash，低价可用 glm-4-air
    zhipu_api_key: str = ""
    zhipu_model: str = "glm-4-flash"
    # 大模型接口地址（OpenAI 兼容）；默认智谱，可改任意兼容服务
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


settings = Settings()
