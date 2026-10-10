# 任务实例 agent 执行容器（设计方案 §6.1-6.2）
# - node22-slim + Claude Agent SDK runner；构建进 openontology-task-agent 镜像
# - 只读 rootfs（--read-only 运行），/workspace 为宿主 bind mount 唯一可写根
# - 非 root 用户运行；模型凭据经容器环境变量注入，不进镜像不落盘
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agent-runner
COPY package.json package-lock.json* ./
RUN npm install --omit=dev
COPY runner.mjs ./

RUN useradd --create-home --uid 1000 agent
USER agent
WORKDIR /workspace

ENTRYPOINT ["node", "/opt/agent-runner/runner.mjs"]
