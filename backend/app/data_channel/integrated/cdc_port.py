"""CDC ontology port — 协议 + fail-closed 单例（组合根注入适配器）。

方向约束：本模块属于 ``data_channel`` 域，只声明 CDC 控制面消费的协议，
绝不 import ``ontologies``；适配器（``ExternalCdcOntologyAdapter``，在
ontologies 侧基于不可变已发布远程映射与 Sentinel 事实/发件箱实现）由组合
根（main.py / runtime_composition）经 :func:`set_cdc_ontology_port` 注入，
先例见 ``app.ontologies.agent_confirmation.write_port``（方向相反的同构
模式：协议端与实现端互不 import，结构化类型即契约）。

协议语义（冻结，蛇形键）：

``subscriptions(db) -> list[dict]``
    从不可变已发布远程映射推导的期望订阅全集。每项：

    ===============  =====================================================
    ``ontology_id``  本体 id（str，必填）
    ``release_id``   发布版本 id（str，必填；发布快照不可变）
    ``mapping_id``   对象映射 id（str，必填）
    ``object_type_id`` 对象类型 id（str，必填）
    ``asset_id``     集成资产 id（str，必填）
    ``connection_id`` 受管连接 id（str，必填）
    ``schema_name`` / ``table_name``  物理库表（str，必填）
    ``schema_fingerprint``  物理结构指纹（str，必填）
    ``logical_fingerprint`` 逻辑视图指纹（str | None）
    ``field_map``    物理源列名 -> 本体属性名（dict，必填；键值均为物理/
                     属性 *NAME*，不含任何列序号或别名层）
    ``primary_key_columns``  物理主键列（list[str]，必填，有序）
    ``contract``     ``{"columns": [...], "object_properties": [...]}``
                     （dict，必填；发布时冻结的列与对象属性契约）
    ===============  =====================================================

    同一 ``(ontology_id, release_id, mapping_id)`` 不得出现两项。

``capture(db, event: dict) -> dict``
    在调用方事务内写入 Sentinel 事实 + 发件箱；**绝不自行 commit**。
    ``event`` 载荷形状（含属性变更的 ``{"old": v, "new": v}`` 前后像）由
    适配器与 ingress 的契约定义，本端口不重复声明。返回值至少携带
    ``status``，取值冻结为 ``captured`` | ``duplicate`` | ``stale`` |
    ``capacity_exceeded``（见 ``CAPTURE_STATUSES``）。

``capture_many(db, events: list[dict]) -> list[dict]``
    同一源行向多个本体/映射扇出的批量形态（必选，无逐条回退）；锁序约束见
    协议方法 docstring。

``activation_position`` 约定（控制面存储的 JSON，运行时锚定写入）：

- MySQL: ``{"dialect": "mysql", "file": str, "pos": int, "row": int}``
- PostgreSQL: ``{"dialect": "postgresql", "lsn": int,
  "data_collection_order": int}``

事件坐标须严格大于订阅的激活前沿；该比较由 ingress/适配器负责，控制面只
原样存储位置，不做任何坐标比较或去重。
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from sqlalchemy.orm import Session

# capture() 返回状态的冻结词表（适配器与运行时共享，不得扩充语义）。
CAPTURE_STATUSES = ("captured", "duplicate", "stale", "capacity_exceeded")


class CdcOntologyPortError(RuntimeError):
    """CDC 本体端口未装配或装配非法（fail-closed）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@runtime_checkable
class CdcOntologyPort(Protocol):
    """CDC 本体适配器协议（ontologies 侧实现，组合根注入）。"""

    def subscriptions(self, db: Session) -> list[dict]:
        """期望订阅全集（见模块 docstring 的冻结形状）。"""
        ...

    def capture(self, db: Session, event: dict) -> dict:
        """事实 + 发件箱写入（调用方事务内，不自行 commit）。"""
        ...

    def capture_many(self, db: Session, events: list[dict]) -> list[dict]:
        """同一源行向多个本体/映射扇出的批量写入（调用方事务内，不 commit）。

        必选方法，无逐条 ``capture`` 回退：适配器必须先对批内全部项目行取
        FOR KEY SHARE，再取全局容量锁，杜绝“持容量锁后再取项目锁”的锁序
        反转。返回逐事件结果列表（预期与 ``events`` 等长同序，每项形状同
        ``capture`` 的返回，``status`` 取自 ``CAPTURE_STATUSES``）。
        """
        ...


_port: Optional[CdcOntologyPort] = None


def set_cdc_ontology_port(port: CdcOntologyPort) -> None:
    """组合根注册（幂等覆盖）；None 拒绝以保持 fail-closed。"""
    global _port
    if port is None:
        raise CdcOntologyPortError(
            "cdc_ontology_port_invalid", "CDC 本体端口不能为空")
    _port = port


def clear_cdc_ontology_port() -> None:
    """测试钩子：恢复未装配的 fail-closed 默认。"""
    global _port
    _port = None


def get_cdc_ontology_port() -> CdcOntologyPort:
    if _port is None:
        raise CdcOntologyPortError(
            "cdc_ontology_port_unavailable",
            "CDC 本体端口未装配（组合根未注入 ontologies 适配器），"
            "拒绝期望订阅 reconcile")
    return _port


def cdc_ontology_port_ready() -> bool:
    return _port is not None
