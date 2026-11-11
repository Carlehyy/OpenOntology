# integrated CDC 执行器镜像：backend Python 运行时 + Debezium Server 发行版
# 及其 Java 21 运行时。发行版与 JVM 都取自同一个固定摘要的官方镜像，
# 不引入第二个外部信任源，JVM 版本即 Debezium 自身的运行版本。
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

FROM ${PYTHON_BASE_IMAGE}
ARG CDC_PREBUILT_BACKEND=

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /bin/
# /debezium 的布局已对该摘要镜像实测确认：runner jar、config/lib、
# lib/*（含 debezium-server-http）。JRE 用上方解引用后的自包含目录。
COPY --from=debezium /tmp/portable-jre /opt/java/openjdk
COPY --from=debezium /debezium /opt/debezium

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
RUN useradd --uid 10001 --user-group --no-create-home \
      --shell /usr/sbin/nologin cdc \
 && mkdir -p /var/lib/openontology-cdc \
 && chown -R cdc:cdc /var/lib/openontology-cdc
USER 10001:10001
VOLUME /var/lib/openontology-cdc

# 存活＝最近一拍对账完成（元数据库不可达时不刷新，健康检查如实失败）。
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,time,sys; sys.exit(0 if time.time()-os.path.getmtime('/tmp/integrated_cdc_executor.heartbeat')<30 else 1)"]
