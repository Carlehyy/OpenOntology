ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /bin/
# stdio MCP（npx 形态）需要 Node 运行时：从官方 Node 镜像拷入 node/npm/npx，
# 不更换 Python 基础镜像；是否放行由 SUPER_ASSISTANT_MCP_STDIO_ENABLED 与
# SUPER_ASSISTANT_MCP_STDIO_ALLOWED_COMMANDS 在运行时控制（默认关闭）。
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# 任务实例 task_worker 以 docker CLI 子进程管理 agent 执行容器（挂载宿主
# docker.sock，仅客户端；daemon 永不在此镜像内运行）。用官方 cli 镜像的
# 静态二进制拷贝——避免 apt 网络往返（低配服务器上曾把部署推超 25 分钟
# SSH 预算），层也更小。
COPY --from=docker:29.3.1-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# 助手评估（OpenJudge）：py-openjudge 声明的全量依赖（pandas<3 / openai>=2.8 /
# nltk / sklearn / dashscope 等）与平台既有锁定版本冲突，但其评分器子集在运行时
# 只需要 loguru/pydantic/tqdm/openai 等轻量包。因此以 --no-deps 安装本体，
# 轻量依赖显式进入 pyproject；版本精确锁定保证可复现。
RUN uv pip install --python /app/.venv/bin/python --no-deps py-openjudge==0.2.1
COPY . .
