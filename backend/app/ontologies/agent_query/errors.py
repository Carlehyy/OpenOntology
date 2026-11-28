"""Machine-readable errors shared by ontology query planning and execution."""
from __future__ import annotations


class QueryError(Exception):
    """查询面可识别错误，携带 HTTP 状态与机器可读 code。

    ``assistant_private_keys`` 标记助手恢复链路专用的 extra，公共直查不回显。
    """

    def __init__(self, status: int, code: str, message: str, *,
                 assistant_private_keys: tuple[str, ...] = (), **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra
        self.assistant_private_keys = tuple(assistant_private_keys)

    def detail(self) -> dict:
        private = set(self.assistant_private_keys)
        return {"code": self.code, "message": self.message,
                **{key: value for key, value in self.extra.items() if key not in private}}
