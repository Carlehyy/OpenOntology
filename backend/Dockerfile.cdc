# integrated CDC 执行器镜像：backend Python 运行时 + Debezium Server 发行版
# 及其 Java 21 运行时。发行版与 JVM 都取自同一个固定摘要的官方镜像，
# 不引入第二个外部信任源，JVM 版本即 Debezium 自身的运行版本。
#
# 同一镜像同时服务两个执行器（共用 Java 依赖，命令不同）：
#   - python -m app.data_channel.integrated.cdc_executor（Sentinel CDC）
#   - python -m app.data_channel.integrated.replica_executor（副本快照）
# 副本执行器的 TransportMetadata/ReadOffsets SMT 在 debezium 构建阶段用
# 该镜像自带的完整 JDK 工具链（javac/jar，实测存在于 jre-21 布局）离线
# 编译为 /opt/debezium/lib/replica-smt.jar；对 CDC 执行器只是 classpath
# 上一个未被引用的 jar，不改变其行为。
#
# 验收口径：镜像验收必须包含真实完整 JVM 启动（Debezium Server 起到
# 连接器配置阶段），`java -version` 与 Class.forName 探针都不足以证明
# JRE 自包含（canary 实证：断裂符号链接只在完整启动时暴露）。
#
# 常规生产构建（与 backend/Dockerfile 相同的 Python 依赖层）：
#   docker build -f backend/Dockerfile.cdc backend
# 复用已构建的 backend 运行时镜像、跳过 Python 依赖层（验收/金丝雀）：
#   docker build -f backend/Dockerfile.cdc \
#     --build-arg PYTHON_BASE_IMAGE=<已构建的 backend 镜像> \
#     --build-arg CDC_PREBUILT_BACKEND=1 backend
#
# 执行器不需要 Node 运行时与 docker CLI，故不拷入（最小化攻击面）。
ARG PYTHON_BASE_IMAGE=python:3.12-slim
ARG DEBEZIUM_SERVER_IMAGE=quay.io/debezium/server:3.7.1.Final@sha256:8c4e6c7efc392f9adfd283ede2e75cd1a3594ef1126b014248054a5e718bfbd5

FROM ${DEBEZIUM_SERVER_IMAGE} AS debezium
# Red Hat 系镜像的 JRE 携带指向 donor 文件系统的绝对符号链接（conf/、
# lib/security/、lib/tzdb.dat → /etc/java/...、/usr/share/javazi/...）。
# 直接 COPY 进本镜像后链接全部断裂，JVM 完整启动会在加载 java.security
# 时以 InternalError 退出。先在 donor 内整体解引用为自包含目录。
RUN cp -aL /usr/lib/jvm/jre-21 /tmp/portable-jre
# 副本 SMT：只用本镜像的 JDK 与 /debezium/lib 既有 jar 离线编译，构建
# 即自校验（SelfTest 随构建运行，失败则镜像构建失败）；产物只取 jar，
# 编译中间物不进入最终镜像。该阶段以镜像自带用户 jboss(185) 运行，
# 产物目录放 /tmp（唯一普遍可写根）。
COPY integrated_runtime/replica/ /src/replica-smt/
RUN sh /src/replica-smt/build.sh /debezium /tmp/replica-smt-artifacts \
 && java -cp "/tmp/replica-smt-artifacts/classes:/tmp/replica-smt-artifacts/test-classes:/debezium/lib/*" \
      io.openontology.replica.SelfTest

FROM ${PYTHON_BASE_IMAGE}
ARG CDC_PREBUILT_BACKEND=

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /bin/
# /debezium 的布局已对该摘要镜像实测确认：runner jar、config/lib、
# lib/*（含 debezium-server-http）。JRE 用上方解引用后的自包含目录。
COPY --from=debezium /tmp/portable-jre /opt/java/openjdk
COPY --from=debezium /debezium /opt/debezium
# 副本执行器的 SMT（TransportMetadata + ReadOffsets）：放行 lib/* 使
# DEBEZIUM_CLASSPATH（lib/* 通配）无需变更即可加载 io.openontology.replica.*。
COPY --from=debezium /tmp/replica-smt-artifacts/replica-smt.jar /opt/debezium/lib/replica-smt.jar

WORKDIR /app
ENV JAVA_HOME=/opt/java/openjdk \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/java/openjdk/bin:/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN if [ -z "$CDC_PREBUILT_BACKEND" ]; then \
      uv sync --frozen --no-dev --no-install-project \
      && uv pip install --python /app/.venv/bin/python --no-deps py-openjudge==0.2.1; \
    fi
COPY . .

# 非 root 运行；状态卷目录预置属主（空命名卷首次挂载会继承镜像内属主）。
# 两个执行器各用独立状态根：CDC /var/lib/openontology-cdc、副本执行器
# /var/lib/openontology-replicas，互不共享偏移与模式历史。
RUN useradd --uid 10001 --user-group --no-create-home \
      --shell /usr/sbin/nologin cdc \
 && mkdir -p /var/lib/openontology-cdc /var/lib/openontology-replicas \
 && chown -R cdc:cdc /var/lib/openontology-cdc /var/lib/openontology-replicas
USER 10001:10001
VOLUME /var/lib/openontology-cdc

# 存活＝最近一拍对账完成（元数据库不可达时不刷新，健康检查如实失败）。
# 镜像默认健康检查是 CDC 执行器口径；副本执行器服务在 Compose 里以
# REPLICA_HEARTBEAT_FILE（/tmp/integrated_replica_executor.heartbeat）
# 同口径覆盖。
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,time,sys; sys.exit(0 if time.time()-os.path.getmtime('/tmp/integrated_cdc_executor.heartbeat')<30 else 1)"]
