#!/usr/bin/env python3
"""下载语音听写所需的 SenseVoice 模型文件（显式运维步骤，绝非后台静默下载）。

从 HuggingFace 仓库 csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-
2024-07-17 下载 model.int8.onnx（约 228MB）与 tokens.txt，逐文件 sha256
校验通过后原子落盘。目标目录默认取 VOICE_MODEL_DIR 环境变量，未设置时
为 backend/voice-models/。国内网络可用 --mirror https://hf-mirror.com。

用法（backend/ 目录下）：
    uv run python scripts/download_voice_model.py
    uv run python scripts/download_voice_model.py --force --mirror https://hf-mirror.com
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
DEFAULT_BASE = "https://huggingface.co"
MIRROR_HINT = "https://hf-mirror.com"

# 哈希与字节数取自 HuggingFace LFS 元数据（2026-10-11 核验）。
FILES: dict[str, tuple[str, int]] = {
    "model.int8.onnx": (
        "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
        239_233_841,
    ),
    "tokens.txt": (
        "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        315_894,
    ),
}


def default_dest() -> Path:
    configured = os.environ.get("VOICE_MODEL_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "voice-models"


def file_matches(path: Path, expected_sha256: str, expected_size: int) -> bool:
    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256


def download(url: str, dest: Path, expected_sha256: str, expected_size: int) -> None:
    digest = hashlib.sha256()
    downloaded = 0
    next_milestone = 32 * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_file = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as response, part_file.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            digest.update(chunk)
            out.write(chunk)
            if downloaded >= next_milestone:
                sys.stdout.write(
                    f"\r  {dest.name}: {downloaded / (1024 * 1024):.0f}MB"
                    f" / {expected_size / (1024 * 1024):.0f}MB"
                )
                sys.stdout.flush()
                next_milestone += 32 * 1024 * 1024
    sys.stdout.write("\n")
    if downloaded != expected_size:
        part_file.unlink(missing_ok=True)
        raise SystemExit(
            f"{dest.name} 大小不符：期望 {expected_size}，实收 {downloaded}"
        )
    actual = digest.hexdigest()
    if actual != expected_sha256:
        part_file.unlink(missing_ok=True)
        raise SystemExit(f"{dest.name} sha256 校验失败：{actual}")
    part_file.replace(dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="模型目标目录（默认取 VOICE_MODEL_DIR，未设置为 backend/voice-models）",
    )
    parser.add_argument(
        "--mirror",
        default=None,
        help=f"下载源 base URL（默认 {DEFAULT_BASE}；国内可用 {MIRROR_HINT}）",
    )
    parser.add_argument("--force", action="store_true", help="已校验通过的文件也重新下载")
    args = parser.parse_args()

    dest_dir = args.dest or default_dest()
    base = (args.mirror or DEFAULT_BASE).rstrip("/")
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"模型目录：{dest_dir.resolve()}\n下载源：{base}")

    for name, (expected_sha256, expected_size) in FILES.items():
        dest = dest_dir / name
        if not args.force and file_matches(dest, expected_sha256, expected_size):
            print(f"{name}：已存在且校验通过，跳过")
            continue
        url = f"{base}/{REPO}/resolve/main/{name}"
        print(f"{name}：下载中（{expected_size / (1024 * 1024):.0f}MB）…")
        download(url, dest, expected_sha256, expected_size)
        print(f"{name}：sha256 校验通过")

    print("全部就绪：语音听写后端已可提供 /voice/transcribe。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
