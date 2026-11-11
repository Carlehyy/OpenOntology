from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    AdvancedConfig,
    BrowserConfig,
    ConfigProfile,
    HindsightConfig,
    MinioConfig,
    NatsConfig,
    PlatformConfig,
    default_profile,
)


def test_default_profile_generates_stable_security_material() -> None:
    profile = default_profile()

    assert len(profile.platform.first_admin_password) >= 12
    assert len(profile.platform.secret_key) >= 32
    assert len(profile.platform.encryption_key) == 44
    assert profile.advanced.api_hub_system_mcp_token
    assert profile.platform.backend_host == "127.0.0.1"
    assert profile.platform.frontend_host == "127.0.0.1"
    assert profile.postgres.host == "127.0.0.1"
    assert profile.postgres.port == 5432
    assert profile.postgres.database == "openontology"
    assert profile.postgres.username == "postgres"
    assert profile.redis.host == "localhost"
    assert profile.nats.host == "localhost"
    assert profile.nats.port == 4222
    assert profile.nats.token == ""
    assert profile.neo4j.uri == "neo4j://localhost:7687"
    assert profile.minio.endpoint == "localhost:9000"
    assert profile.minio.access_key == "admin"
    assert profile.hindsight.base_url == "http://127.0.0.1:18888"
    assert profile.hindsight.web_url == ""
    assert profile.hindsight.timeout_seconds == 2.0


def test_hindsight_config_validates_origin_and_timeout_bounds() -> None:
    config = HindsightConfig()
    assert config.base_url == "http://127.0.0.1:18888"
    assert config.web_url == ""
    assert config.timeout_seconds == 2.0

    with pytest.raises(ValidationError):
        HindsightConfig(base_url="http://127.0.0.1:18888/memory")
    with pytest.raises(ValidationError):
        HindsightConfig(base_url="hindsight:8888")
    with pytest.raises(ValidationError):
        HindsightConfig(timeout_seconds=0)
    with pytest.raises(ValidationError):
        HindsightConfig(timeout_seconds=61)
    assert HindsightConfig(base_url="http://hindsight:8888").base_url == (
        "http://hindsight:8888"
    )


def test_hindsight_web_url_is_optional_and_requires_clean_http_origin() -> None:
    assert HindsightConfig(web_url="").web_url == ""
    assert HindsightConfig(
        web_url="http://127.0.0.1:19999"
    ).web_url == "http://127.0.0.1:19999"
    assert HindsightConfig(web_url="https://hindsight.example/").web_url == (
        "https://hindsight.example/"
    )
    # 主机名按输入原样保存，不做自动改写。
    assert HindsightConfig(web_url="http://localhost:19999").web_url == (
        "http://localhost:19999"
    )

    with pytest.raises(ValidationError, match="完整的 http 或 https"):
        HindsightConfig(web_url="127.0.0.1:19999")
    with pytest.raises(ValidationError, match="账号或密码"):
        HindsightConfig(web_url="http://user:pass@127.0.0.1:19999")
    with pytest.raises(ValidationError, match="查询参数或片段"):
        HindsightConfig(web_url="http://127.0.0.1:19999/?token=1")
    with pytest.raises(ValidationError, match="服务根地址"):
        HindsightConfig(web_url="http://127.0.0.1:19999/admin")
    with pytest.raises(ValidationError, match="空白、控制字符或反斜杠"):
        HindsightConfig(web_url="http://127.0.0.1:19 999")
    with pytest.raises(ValidationError, match="空白、控制字符或反斜杠"):
        HindsightConfig(web_url="http://local host:19999")
    # urlsplit 会静默丢弃制表符，必须在解析前显式拒绝。
    with pytest.raises(ValidationError, match="空白、控制字符或反斜杠"):
        HindsightConfig(web_url="http://127.0.0.1:1999\t9")
    with pytest.raises(ValidationError, match="空白、控制字符或反斜杠"):
        HindsightConfig(web_url="http://127.0.0.1:1999\x019")
    with pytest.raises(ValidationError, match="空白、控制字符或反斜杠"):
        HindsightConfig(web_url="http://127.0.0.1:1999\x7f9")
    with pytest.raises(ValidationError, match="空白、控制字符或反斜杠"):
        HindsightConfig(web_url="http://127.0.0.1:19999\\admin")
    # 空的 ?/# 分隔符同样不是纯 origin。
    with pytest.raises(ValidationError, match="查询参数或片段"):
        HindsightConfig(web_url="http://127.0.0.1:19999?")
    with pytest.raises(ValidationError, match="查询参数或片段"):
        HindsightConfig(web_url="http://127.0.0.1:19999/#")
    with pytest.raises(ValidationError, match="端口无效"):
        HindsightConfig(web_url="http://127.0.0.1:notaport")
    with pytest.raises(ValidationError, match="端口无效"):
        HindsightConfig(web_url="http://127.0.0.1:70000")
    with pytest.raises(ValidationError):
        HindsightConfig(web_url="x" * 501)


def test_nats_config_validates_port_and_token_bounds() -> None:
    assert NatsConfig().host == "127.0.0.1"
    assert NatsConfig().port == 4222
    assert NatsConfig().token == ""
    assert NatsConfig(port=65535).port == 65535

    with pytest.raises(ValidationError):
        NatsConfig(port=0)
    with pytest.raises(ValidationError):
        NatsConfig(port=65536)
    with pytest.raises(ValidationError):
        NatsConfig(host="")
    with pytest.raises(ValidationError):
        NatsConfig(token="x" * 513)


@pytest.mark.parametrize(
    "updates",
    [
        {"backend_host": "0.0.0.0"},
        {"frontend_host": "192.168.1.10"},
        {"backend_port": 5173, "frontend_port": 5173},
    ],
)
def test_platform_rejects_non_local_or_duplicate_bindings(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PlatformConfig(**updates)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/openontology",
        "C:\\openontology",
        "../outside",
        "./runtime/../outside",
    ],
)
def test_advanced_paths_must_be_portable_project_relative(path: str) -> None:
    with pytest.raises(ValidationError):
        AdvancedConfig(uploads_dir=path)


def test_minio_rejects_console_port_and_scheme() -> None:
    with pytest.raises(ValidationError, match="9001"):
        MinioConfig(endpoint="127.0.0.1:9001")
    with pytest.raises(ValidationError, match="不要包含"):
        MinioConfig(endpoint="http://127.0.0.1:9000")


def test_browser_cdp_url_accepts_only_service_root() -> None:
    assert BrowserConfig(cdp_url="https://browser.example:9222/").cdp_url == (
        "https://browser.example:9222/"
    )
    with pytest.raises(ValidationError, match="只填写服务根地址"):
        BrowserConfig(cdp_url="https://browser.example:9222/json/version")


def test_profile_rejects_dotenv_interpolation_and_short_admin_password() -> None:
    profile = default_profile()
    payload = profile.model_dump()
    payload["postgres"]["password"] = "${HOME}"

    with pytest.raises(ValidationError, match="环境变量解析"):
        ConfigProfile.model_validate(payload)

    payload = profile.model_dump()
    payload["platform"]["first_admin_password"] = "short"
    with pytest.raises(ValidationError, match="至少需要 12"):
        ConfigProfile.model_validate(payload)

    payload = profile.model_dump()
    payload["platform"]["encryption_key"] = "not-a-fernet-key"
    with pytest.raises(ValidationError, match="Fernet"):
        ConfigProfile.model_validate(payload)
