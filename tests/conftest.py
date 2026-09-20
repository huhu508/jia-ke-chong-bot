"""pytest 共享 fixture：临时 SQLite + 会话，并确保 ``app`` 包可 import。"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 项目根加入 sys.path，保证 `import app.*` 在任意 cwd 下都能工作
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app.models  # noqa: E402,F401  触发 __init__ 注册全部模型到 Base.metadata
from app.models.base import Base  # noqa: E402


@pytest.fixture()
def db_session(tmp_path):
    """每个测试独立的临时 SQLite + 建表 + 会话（不碰生产库 data/bot.db）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
