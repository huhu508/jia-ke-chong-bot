import logging
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from . import models  # noqa: F401  确保所有模型被导入并注册到 Base.metadata
from .config import settings
from .models.base import Base

logger = logging.getLogger(__name__)

# SQLite 并发写（后台回填历史 + 用户查询同时落库）可能触发 "database is locked"，
# 设 busy_timeout 让写操作等待重试而非立即抛错（仅对 SQLite 生效，其它后端忽略）。
_engine_kwargs: dict = {"future": True}
if settings.db_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"timeout": 30}
engine = create_engine(settings.db_url, **_engine_kwargs)

# SQLite 并发读写：WAL 让读不阻塞写（后台回填历史 + 用户查询可并行），
# synchronous=NORMAL 在 WAL 下既安全又快。仅对 SQLite 生效。
if settings.db_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# 新增列迁移清单：表名 -> {列名: "SQL类型 DEFAULT 默认值"}。
# create_all 只建新表、不会给已存在的表加列，历史库必须靠这里补列。
_MIGRATIONS = {
    "member": {
        "custom_nickname": "VARCHAR(64) DEFAULT ''",
    },
    "daily_record": {
        "ascent_meters": "FLOAT DEFAULT 0",
        "training_load": "FLOAT DEFAULT 0",
        "avg_pace_sec_per_km": "FLOAT DEFAULT 0",
        "avg_hr": "INTEGER DEFAULT 0",
        "max_activity_distance_km": "FLOAT DEFAULT 0",
    },
    "manual_distance": {
        "week_start": "DATE",
        "week_distance_km": "FLOAT DEFAULT 0",
    },
}


def _migrate_columns() -> None:
    """给已存在的表补上缺失的新增列（幂等，只 ADD COLUMN 不删改旧列）。"""
    insp = inspect(engine)
    for table, cols in _MIGRATIONS.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols.items():
            if name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))
            logger.info(f"迁移：表 {table} 新增列 {name}")


def init_db() -> None:
    # SQLite 相对路径需先确保目录存在
    if settings.db_url.startswith("sqlite:///"):
        db_path = settings.db_url.removeprefix("sqlite:///")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate_columns()


def get_session() -> Session:
    return SessionLocal()
