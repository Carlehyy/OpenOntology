"""运营报告 Markdown 特殊块解析与保存期校验。"""
from __future__ import annotations

import re
from dataclasses import dataclass


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,80}$")
# 等价于前端 reportModel.ts 的 /^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$/：
# key 字符集与空白、冒号互斥，先整行 strip 再单遍匹配前缀，没有会产生
# 二次回溯的贪婪/懒惰量词组合，长空白行保持线性。
_CONFIG_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\s*:")


@dataclass(frozen=True)
class ReportBlock:
    kind: str
    name: str = ""
    body: str = ""
    config: dict[str, str] | None = None


@dataclass(frozen=True)
class MarkdownValidation:
    errors: tuple[str, ...]
    blocks: tuple[ReportBlock, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _config(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _CONFIG_KEY.match(stripped)
        if match:
            result[match.group(1)] = stripped[match.end():].strip()
    return result


def parse_blocks(markdown: str) -> tuple[ReportBlock, ...]:
    lines = str(markdown or "").splitlines()
    blocks: list[ReportBlock] = []
    prose: list[str] = []
    fence: tuple[str, int] | None = None
    info = ""
    body: list[str] = []

    def flush_prose() -> None:
        nonlocal prose
        if any(line.strip() for line in prose):
            blocks.append(ReportBlock(kind="prose", body="\n".join(prose).strip()))
        prose = []

    def flush_fence() -> None:
        nonlocal fence, info, body
        language = info.strip().split(None, 1)[0].lower() if info.strip() else ""
        payload = "\n".join(body).strip("\n")
        if language == "sql":
            parts = info.strip().split()
            raw_name = parts[1] if len(parts) > 1 else ""
            name = raw_name.removeprefix("name=")
            blocks.append(ReportBlock(kind="sql", name=name, body=payload))
        elif language in {"chart", "filter"}:
            blocks.append(ReportBlock(kind=language, body=payload, config=_config(payload)))
        else:
            blocks.append(ReportBlock(kind="code", body=payload))
        fence = None
        info = ""
        body = []

    for line in lines:
        match = _FENCE.match(line)
        if match:
            marker, marker_info = match.groups()
            if fence is None:
                flush_prose()
                fence = (marker[0], len(marker))
                info = marker_info
                body = []
                continue
            if marker[0] == fence[0] and len(marker) >= fence[1] and not marker_info.strip():
                flush_fence()
                continue
        if fence is None:
            prose.append(line)
        else:
            body.append(line)
    if fence is not None:
        # 保存期不丢失未闭合内容：与前面的正文合并成一个 prose 块，避免
        # 一个不完整围栏意外制造两个相邻目录节点。
        unfinished = (["```" + info] if info else ["```"]) + body
        if blocks and blocks[-1].kind == "prose":
            previous = blocks[-1]
            blocks[-1] = ReportBlock(kind="prose", body="\n".join((previous.body, *prose, *unfinished)).strip())
            prose = []
        else:
            prose.extend(unfinished)
    flush_prose()
    return tuple(blocks)


def validate_markdown(markdown: str) -> MarkdownValidation:
    errors: list[str] = []
    if not isinstance(markdown, str) or not markdown.strip():
        errors.append("文章正文不能为空")
        return MarkdownValidation(tuple(errors), ())
    if len(markdown) > 700_000:
        errors.append("文章正文超过 700KB 上限")
    blocks = parse_blocks(markdown)
    names: set[str] = set()
    chart_refs: list[str] = []
    filters: set[str] = set()
    for block in blocks:
        if block.kind == "sql":
            if not block.name or not _NAME.fullmatch(block.name):
                errors.append("命名 SQL 必须使用字母开头的短名称")
            elif block.name in names:
                errors.append(f"命名 SQL 重复：{block.name}")
            else:
                names.add(block.name)
            if not block.body.strip():
                errors.append(f"命名 SQL {block.name or '（未命名）'} 不能为空")
        elif block.kind == "chart":
            config = block.config or {}
            query = config.get("query", "").strip()
            chart_type = config.get("type", "table").strip()
            if query:
                chart_refs.append(query)
            else:
                errors.append("图表块缺少 query")
            if chart_type not in {"bar", "line", "pie", "table", "kpi"}:
                errors.append(f"不支持的图表类型：{chart_type}")
            if chart_type in {"bar", "line", "pie"} and not config.get("x", "").strip():
                errors.append(f"{chart_type} 图表需要 x 列")
            y = [item.strip() for item in config.get("y", config.get("value", "")).split(",") if item.strip()]
            if chart_type == "kpi" and len(y) != 1:
                errors.append("KPI 图表需要恰好一个 y/value 列")
            if chart_type == "pie" and len(y) != 1:
                errors.append("饼图需要恰好一个 y 列")
        elif block.kind == "filter":
            config = block.config or {}
            name = config.get("name", "").strip()
            filter_type = config.get("type", "text").strip()
            if not name or not _NAME.fullmatch(name):
                errors.append("筛选器 name 必须使用字母开头的短名称")
            elif name in filters:
                errors.append(f"筛选器重复：{name}")
            else:
                filters.add(name)
            if filter_type not in {"text", "number", "date"}:
                errors.append(f"不支持的筛选类型：{filter_type}")
    for query in chart_refs:
        if query not in names:
            errors.append(f"图表引用了不存在的查询：{query}")
    return MarkdownValidation(tuple(errors), blocks)
