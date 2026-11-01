import { useEffect, useRef } from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';

interface UnsavedChangesDialogProps {
  /** 留在当前编辑器（取消离开） */
  onStay: () => void;
  /** 放弃未保存的结构改动并离开 */
  onLeave: () => void;
}

/**
 * 本体结构未保存离开确认弹窗。
 *
 * 触发来源：画布左列「返回」、左上角本体图标（与返回同一 requestLeave）、
 * 浏览器前进/后退（Chromium Navigation API 拦截）。节点位置由画布布局接口
 * 自动保存，不属于"未保存结构"，因此文案只针对模型结构改动。
 *
 * 层级：画布根节点是 fixed z-[9999]，全站 Dialog 走 body 传送门（--z-modal）
 * 会被盖住，这里沿用本域 DeleteSelectedDialog 的固定浮层做法，z-[130] 高于
 * 面板（z-80/z-110）与搜索（z-120）。
 */
export default function UnsavedChangesDialog({ onStay, onLeave }: UnsavedChangesDialogProps) {
  const stayButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // 安全默认：焦点落在「留在此页」，误触 Enter 不会丢弃改动
    stayButtonRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onStay();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onStay]);

  return (
    <div
      className="fixed inset-0 z-[130] flex items-center justify-center bg-black/50 backdrop-blur-sm"
      onClick={onStay}
      data-testid="graph-unsaved-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="graph-unsaved-title"
      aria-describedby="graph-unsaved-desc"
    >
      <div
        className="w-full max-w-md mx-4 rounded-2xl border border-amber-500/40 bg-surface-800 p-5 shadow-2xl animate-fade-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <div className="shrink-0 rounded-xl bg-amber-500/20 p-2">
            <ExclamationTriangleIcon className="w-5 h-5 text-amber-400" />
          </div>
          <div className="min-w-0">
            <h3 id="graph-unsaved-title" className="font-display text-base font-semibold text-surface-100">
              当前结构已更新，尚未保存
            </h3>
            <p id="graph-unsaved-desc" className="mt-2 text-xs leading-relaxed text-surface-300">
              本体结构已修改，但还没有点击右上角保存。离开后，未保存的结构改动将丢失；
              节点位置会自动保存，不受影响。
            </p>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            ref={stayButtonRef}
            type="button"
            onClick={onStay}
            data-testid="graph-unsaved-stay"
            className="px-3 py-1.5 rounded-lg text-xs text-surface-300 hover:bg-surface-700 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-onto-400"
          >
            留在此页
          </button>
          <button
            type="button"
            onClick={onLeave}
            data-testid="graph-unsaved-leave"
            className="px-3 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-300"
          >
            放弃改动并离开
          </button>
        </div>
      </div>
    </div>
  );
}
