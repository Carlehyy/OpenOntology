"""输入框快捷内容 CRUD 与优先级重排（独立子路由：router.py 已贴近架构测试行数上限）。

快捷内容是用户自管的常用文本片段（标题 + 内容详情），前端输入框工具栏
点击后插入光标处。sort_order 是相对展示优先级：列表恒按
(sort_order, created_at) 升序返回，新建追加末尾，重排按全量顺序归一化。
路径与主路由同前缀，鉴权由 main.py 挂载时的 menu_guard("super_assistant")
统一声明；owner 隔离在查询/变更处强制（他人 id 一律 404，不泄露存在性）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant.models import SuperAssistantQuickInput
from app.super_assistant.schemas import (
    QuickInputCreate,
    QuickInputOut,
    QuickInputReorder,
    QuickInputUpdate,
)

router = APIRouter()

_ORDER_BY = (SuperAssistantQuickInput.sort_order, SuperAssistantQuickInput.created_at)


def _list_owned(db: Session, owner_id: str) -> list[SuperAssistantQuickInput]:
    return list(
        db.query(SuperAssistantQuickInput)
        .filter(SuperAssistantQuickInput.owner_id == owner_id)
        .order_by(*_ORDER_BY)
        .all()
    )


@router.get("/quick-inputs", response_model=list[QuickInputOut])
def list_quick_inputs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[QuickInputOut]:
    return _list_owned(db, current_user.id)  # type: ignore[return-value]


@router.post("/quick-inputs", response_model=QuickInputOut, status_code=status.HTTP_201_CREATED)
def create_quick_input(
    body: QuickInputCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuperAssistantQuickInput:
    # 追加到末尾：并发首建可能同值，列表 (sort_order, created_at) 兜底，
    # 下一次重排归一化（与设置弹窗的保存语义一致）
    max_order = (
        db.query(func.max(SuperAssistantQuickInput.sort_order))
        .filter(SuperAssistantQuickInput.owner_id == current_user.id)
        .scalar()
    ) or 0
    entry = SuperAssistantQuickInput(
        owner_id=current_user.id,
        title=body.title,
        content=body.content,
        sort_order=max_order + 1,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _get_owned(db: Session, owner_id: str, entry_id: str) -> SuperAssistantQuickInput:
    entry = (
        db.query(SuperAssistantQuickInput)
        .filter(
            SuperAssistantQuickInput.id == entry_id,
            SuperAssistantQuickInput.owner_id == owner_id,
        )
        .one_or_none()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="快捷内容不存在")
    return entry


@router.patch("/quick-inputs/{entry_id}", response_model=QuickInputOut)
def update_quick_input(
    entry_id: str,
    body: QuickInputUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuperAssistantQuickInput:
    entry = _get_owned(db, current_user.id, entry_id)
    if body.title is not None:
        entry.title = body.title
    if body.content is not None:
        entry.content = body.content
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/quick-inputs/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quick_input(
    entry_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    entry = _get_owned(db, current_user.id, entry_id)
    db.delete(entry)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/quick-inputs/reorder", response_model=list[QuickInputOut])
def reorder_quick_inputs(
    body: QuickInputReorder,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[SuperAssistantQuickInput]:
    owned = {entry.id: entry for entry in _list_owned(db, current_user.id)}
    # 宽松口径：只重排属于该用户的 id（重复/未知 id 忽略），归一化为 0..n-1；
    # 未列出的条目保持原 sort_order（不保证排在列出条目之后，排序兜底
    # (sort_order, created_at)；当前客户端恒发全量，部分列表属并发边界）
    seen: set[str] = set()
    order = 0
    for entry_id in body.ids:
        entry = owned.get(entry_id)
        if entry is None or entry_id in seen:
            continue
        seen.add(entry_id)
        entry.sort_order = order
        order += 1
    db.commit()
    return _list_owned(db, current_user.id)  # type: ignore[return-value]
