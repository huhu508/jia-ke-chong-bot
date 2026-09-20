import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import logger

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载插件目录（app/plugins 下的每个 .py 都是插件）
nonebot.load_plugins("app/plugins")

from app.db import init_db  # noqa: E402
from app.services import crypto  # noqa: E402
from app.services.tasks import start_scheduler  # noqa: E402


@driver.on_startup
async def _startup():
    init_db()
    # 旧明文凭据（佳明密码 / COROS token）一次性升级为加密存储，读侧已兼容明文
    n = crypto.migrate_encrypt()
    if n:
        logger.info(f"已将 {n} 个明文凭据迁移为加密存储")
    # 把日志同时写到文件，便于排查问题（data 目录已由 init_db 创建）
    logger.add("data/bot.log", rotation="1 MB", retention=7, encoding="utf-8", level="DEBUG")
    logger.info("数据库初始化完成")
    start_scheduler()
    logger.info("机器人启动完成")


if __name__ == "__main__":
    nonebot.run()
