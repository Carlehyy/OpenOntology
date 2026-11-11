"""integrated CDC 执行器：每个受管连接一个 Debezium Server 子进程的监督者。

进程模型（对应 docker-compose.cdc.yml 的 ``integrated_cdc_executor`` 常驻
服务，无宿主端口、无 docker.sock）：

- 每 2 秒一拍：``cdc_control.reconcile`` 收敛 SQL 期望态，随后对每个源行
  收敛——待清理源（``cleanup_sources``：停止未确认或已无订阅）优先走休眠
  清理；有期望订阅 → 维持流式；暂停（凭据轮换等）→ 停子进程但保留槽与
  偏移；
- 新订阅先随期望 include 集进入子进程（旧子进程先停再起），等该子进程
  出现真实流证据（接收器确认事件/心跳）且健康探测显式 UP 后，才捕获当前
  前沿并锚定激活：覆盖从不宣称早于实际起流，锚定前的区间显式不覆盖、
  绝不回填；激活被拒（源暂停/换代竞态）的订阅下一拍重试；
- 偏移/模式历史在（连接, 代）专用持久卷目录：订阅增删与凭据轮换同代续用，
  物理换库由控制面换代表示；执行器重启（SIGTERM/宕机）绝不清理活跃源的槽；
- 模式漂移按连接级 fail-closed：任一订阅表漂移即整源暂停（下一拍停流），
  首版不做连接内单表隔离；
- HTTP sink 指向同进程 127.0.0.1 临时端口的接收器（``cdc_receiver``）；
  每次子进程启动轮换随机 standard-webhooks 密钥（旧进程在途请求立即失
  效），密钥只写入 0600 临时配置文件；
- ``active`` 只在「子进程存活 + 健康探测显式 UP + 接收器在新鲜窗口内确认
  过事件/心跳」同时成立时上报；进程拉起本身不构成 active 证据，长期无任
  何流证据的启动最终按错误回报；
- 日志缺口（WAL/binlog 已回收、自有槽失效）显式上报 gap，绝不以重新快照
  或静默跳过兜底；PG 续流前校验自有槽存在且未失效；
- 每源一把会话级咨询锁租约：多副本/滚动重叠时同一源只有一个子进程；
- 子进程输出只做分类（稳定错误码），不转发进日志；子进程环境只含
  PATH/JAVA_HOME/LANG/TZ，平台数据库与加密密钥绝不进入子进程；
- 退出路径（SIGTERM/SIGINT/异常）按进程组终止并回收全部子进程与接收器。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.integrated import cdc_connector as connector
from app.data_channel.integrated import cdc_runtime as runtime
from app.data_channel.integrated.models import (
    IntegratedCdcSource,
    IntegratedCdcSubscription,
)

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = float(
    os.environ.get("CDC_RECONCILE_INTERVAL_SECONDS", "2"))
HEARTBEAT_FILE = Path(os.environ.get(
    "CDC_EXECUTOR_HEARTBEAT_FILE", "/tmp/integrated_cdc_executor.heartbeat"))
STATE_ROOT = Path(os.environ.get("CDC_STATE_ROOT", "/var/lib/openontology-cdc"))
RUN_ROOT = Path(os.environ.get("CDC_RUN_ROOT", "/tmp/cdc"))
JAVA_HOME = os.environ.get("JAVA_HOME", "/opt/java/openjdk")
JAVA_BINARY = os.environ.get("CDC_JAVA_BINARY", f"{JAVA_HOME}/bin/java")
DEBEZIUM_HOME = os.environ.get("CDC_DEBEZIUM_HOME", "/opt/debezium")
# 实测自固定摘要镜像 quay.io/debezium/server:3.7.1.Final（runner + config/lib
# + lib/*，与镜像内 run.sh 的类路径一致；不引入 lib_opt）。
DEBEZIUM_CLASSPATH = (
    f"{DEBEZIUM_HOME}/debezium-server-dist-3.7.1.Final-runner.jar"
    f":{DEBEZIUM_HOME}/config/lib:{DEBEZIUM_HOME}/lib/*")
DEBEZIUM_MAIN = "io.debezium.server.Main"
MAX_SOURCES = int(os.environ.get("CDC_MAX_SOURCES", "2"))
QUARKUS_PORT_BASE = int(os.environ.get("CDC_QUARKUS_PORT_BASE", "21024"))
CHILD_JAVA_OPTS = shlex.split(os.environ.get(
    "CDC_CHILD_JAVA_OPTS",
    "-Xms64m -Xmx256m -XX:MaxDirectMemorySize=64m "
    "-XX:ActiveProcessorCount=2 -Djava.awt.headless=true"))
# 证据新鲜窗口：数倍于子进程心跳间隔并留出观测余量
EVIDENCE_FRESH_SECONDS = max(15.0, connector.HEARTBEAT_INTERVAL_MS / 1000 * 5)
# 子进程存活但从未见过任何流证据：超过该时限按错误回报（启动卡死可观测）
NO_EVIDENCE_ERROR_SECONDS = float(
    os.environ.get("CDC_NO_EVIDENCE_ERROR_SECONDS", "300"))
CHILD_STOP_GRACE_SECONDS = 15.0
SHUTDOWN_BUDGET_SECONDS = 25.0
RETRY_SECONDS = 30.0


class ChildProcess:
    """Debezium Server 子进程（独立进程组）；输出只分类不转发。"""

    def __init__(self, args: list[str], *, cwd: str, env: dict):
        self._process = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.error_code: str | None = None
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        stream = self._process.stdout
        for raw in iter(stream.readline, b""):
            code = runtime.classify_child_output(
                raw.decode("utf-8", errors="replace"))
            if code is not None:
                self.error_code = runtime.merge_error_codes(self.error_code, code)
        stream.close()

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        if self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self._process.terminate()

    def kill_group(self) -> None:
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if self._process.poll() is None:
            self._process.kill()

    def wait(self, timeout: float) -> bool:
        try:
            self._process.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def reap(self) -> None:
        self._process.wait()
        self._reader.join(timeout=5.0)

    def stop(self, *, grace: float = CHILD_STOP_GRACE_SECONDS) -> None:
        self.terminate()
        if not self.wait(grace):
            self.kill_group()
        self.reap()
        self.kill_group()  # 清扫进程组内残留（若有）


class SourceLease:
    """每源一把会话级 PostgreSQL 咨询锁（专用连接持有）。

    多副本或滚动重启重叠时，同一源只有一个执行器会拉起子进程，避免两个
    Debezium 争用同一复制槽与偏移文件。锁随专用连接存活：执行器宕机自动
    释放；连接断开即视为全部租约丢失，调用方必须先停全部子进程再重建。
    元数据库非 PostgreSQL（本地/单测 SQLite）没有多副本场景，恒视为持有。
    """

    def __init__(self, engine):
        self._engine = engine
        self._postgres = engine.dialect.name == "postgresql"
        self._connection = None
        self._held: set[str] = set()

    @staticmethod
    def _key(connection_id: str) -> int:
        digest = hashlib.sha256(
            f"integrated-cdc-source|{connection_id}".encode()).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def _conn(self):
        if self._connection is None:
            self._connection = self._engine.connect().execution_options(
                isolation_level="AUTOCOMMIT")
        return self._connection

    def acquire(self, connection_id: str) -> bool:
        if connection_id in self._held:
            return True
        if self._postgres and not self._conn().execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"),
                {"key": self._key(connection_id)}).scalar():
            return False
        self._held.add(connection_id)
        return True

    def release(self, connection_id: str) -> None:
        if connection_id not in self._held:
            return
        self._held.discard(connection_id)
        if self._postgres and self._connection is not None:
            try:
                self._connection.execute(
                    sa.text("SELECT pg_advisory_unlock(:key)"),
                    {"key": self._key(connection_id)})
            except sa.exc.SQLAlchemyError:
                self.reset()

    def healthy(self) -> bool:
        if not self._postgres or self._connection is None:
            return True
        try:
            self._connection.execute(sa.text("SELECT 1"))
            return True
        except sa.exc.SQLAlchemyError:
            return False

    def reset(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except sa.exc.SQLAlchemyError:
                pass
        self._connection = None
        self._held.clear()


@dataclass
class _Runtime:
    connection_id: str
    generation: int
    index: int
    names: dict
    state_dir: Path
    run_dir: Path
    child: ChildProcess | None = None
    properties_digest: str | None = None
    secret: bytes | None = None
    sink_url: str | None = None
    # 子进程/启动路径的最近失败码（进程不在时据此回报 error/gap）
    error_code: str | None = None
    # 子进程仍在流式但期望态未收敛（如新表预检失败）时的回报码
    pending_error: str | None = None
    failures: int = 0
    next_start_at: float = 0.0
    started_wall: float = 0.0
    ever_evidence: bool = False


class CdcSupervisor:
    """监督者主体；控制面/接收器/连接器/子进程工厂/租约均可注入以便测试。"""

    def __init__(self, *, session_factory=None, control=None, receiver=None,
                 connector_module=connector, spawn=None, lease=None,
                 state_root: Path = STATE_ROOT, run_root: Path = RUN_ROOT,
                 max_sources: int = MAX_SOURCES,
                 interval: float = RECONCILE_INTERVAL_SECONDS,
                 evidence_window: float = EVIDENCE_FRESH_SECONDS,
                 no_evidence_after: float = NO_EVIDENCE_ERROR_SECONDS,
                 health_probe=None, wall_clock=time.time,
                 heartbeat_file: Path = HEARTBEAT_FILE):
        if session_factory is None:
            from app.shared.database import SessionLocal as session_factory
        if control is None:
            from app.data_channel.integrated import cdc_control as control
        self._session_factory = session_factory
        self._control = control
        self._connector = connector_module
        self._spawn = spawn or (
            lambda args, cwd, env: ChildProcess(args, cwd=cwd, env=env))
        self._receiver = receiver
        self._lease = lease
        self._state_root = Path(state_root)
        self._run_root = Path(run_root)
        self._max_sources = max(1, int(max_sources))
        self._interval = interval
        self._evidence_window = evidence_window
        self._no_evidence_after = no_evidence_after
        self._health_probe = health_probe or probe_health
        self._wall = wall_clock
        self._heartbeat_file = Path(heartbeat_file)
        self._runtimes: dict[str, _Runtime] = {}
        self._free_indexes = list(range(self._max_sources))
        self._cleanup_retry_at: dict[str, float] = {}
        self._stop = threading.Event()

    # ── 主循环 ──────────────────────────────────────────────────────

    def run(self) -> int:
        if self._receiver is None:
            from app.data_channel.integrated import cdc_receiver
            self._receiver = cdc_receiver.create_receiver(
                host="127.0.0.1", port=0, session_factory=self._session_factory)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._request_stop)
        logger.info("cdc executor started receiver=127.0.0.1:%s max_sources=%s",
                    self._receiver.port, self._max_sources)
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self.tick()
                self._stop.wait(
                    max(0.0, self._interval - (time.monotonic() - started)))
        finally:
            self.shutdown()
        return 0

    def _request_stop(self, *_args) -> None:
        self._stop.set()

    def tick(self) -> None:
        db = self._session_factory()
        try:
            if self._lease is None:
                self._lease = SourceLease(db.get_bind())
            if not self._lease.healthy():
                # 租约连接断开：锁已随之释放，其他副本可能接管——先停全部子进程
                logger.error("cdc source leases lost; stopping all children")
                self._lease.reset()
                for item in list(self._runtimes.values()):
                    self._stop_runtime(item)
                self._runtimes.clear()
            try:
                self._control.reconcile(db)
                db.commit()
            except Exception as exc:
                db.rollback()
                self._log_failure("reconcile", exc)
            try:
                desired = {item.connection_id
                           for item in self._control.desired_sources(db)}
                cleanup_ids = {
                    getattr(row, "connection_id", row)
                    for row in self._control.cleanup_sources(db)}
                rows = self._load_sources(db)
            except Exception as exc:
                # 期望态读不出时不做任何停/清理决策：保持现有子进程运行
                db.rollback()
                self._log_failure("desired_state", exc)
                return
            for source in rows:
                try:
                    self._reconcile_source(
                        db, source,
                        desired=source.connection_id in desired,
                        needs_cleanup=source.connection_id in cleanup_ids)
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    self._log_failure("source", exc)
                    self._try_report_internal(
                        source.connection_id, source.generation)
            known = {row.connection_id for row in rows}
            for connection_id in [key for key in self._runtimes if key not in known]:
                # 源行已被控制面安全清理（停止 + 槽清理确认）：只收尾进程
                self._stop_runtime(self._runtimes.pop(connection_id))
        finally:
            db.close()
        # 只有完整走完一拍才刷新存活文件：元数据库不可达时健康检查如实失败
        try:
            self._heartbeat_file.touch()
        except OSError:
            pass

    # ── 单源收敛 ────────────────────────────────────────────────────

    def _reconcile_source(self, db, source, *, desired: bool,
                          needs_cleanup: bool) -> None:
        """收敛优先级：休眠清理 > 维持流式 > 暂停保持。

        停止未确认清理的源即使重新出现期望订阅也先走清理（新代际由控制面
        下一拍 reconcile 显式给出），否则会永远卡在“停止但未清理”。
        """
        connection_id = source.connection_id
        subscriptions = self._control.desired_subscriptions(db, connection_id)
        if not subscriptions or needs_cleanup:
            self._drain(db, source)
            return
        if not desired:
            # 暂停（凭据轮换等）或待换代：停子进程，保留槽与偏移
            self._hold(db, connection_id, subscriptions)
            return
        connection = self._load_connection(db, connection_id)
        if connection is None:
            self._report(db, connection_id, source.generation,
                         status=self._control.SOURCE_ERROR,
                         error_code=runtime.CODE_SOURCE_CONFIG_UNAVAILABLE)
            return
        self._maintain(db, source, connection, subscriptions)

    def _maintain(self, db, source, connection, subscriptions) -> None:
        control = self._control
        connection_id = source.connection_id
        generation = source.generation
        item = self._runtimes.get(connection_id)
        if item is not None and item.generation != generation:
            # 换代只应发生在停止 + 清理确认之后；防御性收尾：停旧子进程并
            # 尽力清理旧代自有槽（控制面换代守卫已确保常态下早已清理）。
            previous = item.generation
            self._stop_runtime(self._runtimes.pop(connection_id))
            self._drop_generation(connection, connection_id, previous)
            item = None
        if source.status == control.SOURCE_GAP:
            # 日志缺口对本代是终态：只有控制面的显式策略（休眠清理 → 新代际）
            # 可以恢复。监督者不自行重启子进程、不占并发名额，也不动槽与偏移。
            if item is not None:
                self._stop_runtime(self._runtimes.pop(connection_id))
            self._report(db, connection_id, generation,
                         status=control.SOURCE_GAP,
                         error_code=source.last_error_code)
            return
        if item is None:
            if not self._lease.acquire(connection_id):
                return  # 其他副本持有该源：不拉起、不回报
            if not self._free_indexes:
                self._lease.release(connection_id)
                self._report(db, connection_id, generation,
                             status=control.SOURCE_ERROR,
                             error_code=runtime.CODE_EXECUTOR_CAPACITY)
                return
            item = self._new_runtime(connection_id, generation)

        wall = self._wall()
        # 期望态指纹先算：订阅等待流证据期间不反复预检源端；配置类错误
        # （不支持的标识符等）以稳定错误码 + 退避回报，不落成每拍内部错误
        try:
            digest = self._connector.desired_digest(
                connection=connection, subscriptions=subscriptions,
                state_dir=str(item.state_dir), names=item.names,
                quarkus_port=QUARKUS_PORT_BASE + item.index)
        except connector.ConnectorError as exc:
            self._config_failure(db, source, item, exc.code)
            return

        alive = item.child is not None and item.child.poll() is None
        if alive and item.properties_digest != digest:
            # include 集/凭据变化：同代重启，偏移保留
            self._stop_child(item)
            item.error_code = None
            alive = False
        if not alive:
            self._collect_exit(item)
            if wall >= item.next_start_at:
                # 预检只在真正（重）启动子进程前执行，并受退避约束
                offsets = item.state_dir / "offsets.dat"
                coverage = (getattr(source, "last_position", None) is not None
                            or any(getattr(s, "activation_position", None)
                                   for s in subscriptions))
                try:
                    self._connector.preflight(connection, subscriptions)
                    self._connector.assert_log_continuity(
                        connection, item.names,
                        has_offsets=offsets.exists() and offsets.stat().st_size > 0,
                        coverage_exists=coverage)
                    self._start_child(item, connection, subscriptions, digest)
                    item.pending_error = None
                except connector.ConnectorError as exc:
                    self._config_failure(db, source, item, exc.code)
                    return
                except OSError as exc:
                    self._log_failure("spawn", exc)
                    item.error_code = runtime.CODE_EXECUTOR_INTERNAL
                    item.failures += 1
                    item.next_start_at = wall + self._backoff(item.failures)
        self._observe(db, source, item)
        self._maybe_anchor(db, connection, item, subscriptions)
        if item.child is not None and item.properties_digest == digest:
            # 子进程当前 include 集已不含被替换订阅：确认其停流
            self._confirm_stopped_subscriptions(
                db, connection_id, keep={s.id for s in subscriptions})

    def _maybe_anchor(self, db, connection, item: _Runtime,
                      subscriptions) -> None:
        """真实起流后才锚定覆盖前沿（不早于实际流证据）。

        新表先进入子进程 include 集；等该子进程被接收器确认过事件/心跳且
        健康探测显式 UP，再捕获当前前沿并激活 starting 订阅。锚定前的区间
        是显式的未覆盖期（接收器跳过 starting 订阅的事件），绝不回填。
        """
        starting = [s for s in subscriptions
                    if s.status == self._control.SUBSCRIPTION_STARTING]
        if not starting or item.child is None or item.child.poll() is not None:
            return
        accepted = self._receiver.last_accepted_at(
            item.connection_id, item.generation) or 0
        heartbeat = self._receiver.last_heartbeat_at(item.connection_id) or 0
        evidence = max(accepted, heartbeat)
        if evidence < item.started_wall or (
                self._wall() - evidence > self._evidence_window):
            return
        if self._health_probe(QUARKUS_PORT_BASE + item.index) is not True:
            return
        try:
            frontier = self._connector.capture_frontier(connection)
        except connector.ConnectorError as exc:
            self._log_failure("frontier", exc)
            return
        for subscription in starting:
            self._control.activate_subscription(
                db, subscription.id, position=frontier)
        db.commit()
        logger.info("cdc subscriptions anchored connection=%s generation=%s count=%s",
                    item.connection_id, item.generation, len(starting))

    def _config_failure(self, db, source, item: _Runtime, code: str) -> None:
        """预检/配置/续流校验失败：稳定错误码 + 退避重试。

        子进程不在时按错误码回报（日志缺口→gap、漂移→paused、其余 error）；
        子进程仍在（仅期望态指纹算不出）时保留现流并回报未收敛。模式漂移
        是连接级 fail-closed：整源暂停，下一拍停止该连接的全部流。
        """
        item.next_start_at = max(item.next_start_at, self._wall() + RETRY_SECONDS)
        if item.child is not None and item.child.poll() is None:
            item.pending_error = code
        else:
            self._collect_exit(item)
            item.error_code = code
        self._observe(db, source, item)

    def _drop_generation(self, connection, connection_id: str,
                         generation: int) -> None:
        names = self._connector.runtime_names(connection_id, generation)
        try:
            self._connector.drop_owned_pg_resources(
                connection, names["slot"], names["publication"])
        except connector.ConnectorError as exc:
            self._log_failure("generation_cleanup", exc)

    def _collect_exit(self, item: _Runtime) -> None:
        """子进程已退出：回收、记录分类码并安排退避重启。"""
        if item.child is None or item.child.poll() is None:
            return
        child, item.child = item.child, None
        child.reap()
        item.error_code = (runtime.merge_error_codes(item.error_code, child.error_code)
                           or runtime.CODE_EXECUTOR_CHILD_EXITED)
        item.failures += 1
        item.next_start_at = max(
            item.next_start_at, self._wall() + self._backoff(item.failures))
        shutil.rmtree(item.run_dir, ignore_errors=True)

    @staticmethod
    def _backoff(failures: int) -> float:
        return min(60.0, 5.0 * (2 ** min(max(failures, 1) - 1, 4)))

    def _status_for_code(self, code: str | None) -> str:
        control = self._control
        if code == runtime.CODE_SOURCE_LOG_GAP:
            return control.SOURCE_GAP
        if code == runtime.CODE_SCHEMA_DRIFT:
            return control.SOURCE_PAUSED
        return control.SOURCE_ERROR

    def _observe(self, db, source, item: _Runtime) -> None:
        """按状态所有权回报观测；绝不把已持久化的故障抹成 starting/NULL。

        - paused / gap：只由控制面显式策略解除（连接重测 / 休眠清理后开新
          代际），无条件保持，任何流证据都不得改写；
        - error：无论由接收器还是本监督者写入，都保持到出现“晚于该错误记录”
          的新接收证据且健康探测显式 UP 才恢复为 active。接收器对 error 源
          照常放行事件，所以监督者自身的临时错误不会被永久卡死；
        - 子进程已退出或期望态未收敛时，回报监督者自己的确定性错误码。
        """
        control = self._control
        wall = self._wall()
        alive = item.child is not None and item.child.poll() is None
        # 读最新源行：接收器可能在本拍内写入暂停或错误
        db.refresh(source)
        if source.status in (control.SOURCE_PAUSED, control.SOURCE_GAP):
            status, error_code = source.status, source.last_error_code
        elif not alive:
            error_code = item.error_code or runtime.CODE_EXECUTOR_CHILD_EXITED
            status = self._status_for_code(error_code)
        elif item.pending_error is not None:
            error_code = item.pending_error
            status = self._status_for_code(error_code)
        else:
            accepted = self._receiver.last_accepted_at(
                item.connection_id, item.generation) or 0
            heartbeat = self._receiver.last_heartbeat_at(item.connection_id) or 0
            evidence = max(accepted, heartbeat)
            fresh = (evidence >= item.started_wall
                     and wall - evidence <= self._evidence_window)
            if fresh:
                item.ever_evidence = True
            persisted_error = source.status == control.SOURCE_ERROR
            # 错误只能被严格晚于其记录时刻的接收证据清除：同一新鲜窗口内、
            # 错误落库之前的旧证据不算。源行每次写入（错误记录、成功回报）
            # 都会推进 updated_at，而成功接收的证据时刻在其提交之后。
            recovering = fresh and (
                not persisted_error
                or evidence > _epoch(getattr(source, "updated_at", None)))
            # 健康门必须显式 UP：探测失败/未知绝不冒充健康
            if recovering and self._health_probe(
                    QUARKUS_PORT_BASE + item.index) is True:
                status, error_code = control.SOURCE_ACTIVE, None
                item.failures = 0
            elif persisted_error:
                status, error_code = source.status, source.last_error_code
            elif (not item.ever_evidence
                  and wall - item.started_wall >= self._no_evidence_after):
                # 启动后从未见过任何流证据：按时限显式报错，可观测、不冒充
                status = control.SOURCE_ERROR
                error_code = runtime.CODE_EXECUTOR_NO_EVIDENCE
            else:
                status, error_code = control.SOURCE_STARTING, None
        control.report_source(
            db, item.connection_id, generation=item.generation, status=status,
            error_code=error_code)

    def _drain(self, db, source) -> None:
        """休眠清理：停子进程 → 确认订阅停流 → 清理自有 PG 资源 → 报 stopped。

        只有清理成功（非 PG 源无事可清亦算成功）才上报 stopped +
        cleanup_completed；失败保持 error + PG_CLEANUP_FAILED 并定期重试，
        防止控制面在旧槽仍占用 WAL 时换代。控制面在收到 stopped + 清理确认
        后才会开新覆盖代际。
        """
        control = self._control
        connection_id = source.connection_id
        if not self._lease.acquire(connection_id):
            return  # 其他副本持有该源，由其收尾
        try:
            item = self._runtimes.pop(connection_id, None)
            if item is not None:
                self._stop_runtime(item, release=False)  # 1) 先停流
            self._confirm_stopped_subscriptions(db, connection_id, keep=set())
            if (source.status == control.SOURCE_STOPPED
                    and source.cleanup_completed_at is not None):
                return
            if self._wall() < self._cleanup_retry_at.get(connection_id, 0.0):
                return
            connection = self._load_connection(db, connection_id)
            cleaned = False
            if connection is not None:
                names = self._connector.runtime_names(connection_id, source.generation)
                try:
                    cleaned = self._connector.drop_owned_pg_resources(
                        connection, names["slot"], names["publication"])
                except connector.ConnectorError:
                    cleaned = False
            if cleaned:
                self._cleanup_retry_at.pop(connection_id, None)
                self._report(db, connection_id, source.generation,
                             status=control.SOURCE_STOPPED, cleanup_completed=True)
            else:
                self._cleanup_retry_at[connection_id] = self._wall() + RETRY_SECONDS
                self._report(db, connection_id, source.generation,
                             status=control.SOURCE_ERROR,
                             error_code=runtime.CODE_PG_CLEANUP_FAILED)
        finally:
            self._lease.release(connection_id)

    def _hold(self, db, connection_id: str, subscriptions) -> None:
        """暂停/待换代：停子进程但保留槽、偏移与租约（同代恢复）。"""
        item = self._runtimes.get(connection_id)
        if item is None:
            return
        self._stop_child(item)
        item.error_code = None
        item.pending_error = None
        self._confirm_stopped_subscriptions(
            db, connection_id, keep={s.id for s in subscriptions})

    # ── 子进程与运行时管理 ──────────────────────────────────────────

    def _new_runtime(self, connection_id: str, generation: int) -> _Runtime:
        index = self._free_indexes.pop(0)
        state_dir = self._state_root / connection_id / f"g{generation}"
        state_dir.mkdir(parents=True, exist_ok=True)
        item = _Runtime(
            connection_id=connection_id, generation=generation, index=index,
            names=self._connector.runtime_names(connection_id, generation),
            state_dir=state_dir,
            run_dir=self._run_root / connection_id / f"g{generation}")
        self._runtimes[connection_id] = item
        return item

    def _start_child(self, item: _Runtime, connection, subscriptions,
                     digest: str) -> None:
        # 每次启动轮换密钥并重新注册：旧子进程的在途请求立即失效，接收器
        # 的流证据不会被旧进程推进
        item.secret = os.urandom(32)
        session = self._receiver.register(
            item.connection_id, item.generation, item.secret)
        item.sink_url = f"http://127.0.0.1:{self._receiver.port}{session.path}"
        properties = self._connector.build_properties(
            connection=connection, subscriptions=subscriptions,
            secret_b64=base64.b64encode(item.secret).decode("ascii"),
            sink_url=item.sink_url,
            quarkus_port=QUARKUS_PORT_BASE + item.index,
            state_dir=str(item.state_dir), names=item.names)
        config_dir = item.run_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        for directory in (item.run_dir.parent, item.run_dir, config_dir):
            os.chmod(directory, 0o700)
        config_file = config_dir / "application.properties"
        # 直接以 0600 创建：密钥文件不存在权限过宽的窗口
        descriptor = os.open(
            config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(properties)
        args = [JAVA_BINARY, *CHILD_JAVA_OPTS, "-cp", DEBEZIUM_CLASSPATH,
                DEBEZIUM_MAIN]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "JAVA_HOME": JAVA_HOME, "LANG": "C.UTF-8", "TZ": "UTC"}
        item.child = self._spawn(args, cwd=str(item.run_dir), env=env)
        item.properties_digest = digest
        item.error_code = None
        item.ever_evidence = False
        item.started_wall = self._wall()
        logger.info("cdc child started connection=%s generation=%s quarkus_port=%s",
                    item.connection_id, item.generation,
                    QUARKUS_PORT_BASE + item.index)

    def _stop_child(self, item: _Runtime) -> None:
        if item.child is not None:
            child, item.child = item.child, None
            child.stop()
        shutil.rmtree(item.run_dir, ignore_errors=True)

    def _stop_runtime(self, item: _Runtime, *, release: bool = True) -> None:
        self._stop_child(item)
        if self._receiver is not None and item.secret is not None:
            try:
                self._receiver.unregister(item.connection_id)
            except Exception as exc:
                self._log_failure("receiver.unregister", exc)
        self._free_indexes.append(item.index)
        if release and self._lease is not None:
            self._lease.release(item.connection_id)

    def shutdown(self) -> None:
        """SIGTERM/退出：并行终止全部子进程组并回收；绝不清理活跃源的槽。"""
        deadline = time.monotonic() + SHUTDOWN_BUDGET_SECONDS
        items = list(self._runtimes.values())
        self._runtimes.clear()
        for item in items:
            if item.child is not None:
                item.child.terminate()
        for item in items:
            if item.child is not None:
                if not item.child.wait(max(0.0, deadline - time.monotonic())):
                    item.child.kill_group()
                item.child.reap()
                item.child.kill_group()
                item.child = None
            shutil.rmtree(item.run_dir, ignore_errors=True)
        if self._receiver is not None:
            try:
                self._receiver.stop()
            except Exception as exc:
                self._log_failure("receiver.stop", exc)
        if self._lease is not None:
            self._lease.reset()

    # ── 回报/数据访问辅助 ───────────────────────────────────────────

    def _report(self, db, connection_id: str, generation: int, *, status: str,
                error_code: str | None = None,
                cleanup_completed: bool = False) -> None:
        self._control.report_source(
            db, connection_id, generation=generation, status=status,
            error_code=error_code, cleanup_completed=cleanup_completed)

    def _try_report_internal(self, connection_id: str, generation: int) -> None:
        db = self._session_factory()
        try:
            self._report(db, connection_id, generation,
                         status=self._control.SOURCE_ERROR,
                         error_code=runtime.CODE_EXECUTOR_INTERNAL)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _confirm_stopped_subscriptions(self, db, connection_id: str,
                                       keep: set[str]) -> None:
        """确认已不在子进程 include 集内的 superseded 订阅为 stopped。

        只确认 superseded 行；期望存续（starting/active）的订阅绝不触碰。
        """
        superseded = [row.id for row in self._superseded_subscriptions(db, connection_id)
                      if row.id not in keep]
        if not superseded and keep:
            return
        confirmed = self._control.confirm_subscriptions_stopped(
            db, connection_id, subscription_ids=superseded or None)
        if confirmed:
            logger.info("cdc subscriptions stopped connection=%s count=%s",
                        connection_id, confirmed)

    def _load_sources(self, db) -> list:
        return list(db.scalars(sa.select(IntegratedCdcSource)))

    def _load_connection(self, db, connection_id: str):
        return db.get(Connection, connection_id)

    def _superseded_subscriptions(self, db, connection_id: str) -> list:
        return list(db.scalars(sa.select(IntegratedCdcSubscription).where(
            IntegratedCdcSubscription.connection_id == connection_id,
            IntegratedCdcSubscription.status
            == self._control.SUBSCRIPTION_SUPERSEDED)))

    @staticmethod
    def _log_failure(stage: str, exc: BaseException) -> None:
        # 只记录阶段与异常类型：异常文本可能携带主机/账号等源信息
        logger.error("cdc executor stage=%s failed exc=%s",
                     stage, type(exc).__name__)


def _epoch(value) -> float:
    """源行时间戳 → epoch 秒；无时区值（SQLite）按 UTC 解释，缺失记 0。"""
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def probe_health(port: int) -> bool | None:
    """子进程 Quarkus 健康端点：UP=True，DOWN=False，未知/未就绪=None。

    UP 只是必要条件；active 还要求接收器侧的真实流证据。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/q/health", timeout=2) as response:
            payload = json.loads(response.read(65536) or b"{}")
    except urllib.error.HTTPError as exc:
        return False if exc.code == 503 else None
    except Exception:
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    if status == "UP":
        return True
    if status == "DOWN":
        return False
    return None


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("APP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # 与 Web main 相同的进程装配：注册全部表映射 + 组合根端口
    # （cdc_control.reconcile 依赖组合根注入的 cdc_ontology_port）
    from app.model_registry import import_all_models

    import_all_models()
    from app.bootstrap.runtime_composition import install_runtime_ports

    install_runtime_ports()
    raise SystemExit(CdcSupervisor().run())


if __name__ == "__main__":
    main()
