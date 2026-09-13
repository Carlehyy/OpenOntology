"""独立 process plugin 宿主边界。

PluginCatalog 只负责 manifest 和生命周期策略；本模块负责受限子进程、
JSON-lines 请求和 drain。真正的 network/workspace sandbox 由部署层提供。
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Any, Mapping

from .contracts import ContractError
from .plugins import PluginManifest


class PluginHostError(ContractError):
    """Process plugin 违反宿主协议或无法运行。"""


class ProcessPluginHost:
    """每个插件 revision 一个独立进程的最小 JSON-lines host。"""

    def __init__(self, manifest: PluginManifest, *, secret_env: Mapping[str, str] | None = None):
        self.manifest = manifest
        self._secret_env = dict(secret_env or {})
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    def _command(self) -> list[str]:
        try:
            command = shlex.split(self.manifest.entrypoint)
        except ValueError as exc:
            raise PluginHostError("invalid plugin entrypoint") from exc
        if not command or any("\x00" in part for part in command):
            raise PluginHostError("invalid plugin entrypoint")
        return command

    def _env(self) -> dict[str, str]:
        # Backend credentials never flow into an untrusted child implicitly.
        env = {key: value for key, value in os.environ.items() if key.startswith("PLUGIN_")}
        env.update({str(key): str(value) for key, value in self._secret_env.items()})
        return env

    async def start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        cwd = self.manifest.workspace_scope[0] if self.manifest.workspace_scope else None
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Structured responses are the only plugin diagnostic channel.
                # A never-read stderr pipe can otherwise block a noisy child.
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd, env=self._env(),
            )
        except (OSError, ValueError) as exc:
            raise PluginHostError(f"plugin process start failed: {exc}") from exc

    async def _request(self, payload: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
        async with self._lock:
            await self.start()
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise PluginHostError("plugin process is unavailable")
            try:
                process.stdin.write((json.dumps(dict(payload), ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                # A timed-out child is no longer trusted to consume the next
                # request.  Kill it before returning so no process or secret
                # environment survives a failed call.
                await self.stop()
                raise PluginHostError("plugin process request timed out") from exc
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                await self.stop()
                raise PluginHostError("plugin process is unavailable") from exc
            if not line:
                await self.stop()
                raise PluginHostError("plugin process exited without a response")
            if len(line) > 1024 * 1024:
                await self.stop()
                raise PluginHostError("plugin response frame is too large")
            try:
                value = json.loads(line.decode())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                await self.stop()
                raise PluginHostError("plugin response is not valid JSON") from exc
            if not isinstance(value, dict) or any(key in value for key in ("capabilities", "permissions")):
                await self.stop()
                raise PluginHostError("plugin response must not expand capabilities")
            return value

    async def health(self, *, timeout: float = 5.0) -> dict[str, Any]:
        value = await self._request(
            {"op": "health", "key": self.manifest.key, "revision": self.manifest.revision},
            timeout=timeout,
        )
        # Identity and protocol are mandatory handshake facts.  Accepting a
        # missing value would let a stale or unrelated process pass health.
        if value.get("key") != self.manifest.key or value.get("revision") != self.manifest.revision:
            await self.stop()
            raise PluginHostError("plugin health identity mismatch")
        if value.get("protocol") != "plugin.v1":
            await self.stop()
            raise PluginHostError("unsupported plugin protocol")
        if value.get("ok") is not True:
            await self.stop()
            raise PluginHostError("plugin health check failed")
        return value

    async def invoke(self, request: Mapping[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
        return await self._request({"op": "invoke", "request": dict(request)}, timeout=timeout)

    async def stop(self, *, grace_seconds: float = 2.0) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=max(0.0, grace_seconds))
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def __aenter__(self) -> "ProcessPluginHost":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()
