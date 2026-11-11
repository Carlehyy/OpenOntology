(() => {
  "use strict";

  const SERVICES = [
    "postgres",
    "redis",
    "nats",
    "neo4j",
    "minio",
    "browser",
    "n8n",
    "weknora",
    "kaneo",
    "hindsight",
  ];

  const SERVICE_LABELS = {
    postgres: "PostgreSQL",
    redis: "Redis",
    nats: "NATS",
    neo4j: "Neo4j",
    minio: "MinIO",
    browser: "Chromium CDP",
    n8n: "n8n",
    weknora: "WeKnora",
    kaneo: "Kaneo",
    hindsight: "Hindsight",
  };

  const state = {
    accessToken: "",
    csrfToken: "",
    hasConfig: false,
    secretsPresent: {},
    guides: {},
    commands: {},
    requirements: [],
    requiredServices: [
      "postgres",
      "redis",
      "nats",
      "neo4j",
      "minio",
      "n8n",
      "weknora",
      "kaneo",
    ],
    tests: Object.fromEntries(SERVICES.map((service) => [service, "idle"])),
    fingerprints: Object.fromEntries(SERVICES.map((service) => [service, ""])),
    generated: false,
    busyAll: false,
  };

  const elements = {};

  const fallbackGuides = {
    postgres: {
      title: "找到 PostgreSQL 账号和密码",
      intro:
        "如果 PostgreSQL 由 Docker Compose 启动，账号、密码和数据库名称通常写在 Compose 配置或本地环境文件中。",
      warning: "以下命令可能在屏幕上显示密码。不要分享终端截图，也不要把输出粘贴到聊天中。",
      platforms: {
        Windows: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | findstr /I "POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB"',
          },
          {
            text: "如果是手工安装，请在 Windows 服务中确认 PostgreSQL 正在运行，再向安装人员询问初始化密码。",
          },
        ],
        Ubuntu: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | grep -E "POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB"',
          },
          {
            text: "只能登录但不知道原密码时，建议创建一个仅供 OpenOntology 使用的新账号，不要直接修改生产账号密码。",
          },
        ],
        macOS: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | grep -E "POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB"',
          },
          {
            text: "使用 Homebrew 安装时，可先执行 brew services list 确认服务状态。",
          },
        ],
      },
    },
    redis: {
      title: "确认 Redis 密码和连接状态",
      intro:
        "Redis 新安装时可能没有密码，但完整功能配置要求设置密码。Docker Compose 中通常使用 requirepass 或 REDIS_PASSWORD。",
      warning: "修改 Redis 密码后，要让 Redis、后端和后台任务 executor 使用同一个值。",
      platforms: {
        Windows: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | findstr /I "REDIS_PASSWORD requirepass"',
          },
          {
            command:
              "docker exec -it <redis-container> redis-cli -a <password> PING",
          },
        ],
        Ubuntu: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | grep -E "REDIS_PASSWORD|requirepass"',
          },
          {
            command:
              "docker exec -it <redis-container> redis-cli -a '<password>' PING",
          },
        ],
        macOS: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | grep -E "REDIS_PASSWORD|requirepass"',
          },
          {
            text: "使用 Homebrew 安装时，可以在 redis.conf 中查找 requirepass。",
          },
        ],
      },
    },
    nats: {
      title: "启动带 JetStream 的 NATS",
      intro:
        "NATS 是流水线任务派发通道，必须以 -js 启用 JetStream。本机 Docker 启动的 NATS 默认无认证，连接令牌可留空。",
      warning: "未启用 JetStream 时连接测试会明确报错；启用令牌认证时，本页填写的令牌必须与服务端一致。",
      platforms: {
        Windows: [
          {
            command:
              "docker run -d --name nats -p 4222:4222 -v nats_data:/data nats:alpine -js --store_dir /data -m 8222",
          },
          {
            text: "已有容器但未加 -js 时，请先删除旧容器再用上面的命令重建。",
          },
        ],
        Ubuntu: [
          {
            command:
              "docker run -d --name nats -p 4222:4222 -v nats_data:/data nats:alpine -js --store_dir /data -m 8222",
          },
          {
            command: "docker logs nats",
          },
        ],
        macOS: [
          {
            command:
              "docker run -d --name nats -p 4222:4222 -v nats_data:/data nats:alpine -js --store_dir /data -m 8222",
          },
          {
            text: "使用 Homebrew 安装时，请在启动参数中加入 -js 或 --jetstream。",
          },
        ],
      },
    },
    neo4j: {
      title: "找到或重置 Neo4j 登录密码",
      intro:
        "Neo4j 默认账号通常是 neo4j。首次登录管理页面时会要求把初始密码改成新密码。",
      warning: "Neo4j 不能读取已经保存的明文密码。不确定时应按官方流程重置，不要反复猜测导致账号锁定。",
      platforms: {
        Windows: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | findstr /I "NEO4J_AUTH NEO4J_URI"',
          },
          {
            text: "打开 http://127.0.0.1:7474 尝试登录。Bolt 连接地址通常是 bolt://127.0.0.1:7687。",
          },
        ],
        Ubuntu: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | grep -E "NEO4J_AUTH|NEO4J_URI"',
          },
          {
            text: "需要重置时，请先停止服务并按当前 Neo4j 版本的密码重置文档操作。",
          },
        ],
        macOS: [
          { command: "docker compose ps" },
          {
            text: "Neo4j Desktop 用户可以在项目连接详情中查看 Bolt 地址，但密码仍需由创建者提供。",
          },
          {
            text: "管理页面通常是 http://127.0.0.1:7474。",
          },
        ],
      },
    },
    minio: {
      title: "找到 MinIO 访问账号和密码",
      intro:
        "MinIO 使用 Access Key 和 Secret Key 登录。Docker Compose 中常见名称是 MINIO_ROOT_USER 和 MINIO_ROOT_PASSWORD。",
      warning: "Access Key 和 Secret Key 都属于敏感信息。不要通过截图、邮件或聊天发送。",
      platforms: {
        Windows: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | findstr /I "MINIO_ROOT_USER MINIO_ROOT_PASSWORD"',
          },
          {
            text: "管理页面通常是 http://127.0.0.1:9001，程序连接地址通常是 127.0.0.1:9000。",
          },
        ],
        Ubuntu: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | grep -E "MINIO_ROOT_USER|MINIO_ROOT_PASSWORD"',
          },
          {
            text: "如果使用 systemd，请检查服务启动时引用的 EnvironmentFile，但不要把文件内容复制到公共位置。",
          },
        ],
        macOS: [
          { command: "docker compose ps" },
          {
            command:
              'docker compose config | grep -E "MINIO_ROOT_USER|MINIO_ROOT_PASSWORD"',
          },
          {
            text: "使用 Homebrew 启动时，请检查你为 MinIO 设置的环境变量。",
          },
        ],
      },
    },
    browser: {
      title: "用独立资料目录启动 Chromium CDP",
      intro:
        "CDP 是浏览器远程控制接口。请关闭占用同一资料目录的浏览器，并为 OpenOntology 使用独立目录。",
      warning: "CDP 端口必须只监听本机或可信网络。不要把 9222 端口直接开放到公网。",
      platforms: {
        Windows: [
          {
            command:
              '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=.\\runtime\\chromium-profile',
          },
          {
            text: "如果 Chrome 安装在其他位置，请只替换最前面的程序路径，资料目录继续使用项目相对路径。",
          },
        ],
        Ubuntu: [
          {
            command:
              "google-chrome --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=./runtime/chromium-profile",
          },
          {
            text: "使用 Chromium 时，把命令开头的 google-chrome 改为 chromium 或 chromium-browser。",
          },
        ],
        macOS: [
          {
            command:
              '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=./runtime/chromium-profile',
          },
          {
            text: "命令会打开一个独立浏览器窗口。不要用它登录不必要的个人账号。",
          },
        ],
      },
    },
    n8n: {
      title: "在 n8n 中创建 API Key",
      intro:
        "API Key 不是登录密码。请登录 n8n 管理页面，在设置中创建一个供 OpenOntology 使用的 API Key。",
      warning: "API Key 可以读取或修改工作流。只保存在本机配置中，泄露后请立即在 n8n 中撤销。",
      platforms: {
        Windows: [
          {
            text: "打开 n8n 页面，进入 Settings，再进入 n8n API，选择 Create an API Key。",
          },
          {
            command: "docker compose ps",
          },
          {
            text: "本机默认地址通常是 http://127.0.0.1:5678。",
          },
        ],
        Ubuntu: [
          {
            text: "打开 n8n 页面，进入 Settings，再进入 n8n API，选择 Create an API Key。",
          },
          {
            command: "docker compose ps",
          },
          {
            text: "如果 n8n 在远程服务器，请填写浏览器实际能访问的 HTTPS 地址。",
          },
        ],
        macOS: [
          {
            text: "打开 n8n 页面，进入 Settings，再进入 n8n API，选择 Create an API Key。",
          },
          {
            command: "docker compose ps",
          },
          {
            text: "复制后立即保存到此页面，n8n 可能不会再次显示完整 Key。",
          },
        ],
      },
    },
    security: {
      title: "安全保管本地密码和密钥",
      intro:
        "本配置中心生成的文件包含第三方密码，只应保存在本机。项目已经把生成目录排除在 Git 跟踪之外。",
      warning: "不要提交生成的配置文件，不要把文件内容复制到 Issue、聊天或终端共享记录中。",
      platforms: {
        Windows: [
          {
            text: "请使用当前 Windows 账号保护项目目录，不要把项目放在公共共享文件夹。",
          },
          {
            command: "git status --short",
          },
          {
            text: "生成后可以执行上面的命令，确认本地配置文件没有出现在待提交列表。",
          },
        ],
        Ubuntu: [
          {
            text: "项目目录应只允许当前用户访问。多人服务器上不要使用公共账号运行。",
          },
          {
            command: "git status --short",
          },
          {
            text: "备份时请使用加密磁盘或可信密码管理工具保存恢复信息。",
          },
        ],
        macOS: [
          {
            text: "建议开启 FileVault，并避免把含密码的项目目录同步到公共网盘。",
          },
          {
            command: "git status --short",
          },
          {
            text: "密钥保持稳定。需要轮换时，应先确认所有已加密配置都有迁移方案。",
          },
        ],
      },
    },
  };

  function cacheElements() {
    elements.loading = document.querySelector("#bootstrap-loading");
    elements.appShell = document.querySelector("#app-shell");
    elements.form = document.querySelector("#config-form");
    elements.saveState = document.querySelector("#save-state");
    elements.existingNotice = document.querySelector("#existing-config-notice");
    elements.globalError = document.querySelector("#global-error");
    elements.progress = document.querySelector("#test-progress");
    elements.progressCount = document.querySelector("#progress-count");
    elements.requirements = document.querySelector("#requirements-list");
    elements.testAll = document.querySelector("#test-all");
    elements.generate = document.querySelector("#generate-config");
    elements.generateHelp = document.querySelector("#generate-help");
    elements.generatedResult = document.querySelector("#generated-result");
    elements.generatedPath = document.querySelector("#generated-path");
    elements.commandList = document.querySelector("#command-list");
    elements.runtimeCheck = document.querySelector("#runtime-check");
    elements.runtimeResult = document.querySelector("#runtime-result");
    elements.stickyActions = document.querySelector("#sticky-actions");
    elements.stickyTitle = document.querySelector("#sticky-title");
    elements.stickyCopy = document.querySelector("#sticky-copy");
    elements.stickyTestAll = document.querySelector("#sticky-test-all");
    elements.stickyGenerate = document.querySelector("#sticky-generate");
    elements.guideDialog = document.querySelector("#guide-dialog");
    elements.guideTitle = document.querySelector("#guide-title");
    elements.guideContent = document.querySelector("#guide-content");
    elements.closeGuide = document.querySelector("#close-guide");
    elements.secretsDialog = document.querySelector("#confirm-secrets-dialog");
    elements.generateSecrets = document.querySelector("#generate-secrets");
    elements.confirmSecrets = document.querySelector("#confirm-secret-generation");
    elements.cancelSecrets = document.querySelector("#cancel-secret-generation");
    elements.toastRegion = document.querySelector("#toast-region");
  }

  async function bootstrap() {
    cacheElements();
    bindEvents();

    try {
      state.accessToken = accessTokenFromFragment();
      if (!state.accessToken) {
        throw new Error(
          "页面缺少本地访问令牌。请关闭此页面，并从配置中心启动窗口重新打开完整地址。",
        );
      }
      const payload = await apiRequest("/api/bootstrap", { method: "GET" });
      state.csrfToken = String(payload.csrf_token || payload.csrfToken || "");
      state.hasConfig = Boolean(payload.has_config ?? payload.hasConfig);
      state.secretsPresent =
        payload.secrets_present || payload.secretsPresent || {};
      state.guides = payload.guides || {};
      state.commands = payload.commands || {};
      state.requirements = Array.isArray(payload.requirements)
        ? payload.requirements
        : [];
      state.requiredServices = normalizeServiceList(
        payload.required_services,
        state.requiredServices,
      );
      populateProfile(payload.profile || payload.config || {});
      applySecretRetention();

      if (!state.hasConfig && !payload.defaults_loaded) {
        generateSecretValues();
      }

      elements.existingNotice.hidden = !state.hasConfig && !payload.defaults_loaded;
      if (payload.defaults_loaded) {
        const title = elements.existingNotice.querySelector("strong");
        const copy = elements.existingNotice.querySelector("span");
        if (title) {
          title.textContent = "已加载本机默认值";
        }
        if (copy) {
          copy.textContent =
            "固定参数已预填；已保存的密码仍保持遮蔽，生成后会写入正式本地配置。";
        }
      }
      if (payload.config_warning) {
        elements.globalError.className = "notice notice-warning";
        elements.globalError.textContent = String(payload.config_warning);
        elements.globalError.hidden = false;
      }
      elements.saveState.textContent = state.hasConfig
        ? "已加载本地配置"
        : payload.defaults_loaded
          ? "已加载本机默认值"
          : payload.config_warning
            ? "现有配置需要修复"
            : "尚未生成配置";
      elements.loading.hidden = true;
      elements.appShell.hidden = false;
      elements.stickyActions.hidden = false;

      updateLocalSectionStates();
      updateSummary();
      setupSectionObserver();
    } catch (error) {
      showBootstrapError(error);
    }
  }

  function accessTokenFromFragment() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const token = params.get("access_token") || "";
    return /^[A-Za-z0-9_-]{32,256}$/.test(token) ? token : "";
  }

  function normalizeServiceList(candidate, fallback) {
    if (!Array.isArray(candidate)) {
      return [...fallback];
    }
    const normalized = candidate.filter(
      (service, index) =>
        SERVICES.includes(service) && candidate.indexOf(service) === index,
    );
    return normalized.length ? normalized : [...fallback];
  }

  function bindEvents() {
    document.querySelectorAll('a[href^="#"]').forEach((link) => {
      link.addEventListener("click", (event) => {
        const target = document.querySelector(link.getAttribute("href"));
        if (!target) {
          return;
        }
        // The fragment carries the ephemeral access token. Native anchor
        // navigation would replace it and make a later refresh unrecoverable.
        event.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        if (link.classList.contains("skip-link")) {
          target.focus({ preventScroll: true });
        }
      });
    });

    document.querySelectorAll("[data-test-service]").forEach((button) => {
      button.addEventListener("click", () => testService(button.dataset.testService));
    });

    document.querySelectorAll("[data-guide]").forEach((button) => {
      button.addEventListener("click", () => showGuide(button.dataset.guide));
    });

    document.querySelectorAll("[data-toggle-secret]").forEach((button) => {
      button.addEventListener("click", () => toggleSecret(button));
    });

    elements.form.addEventListener("input", handleFieldChange);
    elements.form.addEventListener("change", handleFieldChange);
    elements.form.addEventListener("submit", (event) => event.preventDefault());

    elements.testAll.addEventListener("click", testAllServices);
    elements.stickyTestAll.addEventListener("click", testAllServices);
    elements.generate.addEventListener("click", generateConfig);
    elements.stickyGenerate.addEventListener("click", generateConfig);
    elements.runtimeCheck.addEventListener("click", runtimeCheck);

    elements.closeGuide.addEventListener("click", () => elements.guideDialog.close());
    elements.guideDialog.addEventListener("click", closeDialogFromBackdrop);
    elements.secretsDialog.addEventListener("click", closeDialogFromBackdrop);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") {
        return;
      }
      if (elements.guideDialog.open) {
        event.preventDefault();
        elements.guideDialog.close();
      } else if (elements.secretsDialog.open) {
        event.preventDefault();
        elements.secretsDialog.close();
      }
    });

    elements.generateSecrets.addEventListener("click", () => {
      if (state.hasConfig || hasRetainedCoreSecrets()) {
        elements.secretsDialog.showModal();
      } else {
        generateSecretValues();
        showToast("已生成新的随机密码与密钥。", "success");
      }
    });

    elements.cancelSecrets.addEventListener("click", () => {
      elements.secretsDialog.close();
    });

    elements.confirmSecrets.addEventListener("click", () => {
      generateSecretValues();
      elements.secretsDialog.close();
      showToast("已生成新值。生成配置前请确认你确实需要更换密钥。", "success");
    });

  }

  function closeDialogFromBackdrop(event) {
    if (event.target === event.currentTarget) {
      event.currentTarget.close();
    }
  }

  function handleFieldChange(event) {
    const control = event.target.closest("[data-path]");
    if (!control) {
      return;
    }

    clearFieldError(control.dataset.path);
    const section = control.closest("[data-section]")?.dataset.section;
    if (section && SERVICES.includes(section)) {
      const fingerprint = fingerprintService(section);
      if (
        state.tests[section] !== "idle" &&
        fingerprint !== state.fingerprints[section]
      ) {
        setTestStatus(section, "idle", "配置已修改，请重新测试。");
        state.fingerprints[section] = "";
      }
    }

    if (state.generated) {
      state.generated = false;
      setSectionStatus("finish", "idle", "配置已修改");
      elements.saveState.textContent = "有未生成的修改";
    }

    updateLocalSectionStates();
    updateSummary();
  }

  function populateProfile(profile) {
    document.querySelectorAll("[data-path]").forEach((control) => {
      const value = getNestedValue(profile, control.dataset.path);
      if (value === undefined || value === null) {
        return;
      }

      if (control.dataset.secret && isMaskedSecret(value)) {
        return;
      }

      if (control.type === "checkbox") {
        control.checked = Boolean(value);
      } else if (
        control.tagName === "SELECT" &&
        !Array.from(control.options).some((option) => option.value === String(value))
      ) {
        return;
      } else {
        control.value = String(value);
      }
    });
  }

  function applySecretRetention() {
    document.querySelectorAll("[data-secret]").forEach((control) => {
      const path = control.dataset.secret;
      if (!secretIsPresent(path)) {
        return;
      }

      control.value = "";
      control.dataset.retained = "true";
      control.required = false;
      control.placeholder = "已安全保存，留空继续使用";

      const field = control.closest(".field");
      const helper = field?.querySelector(".secret-help");
      if (helper) {
        helper.dataset.originalText ||= helper.textContent;
        helper.textContent = "已安全保存。留空会继续使用原值，填写新值才会替换。";
        helper.classList.add("retained-secret");
      }
    });
  }

  function secretIsPresent(path) {
    const direct = state.secretsPresent[path];
    if (direct !== undefined) {
      return Boolean(direct);
    }

    const nested = getNestedValue(state.secretsPresent, path);
    if (nested !== undefined) {
      return Boolean(nested);
    }

    if (Array.isArray(state.secretsPresent)) {
      return state.secretsPresent.includes(path);
    }

    return false;
  }

  function isMaskedSecret(value) {
    const text = String(value);
    return (
      text === "__KEEP_EXISTING__" ||
      text === "__REDACTED__" ||
      /^\*{4,}$/.test(text) ||
      /^•{4,}$/.test(text)
    );
  }

  function collectProfile() {
    const profile = {};
    document.querySelectorAll("[data-path]").forEach((control) => {
      let value;
      if (control.type === "checkbox") {
        value = control.checked;
      } else if (control.type === "number") {
        const text = control.value.trim();
        value = text === "" ? null : Number(text);
      } else {
        value = control.value.trim();
      }
      setNestedValue(profile, control.dataset.path, value);
    });
    return profile;
  }

  function getNestedValue(object, path) {
    return path
      .split(".")
      .reduce(
        (current, segment) =>
          current && Object.prototype.hasOwnProperty.call(current, segment)
            ? current[segment]
            : undefined,
        object,
      );
  }

  function setNestedValue(object, path, value) {
    const segments = path.split(".");
    let current = object;
    segments.forEach((segment, index) => {
      if (index === segments.length - 1) {
        current[segment] = value;
      } else {
        current[segment] ||= {};
        current = current[segment];
      }
    });
  }

  function validateSection(section) {
    const container = document.querySelector(`[data-section="${section}"]`);
    if (!container) {
      return true;
    }
    return validateControls(Array.from(container.querySelectorAll("[data-path]")));
  }

  function validateAll() {
    const controls = Array.from(document.querySelectorAll("[data-path]"));
    const validControls = validateControls(controls);
    const validPorts = validatePortConflicts();
    return validControls && validPorts;
  }

  function validateControls(controls) {
    let valid = true;
    let firstInvalid = null;

    controls.forEach((control) => {
      clearFieldError(control.dataset.path);

      if (control.dataset.retained === "true" && control.value.trim() === "") {
        control.setCustomValidity("");
        control.removeAttribute("aria-invalid");
        return;
      }

      control.setCustomValidity("");
      const customError = customControlError(control);
      if (customError) {
        control.setCustomValidity(customError);
      }

      if (!control.checkValidity()) {
        valid = false;
        firstInvalid ||= control;
        const message = friendlyValidationMessage(control);
        showFieldError(control.dataset.path, message);
        control.setAttribute("aria-invalid", "true");
      } else {
        control.removeAttribute("aria-invalid");
      }
    });

    if (!valid && firstInvalid) {
      firstInvalid.focus({ preventScroll: true });
      firstInvalid.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    return valid;
  }

  function customControlError(control) {
    if (
      ["platform.backend_host", "platform.frontend_host"].includes(
        control.dataset.path,
      )
    ) {
      const host = control.value.trim().toLowerCase();
      if (!["127.0.0.1", "::1", "localhost"].includes(host)) {
        return "为了保护本机服务，请使用 127.0.0.1、::1 或 localhost。";
      }
    }

    const relativePaths = new Set([
      "advanced.uploads_dir",
      "advanced.storage_local_dir",
      "advanced.api_hub_data_dir",
      "advanced.super_assistant_skill_root",
      "advanced.steward_workspace_root",
    ]);

    if (!relativePaths.has(control.dataset.path)) {
      return "";
    }

    const value = control.value.trim();
    const isAbsolute =
      /^(?:[a-zA-Z]:[\\/]|\/|\\\\)/.test(value) ||
      value.startsWith("~");
    return isAbsolute ? "请使用项目相对路径，不要填写绝对路径。" : "";
  }

  function validatePortConflicts() {
    const fields = [
      ["platform.backend_port", "后端端口", "platform.backend_host"],
      ["platform.frontend_port", "前端端口", "platform.frontend_host"],
      ["postgres.port", "PostgreSQL 端口", "postgres.host"],
      ["redis.port", "Redis 端口", "redis.host"],
      ["nats.port", "NATS 端口", "nats.host"],
    ];
    const used = new Map();
    let valid = true;

    fields.forEach(([path, label, hostPath]) => {
      const control = document.querySelector(`[data-path="${path}"]`);
      const hostControl = document.querySelector(`[data-path="${hostPath}"]`);
      if (!control || !control.value || !hostControl) {
        return;
      }
      const port = control.value;
      const host = normalizeHostForComparison(hostControl.value);
      const address = `${host}:${port}`;
      if (used.has(address)) {
        const previous = used.get(address);
        const message = `${label}与${previous.label}重复，请为本机服务使用不同端口。`;
        showFieldError(path, message);
        showFieldError(previous.path, message);
        control.setAttribute("aria-invalid", "true");
        document
          .querySelector(`[data-path="${previous.path}"]`)
          ?.setAttribute("aria-invalid", "true");
        valid = false;
      } else {
        used.set(address, { path, label });
      }
    });

    return valid;
  }

  function normalizeHostForComparison(host) {
    const normalized = String(host).trim().toLowerCase().replace(/^\[|\]$/g, "");
    return ["localhost", "127.0.0.1", "::1", "0.0.0.0", "::"].includes(
      normalized,
    )
      ? "local"
      : normalized;
  }

  function friendlyValidationMessage(control) {
    if (control.validity.valueMissing) {
      return "这一项不能为空。";
    }
    if (control.validity.typeMismatch) {
      return "格式不正确，请检查地址是否完整。";
    }
    if (control.validity.rangeUnderflow || control.validity.rangeOverflow) {
      return `请输入 ${control.min || "允许范围"} 到 ${control.max || "允许范围"} 之间的数值。`;
    }
    if (control.validity.tooShort) {
      return `至少需要 ${control.minLength} 个字符。`;
    }
    if (control.validity.patternMismatch) {
      return "格式不符合要求，请参考输入框下方的说明。";
    }
    return control.validationMessage || "请检查这一项。";
  }

  function showFieldError(path, message) {
    const output = document.querySelector(`[data-error-for="${path}"]`);
    if (output) {
      output.textContent = message;
    }
  }

  function clearFieldError(path) {
    const output = document.querySelector(`[data-error-for="${path}"]`);
    const control = document.querySelector(`[data-path="${path}"]`);
    if (output) {
      output.textContent = "";
    }
    if (control) {
      control.setCustomValidity("");
      control.removeAttribute("aria-invalid");
    }
  }

  async function testService(service, options = {}) {
    if (!SERVICES.includes(service) || state.tests[service] === "loading") {
      return false;
    }

    if (!validateSection(service)) {
      setTestStatus(service, "error", "请先修正本节中标出的内容。");
      if (!options.quiet) {
        showToast(`${SERVICE_LABELS[service]} 的配置还不完整。`, "error");
      }
      return false;
    }

    setTestStatus(service, "loading", "正在测试连接，请稍候。");
    setServiceButtonsBusy(service, true);

    try {
      const profile = collectProfile();
      const payload = await apiRequest(`/api/test/${service}`, {
        method: "POST",
        body: profile,
      });

      if (!responseIsSuccessful(payload)) {
        throw new Error(responseMessage(payload, "连接测试未通过。"));
      }

      state.fingerprints[service] = fingerprintService(service);
      const latency = extractLatency(payload);
      const message =
        responseMessage(payload, "") ||
        (latency
          ? `连接成功，响应时间 ${latency} 毫秒。`
          : "连接成功，所需权限可用。");
      setTestStatus(service, "success", message);
      if (!options.quiet) {
        showToast(`${SERVICE_LABELS[service]} 连接成功。`, "success");
      }
      return true;
    } catch (error) {
      setTestStatus(service, "error", safeErrorMessage(error));
      if (!options.quiet) {
        showToast(`${SERVICE_LABELS[service]} 连接失败，请查看本节提示。`, "error");
      }
      return false;
    } finally {
      setServiceButtonsBusy(service, false);
      updateSummary();
    }
  }

  async function testAllServices() {
    if (state.busyAll) {
      return;
    }

    if (!validateAll()) {
      showToast("还有内容需要修正，请检查红色提示。", "error");
      return;
    }

    state.busyAll = true;
    setButtonBusy(elements.testAll, true, "正在测试必选项");
    setButtonBusy(elements.stickyTestAll, true, "正在测试");

    const results = await Promise.all(
      state.requiredServices.map((service) =>
        testService(service, { quiet: true }),
      ),
    );

    state.busyAll = false;
    setButtonBusy(elements.testAll, false);
    setButtonBusy(elements.stickyTestAll, false);

    const passed = results.filter(Boolean).length;
    if (passed === state.requiredServices.length) {
      showToast("全部必选依赖连接成功，现在可以生成配置。", "success");
    } else {
      showToast(
        `${passed} 项通过，${state.requiredServices.length - passed} 项需要处理。`,
        "error",
      );
    }
    updateSummary();
  }

  function setTestStatus(service, status, message) {
    state.tests[service] = status;
    setSectionStatus(
      service,
      status,
      {
        idle: "待测试",
        loading: "测试中",
        success: "已通过",
        error: "未通过",
      }[status],
    );

    const output = document.querySelector(`[data-test-message="${service}"]`);
    if (output) {
      output.className = `test-message message-${status}`;
      output.textContent = message || "";
    }

    updateSummary();
  }

  function setSectionStatus(section, status, label) {
    const badge = document.querySelector(`[data-section-status="${section}"]`);
    const nav = document.querySelector(
      `[data-nav-section="${section}"] .nav-status`,
    );

    if (badge) {
      badge.className = `section-state status-${status}`;
      const text = badge.querySelector("span:last-child");
      if (text) {
        text.textContent = label;
      }
    }

    if (nav) {
      nav.className = `nav-status status-${status}`;
      nav.textContent = label;
    }
  }

  function updateLocalSectionStates() {
    const platformValid = sectionLooksComplete("platform");
    const advancedValid = sectionLooksComplete("advanced");
    setSectionStatus(
      "platform",
      platformValid ? "success" : "idle",
      platformValid ? "已填写" : "待填写",
    );
    setSectionStatus(
      "advanced",
      advancedValid ? "success" : "idle",
      advancedValid ? "已确认" : "待确认",
    );
  }

  function sectionLooksComplete(section) {
    const controls = document.querySelectorAll(
      `[data-section="${section}"] [data-path]`,
    );
    return Array.from(controls).every((control) => {
      if (!control.required) {
        return true;
      }
      if (control.dataset.retained === "true" && !control.value.trim()) {
        return true;
      }
      return control.value.trim() !== "";
    });
  }

  function setServiceButtonsBusy(service, busy) {
    document
      .querySelectorAll(`[data-test-service="${service}"]`)
      .forEach((button) => {
        setButtonBusy(button, busy, "正在测试");
      });
  }

  function setButtonBusy(button, busy, busyText = "处理中") {
    if (!button) {
      return;
    }
    if (busy) {
      button.dataset.originalText ||= button.textContent;
      button.textContent = busyText;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    } else {
      button.textContent = button.dataset.originalText || button.textContent;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  function fingerprintService(service) {
    const profile = collectProfile();
    return stableStringify(profile[service] || {});
  }

  function stableStringify(value) {
    if (Array.isArray(value)) {
      return `[${value.map(stableStringify).join(",")}]`;
    }
    if (value && typeof value === "object") {
      return `{${Object.keys(value)
        .sort()
        .map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
        .join(",")}}`;
    }
    return JSON.stringify(value);
  }

  function allTestsPassed() {
    return state.requiredServices.every(
      (service) =>
        state.tests[service] === "success" &&
        state.fingerprints[service] === fingerprintService(service),
    );
  }

  function updateSummary() {
    const passed = state.requiredServices.filter(
      (service) => state.tests[service] === "success",
    ).length;
    const requiredCount = state.requiredServices.length;
    elements.progress.max = requiredCount;
    elements.progress.value = passed;
    elements.progress.textContent = `${passed} / ${requiredCount}`;
    elements.progressCount.textContent = `${passed} / ${requiredCount}`;
    elements.stickyCopy.textContent = `${passed} / ${requiredCount} 项必选测试已通过`;

    const ready = allTestsPassed();
    elements.generate.disabled = !ready;
    elements.stickyGenerate.disabled = !ready;
    elements.generateHelp.textContent = ready
      ? "全部必选依赖已通过，可以安全生成本地配置。"
      : `还需通过 ${requiredCount - passed} 项必选连通性测试。`;
    elements.stickyTitle.textContent = ready
      ? "全部必选依赖已通过"
      : "还未完成必选测试";

    renderRequirements();
  }

  function renderRequirements() {
    const fragment = document.createDocumentFragment();
    state.requiredServices.forEach((service) => {
      const item = document.createElement("div");
      const success = state.tests[service] === "success";
      item.className = `requirement-item${success ? " requirement-success" : ""}`;
      item.textContent = `${SERVICE_LABELS[service]} ${
        success ? "已通过" : "需要通过连通性测试"
      }`;
      fragment.appendChild(item);
    });
    state.requirements
      .filter((requirement) => {
        const key =
          typeof requirement === "object"
            ? requirement.service || requirement.key
            : "";
        return !SERVICES.includes(key);
      })
      .forEach((requirement) => {
        const item = document.createElement("div");
        const success =
          typeof requirement === "object" && requirement.ok === true;
        item.className = `requirement-item${
          success ? " requirement-success" : ""
        }`;
        if (typeof requirement === "string") {
          item.textContent = requirement;
        } else {
          const label =
            requirement.label ||
            requirement.message ||
            requirement.name ||
            "完整功能配置要求";
          item.textContent = requirement.value
            ? `${label}: ${requirement.value}`
            : label;
          if (requirement.help) {
            item.title = requirement.help;
          }
        }
        fragment.appendChild(item);
      });

    elements.requirements.replaceChildren(fragment);
  }

  async function generateConfig() {
    if (!validateAll()) {
      showToast("还有内容需要修正，请检查红色提示。", "error");
      return;
    }

    if (!allTestsPassed()) {
      showToast("配置已变化或测试未完成，请先测试全部必选依赖。", "error");
      return;
    }

    setButtonBusy(elements.generate, true, "正在生成");
    setButtonBusy(elements.stickyGenerate, true, "正在生成");

    try {
      const profile = collectProfile();
      const payload = await apiRequest("/api/generate", {
        method: "POST",
        body: profile,
      });

      if (!responseIsSuccessful(payload)) {
        throw new Error(responseMessage(payload, "配置文件生成失败。"));
      }

      state.generated = true;
      state.hasConfig = true;
      state.commands = payload.commands || state.commands;
      elements.existingNotice.hidden = false;
      const noticeTitle = elements.existingNotice.querySelector("strong");
      const noticeCopy = elements.existingNotice.querySelector("span");
      if (noticeTitle) {
        noticeTitle.textContent = "已发现本地配置";
      }
      if (noticeCopy) {
        noticeCopy.textContent =
          "页面已加载可安全展示的内容。密码框留空表示继续使用原密码。";
      }
      elements.generatedResult.hidden = false;
      elements.generatedPath.textContent =
        payload.message ||
        (payload.path || payload.config_path
          ? `配置已写入 ${payload.path || payload.config_path}`
          : "配置已写入项目的本地配置目录。");
      elements.saveState.textContent = "本地配置已生成";
      setSectionStatus("finish", "success", "已生成");
      renderCommands(state.commands);
      elements.generatedResult.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
      showToast("本地配置文件已生成。", "success");
    } catch (error) {
      showToast(safeErrorMessage(error), "error");
      setSectionStatus("finish", "error", "生成失败");
    } finally {
      setButtonBusy(elements.generate, false);
      setButtonBusy(elements.stickyGenerate, false);
      updateSummary();
    }
  }

  function renderCommands(commands) {
    const normalized = normalizeCommands(commands);
    const fragment = document.createDocumentFragment();

    if (!normalized.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent =
        "启动命令未从服务端返回。请查看 config 目录中的 README 说明。";
      fragment.appendChild(empty);
      elements.commandList.replaceChildren(fragment);
      return;
    }

    normalized.forEach(({ label, command }) => {
      const item = document.createElement("div");
      item.className = "command-item";

      const title = document.createElement("strong");
      title.textContent = label;

      const code = document.createElement("code");
      code.textContent = command;

      const copy = document.createElement("button");
      copy.className = "secondary-button copy-button";
      copy.type = "button";
      copy.textContent = "复制";
      copy.addEventListener("click", () => copyText(command, copy));

      item.append(title, code, copy);
      fragment.appendChild(item);
    });

    elements.commandList.replaceChildren(fragment);
  }

  function normalizeCommands(commands) {
    if (Array.isArray(commands)) {
      return commands
        .map((item, index) =>
          typeof item === "string"
            ? { label: `终端 ${index + 1}`, command: item }
            : {
                label: item.label || item.name || `终端 ${index + 1}`,
                command: item.command || item.value || "",
              },
        )
        .filter((item) => item.command);
    }

    if (!commands || typeof commands !== "object") {
      return [];
    }

    const currentPlatform = detectPlatformKey();
    const platformCommands =
      commands[currentPlatform] ||
      (currentPlatform === "windows" ? null : commands.unix);
    if (platformCommands) {
      return normalizeCommands(platformCommands);
    }
    const source = commands;

    return Object.entries(source)
      .flatMap(([key, value]) => {
        const label = commandLabel(key);
        if (typeof value === "string") {
          return [{ label, command: value }];
        }
        if (Array.isArray(value)) {
          return value.map((command, index) => ({
            label: value.length > 1 ? `${label} ${index + 1}` : label,
            command:
              typeof command === "string"
                ? command
                : command.command || command.value || "",
          }));
        }
        if (value && typeof value === "object") {
          const platformValue =
            value[currentPlatform] ||
            value.command ||
            value.value ||
            value.windows ||
            value.unix;
          return platformValue
            ? [{ label, command: String(platformValue) }]
            : [];
        }
        return [];
      })
      .filter((item) => item.command);
  }

  function commandLabel(key) {
    const labels = {
      backend: "后端",
      executor: "后台任务 executor",
      worker: "后台任务 executor",
      frontend: "前端",
      browser: "Chromium CDP",
      config: "配置中心",
    };
    return labels[String(key).toLowerCase()] || String(key);
  }

  function detectPlatformKey() {
    const platform = navigator.userAgentData?.platform || navigator.platform || "";
    if (/win/i.test(platform)) {
      return "windows";
    }
    if (/mac/i.test(platform)) {
      return "macos";
    }
    return "linux";
  }

  async function runtimeCheck() {
    setButtonBusy(elements.runtimeCheck, true, "正在复检");
    elements.runtimeResult.textContent = "正在检查启动状态和运行时依赖。";

    try {
      const payload = await apiRequest("/api/runtime-check", {
        method: "POST",
        body: { profile: collectProfile() },
      });
      renderRuntimeResult(payload);
    } catch (error) {
      elements.runtimeResult.textContent = safeErrorMessage(error);
      showToast("启动后复检未完成，请查看提示。", "error");
    } finally {
      setButtonBusy(elements.runtimeCheck, false);
    }
  }

  function renderRuntimeResult(payload) {
    const resultSource =
      payload.results ||
      payload.checks ||
      payload.services ||
      (Array.isArray(payload) ? payload : []);
    const entries = Array.isArray(resultSource)
      ? resultSource.map((item, index) => [
          item.service || item.name || `检查 ${index + 1}`,
          item,
        ])
      : Object.entries(resultSource);

    if (!entries.length) {
      const message = responseMessage(
        payload,
        responseIsSuccessful(payload)
          ? "启动后复检通过。"
          : "启动后复检未通过。",
      );
      elements.runtimeResult.textContent = message;
      showToast(
        message,
        responseIsSuccessful(payload) ? "success" : "error",
      );
      return;
    }

    const list = document.createElement("div");
    list.className = "runtime-result-list";
    let allSuccess = true;

    entries.forEach(([name, result]) => {
      const success =
        typeof result === "boolean" ? result : responseIsSuccessful(result);
      allSuccess &&= success;

      const item = document.createElement("div");
      item.className = `runtime-item ${success ? "success" : "error"}`;

      const label = document.createElement("span");
      label.textContent = SERVICE_LABELS[name] || result.label || name;

      const message = document.createElement("strong");
      message.textContent = success
        ? "正常"
        : responseMessage(result, "需要处理");

      item.append(label, message);
      list.appendChild(item);
    });

    elements.runtimeResult.replaceChildren(list);
    showToast(
      allSuccess ? "启动后复检全部通过。" : "复检发现问题，请查看结果。",
      allSuccess ? "success" : "error",
    );
  }

  function showGuide(key) {
    const serverGuide = state.guides[key];
    const guide = normalizeGuide(serverGuide, fallbackGuides[key]);

    elements.guideTitle.textContent =
      guide.title || "如何找到配置信息";
    const fragment = document.createDocumentFragment();

    if (guide.intro) {
      const intro = document.createElement("p");
      intro.textContent = guide.intro;
      fragment.appendChild(intro);
    }

    if (guide.warning) {
      const warning = document.createElement("p");
      warning.className = "guide-warning";
      warning.textContent = guide.warning;
      fragment.appendChild(warning);
    }

    Object.entries(guide.platforms || {}).forEach(([platform, steps]) => {
      const section = document.createElement("section");
      section.className = "guide-platform";

      const title = document.createElement("h3");
      title.textContent = platform;
      section.appendChild(title);

      (Array.isArray(steps) ? steps : [steps]).forEach((step) => {
        if (typeof step === "string") {
          const paragraph = document.createElement("p");
          paragraph.textContent = step;
          section.appendChild(paragraph);
          return;
        }

        if (step.command) {
          const commandBox = document.createElement("div");
          commandBox.className = "guide-command";
          const code = document.createElement("code");
          code.textContent = step.command;
          const copy = document.createElement("button");
          copy.type = "button";
          copy.className = "text-button copy-guide-command";
          copy.textContent = "复制";
          copy.setAttribute("aria-label", `复制 ${platform} 命令`);
          copy.addEventListener("click", () => copyText(step.command, copy));
          commandBox.append(code, copy);
          section.appendChild(commandBox);
        } else if (step.text || step.message) {
          const paragraph = document.createElement("p");
          paragraph.textContent = step.text || step.message;
          section.appendChild(paragraph);
        }
      });

      fragment.appendChild(section);
    });

    elements.guideContent.replaceChildren(fragment);
    elements.guideDialog.showModal();
  }

  function normalizeGuide(serverGuide, fallback) {
    if (!serverGuide) {
      return fallback || {
        title: "配置帮助",
        intro: "服务端暂未提供这一项的操作说明。",
        platforms: {},
      };
    }

    if (typeof serverGuide === "string") {
      return {
        title: fallback?.title || "配置帮助",
        intro: serverGuide,
        warning: fallback?.warning || "",
        platforms: fallback?.platforms || {},
      };
    }

    if (Array.isArray(serverGuide)) {
      return {
        title: fallback?.title || "配置帮助",
        intro: fallback?.intro || "",
        warning: fallback?.warning || "",
        platforms: { 通用步骤: serverGuide },
      };
    }

    const serverPlatforms =
      serverGuide.platforms ||
      serverGuide.steps_by_platform ||
      {
        Windows: serverGuide.windows,
        Ubuntu: serverGuide.ubuntu || serverGuide.linux,
        macOS: serverGuide.macos || serverGuide.mac,
      };

    const filteredPlatforms = Object.fromEntries(
      Object.entries(serverPlatforms).filter(([, value]) => value),
    );

    return {
      title: serverGuide.title || fallback?.title,
      intro:
        serverGuide.intro ||
        serverGuide.description ||
        serverGuide.summary ||
        fallback?.intro ||
        "",
      warning: serverGuide.warning || fallback?.warning || "",
      platforms: Object.keys(filteredPlatforms).length
        ? filteredPlatforms
        : fallback?.platforms || {},
    };
  }

  function toggleSecret(button) {
    const input = document.getElementById(button.dataset.toggleSecret);
    if (!input) {
      return;
    }
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    button.textContent = showing ? "显示" : "隐藏";
    button.setAttribute(
      "aria-label",
      `${showing ? "显示" : "隐藏"}${input.labels?.[0]?.textContent || "密码"}`,
    );
  }

  function generateSecretValues() {
    const values = {
      "platform.first_admin_password": securePassword(22),
      "platform.secret_key": randomBase64Url(48),
      "platform.encryption_key": randomBase64Url(32, true),
      "advanced.api_hub_mcp_token": randomBase64Url(32),
      "advanced.api_hub_system_mcp_token": randomBase64Url(32),
      "advanced.api_hub_internal_proxy_token": randomBase64Url(32),
    };

    Object.entries(values).forEach(([path, value]) => {
      const control = document.querySelector(`[data-path="${path}"]`);
      if (!control) {
        return;
      }
      control.value = value;
      control.dataset.retained = "false";
      control.required = true;
      control.placeholder = "";
      const helper = control.closest(".field")?.querySelector(".secret-help");
      if (helper) {
        helper.classList.remove("retained-secret");
        if (helper.dataset.originalText) {
          helper.textContent = helper.dataset.originalText;
        }
      }
      clearFieldError(path);
    });

    updateLocalSectionStates();
  }

  function hasRetainedCoreSecrets() {
    return [
      "platform.secret_key",
      "platform.encryption_key",
      "platform.first_admin_password",
    ].some(
      (path) =>
        document.querySelector(`[data-path="${path}"]`)?.dataset.retained ===
        "true",
    );
  }

  function securePassword(length) {
    const uppercase = "ABCDEFGHJKLMNPQRSTUVWXYZ";
    const lowercase = "abcdefghijkmnopqrstuvwxyz";
    const digits = "23456789";
    const symbols = "!@#$%*+-_";
    const all = uppercase + lowercase + digits + symbols;
    const required = [
      randomCharacter(uppercase),
      randomCharacter(lowercase),
      randomCharacter(digits),
      randomCharacter(symbols),
    ];
    while (required.length < length) {
      required.push(randomCharacter(all));
    }
    return secureShuffle(required).join("");
  }

  function randomCharacter(characters) {
    const max = Math.floor(256 / characters.length) * characters.length;
    const random = new Uint8Array(1);
    do {
      crypto.getRandomValues(random);
    } while (random[0] >= max);
    return characters[random[0] % characters.length];
  }

  function secureShuffle(values) {
    const output = [...values];
    for (let index = output.length - 1; index > 0; index -= 1) {
      const random = new Uint32Array(1);
      crypto.getRandomValues(random);
      const target = random[0] % (index + 1);
      [output[index], output[target]] = [output[target], output[index]];
    }
    return output;
  }

  function randomBase64Url(byteLength, keepPadding = false) {
    const bytes = new Uint8Array(byteLength);
    crypto.getRandomValues(bytes);
    let binary = "";
    bytes.forEach((byte) => {
      binary += String.fromCharCode(byte);
    });
    const encoded = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_");
    return keepPadding ? encoded : encoded.replace(/=+$/g, "");
  }

  function responseIsSuccessful(payload) {
    if (typeof payload === "boolean") {
      return payload;
    }
    if (!payload || typeof payload !== "object") {
      return false;
    }
    if (payload.ok !== undefined) {
      return Boolean(payload.ok);
    }
    if (payload.success !== undefined) {
      return Boolean(payload.success);
    }
    if (payload.connected !== undefined) {
      return Boolean(payload.connected);
    }
    if (payload.passed !== undefined) {
      return Boolean(payload.passed);
    }
    if (payload.status !== undefined) {
      return ["ok", "success", "passed", "healthy", "ready"].includes(
        String(payload.status).toLowerCase(),
      );
    }
    return true;
  }

  function responseMessage(payload, fallback) {
    if (!payload || typeof payload !== "object") {
      return fallback;
    }
    const detail = payload.message || payload.detail || payload.error;
    if (typeof detail === "string" && detail.trim()) {
      return redactSensitiveText(detail.trim());
    }
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const nestedMessage =
        detail.message || detail.detail || detail.error || fallback;
      const missing = Array.isArray(detail.missing_services)
        ? detail.missing_services
            .map((service) => SERVICE_LABELS[service] || service)
            .join("、")
        : "";
      return redactSensitiveText(
        missing
          ? `${nestedMessage}。需要重新测试: ${missing}`
          : String(nestedMessage),
      );
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => item.msg || item.message || String(item))
        .join("；");
    }
    return fallback;
  }

  function extractLatency(payload) {
    const value =
      payload.latency_ms ||
      payload.elapsed_ms ||
      payload.duration_ms ||
      payload.details?.latency_ms;
    return Number.isFinite(Number(value)) ? Math.round(Number(value)) : 0;
  }

  async function apiRequest(url, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 190000);
    const headers = {
      Accept: "application/json",
      "X-Config-Access-Token": state.accessToken,
    };

    if (options.method && options.method !== "GET") {
      headers["Content-Type"] = "application/json";
      if (state.csrfToken) {
        headers["X-CSRF-Token"] = state.csrfToken;
      }
    }

    try {
      const response = await fetch(url, {
        method: options.method || "GET",
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: "same-origin",
        signal: controller.signal,
      });

      const text = await response.text();
      let payload = {};
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = { message: text };
        }
      }

      if (!response.ok) {
        throw new Error(
          responseMessage(payload, `请求失败，状态码 ${response.status}。`),
        );
      }

      if (payload && typeof payload === "object" && payload.data) {
        return { ...payload.data, ...payload };
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error("连接超时，请确认服务已经启动且地址可以访问。");
      }
      if (error instanceof TypeError) {
        throw new Error("无法连接配置中心，请确认本地服务仍在运行。");
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function safeErrorMessage(error) {
    if (!error) {
      return "发生未知错误，请重试。";
    }
    return redactSensitiveText(error.message || String(error));
  }

  function redactSensitiveText(text) {
    return String(text)
      .replace(
        /(password|passwd|secret|token|api[_ -]?key)(\s*[:=]\s*)[^\s,;]+/gi,
        "$1$2[已隐藏]",
      )
      .replace(/:\/\/([^:/\s]+):([^@/\s]+)@/g, "://$1:[已隐藏]@");
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      const original = button.textContent;
      button.textContent = "已复制";
      button.classList.add("copied");
      window.setTimeout(() => {
        button.textContent = original;
        button.classList.remove("copied");
      }, 1600);
    } catch {
      showToast("浏览器未允许复制，请手动选择命令。", "error");
    }
  }

  function showToast(message, kind = "") {
    const toast = document.createElement("div");
    toast.className = `toast ${kind}`.trim();
    toast.textContent = message;
    elements.toastRegion.appendChild(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  function showBootstrapError(error) {
    const message = safeErrorMessage(error);
    elements.loading.classList.add("bootstrap-failed");
    elements.loading.querySelector("p").textContent = message;
    elements.saveState.textContent = "初始化失败";
    elements.globalError.textContent = message;
    elements.globalError.hidden = false;

    if (message.includes("访问令牌")) {
      return;
    }
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "primary-button";
    retry.textContent = "重新读取";
    retry.addEventListener("click", () => window.location.reload());
    elements.loading.appendChild(retry);
  }

  function setupSectionObserver() {
    if (!("IntersectionObserver" in window)) {
      return;
    }
    const links = new Map(
      Array.from(document.querySelectorAll("[data-nav-section]")).map((link) => [
        link.dataset.navSection,
        link,
      ]),
    );
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (!visible) {
          return;
        }
        links.forEach((link) => link.removeAttribute("aria-current"));
        links
          .get(visible.target.dataset.section)
          ?.setAttribute("aria-current", "location");
      },
      { rootMargin: "-18% 0px -68% 0px", threshold: [0.05, 0.25] },
    );
    document
      .querySelectorAll("[data-section]")
      .forEach((section) => observer.observe(section));
  }

  bootstrap();
})();
