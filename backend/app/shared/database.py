from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.shared.config import settings
from app.shared.exact_json import dumps_exact

_is_sqlite = settings.database_url.startswith("sqlite")

_kwargs: dict = {"pool_pre_ping": True}
if _is_sqlite:
    _kwargs["connect_args"] = {"check_same_thread": False}
else:
    _kwargs["pool_size"] = 5
    _kwargs["max_overflow"] = 10
# 所有引擎（含 SQLite 测试引擎，见 tests/conftest.py）使用同一个 serializer：
# 原生 JSON 值由 stdlib json.dumps 逐字节输出；只有 Decimal 落成精确数值
# token，PG json 列逐字保留 scale，typed 读端 ->> + NUMERIC 才有源字面。
_kwargs["json_serializer"] = dumps_exact

engine = create_engine(settings.database_url, **_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass
