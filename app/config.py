from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 .env 读取配置，字段名与 .env 中变量名大小写不敏感对应。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库连接
    db_url: str = "sqlite:///data/bot.db"

    # 周聚合触发时间（每周一）
    weekly_hour: int = 0
    weekly_minute: int = 10

    # 每日运动排行播报（默认每晚 23:00，本机本地时间即北京时间）
    broadcast_hour: int = 23
    broadcast_minute: int = 0
    # 显式指定播报群号列表（JSON 数组）；留空 = 自动播报到机器人出现过的所有群
    broadcast_groups: list[int] = []

    # Garmin 佳明（每用户凭据绑定，此处仅区域开关；中国区账号 is_cn=true）
    garmin_is_cn: bool = True

    # COROS 高驰（官方 MCP + OAuth 授权，无需账号密码；region 决定接口区域）
    coros_region: str = "cn"  # cn / us

    # 智谱 GLM 大模型（可选增强层：数据解读/总结；留空 = 自动禁用，机器人照常工作）
    # 免费模型用 glm-4-flash，低价可用 glm-4-air
    zhipu_api_key: str = ""
    zhipu_model: str = "glm-4-flash"


settings = Settings()
