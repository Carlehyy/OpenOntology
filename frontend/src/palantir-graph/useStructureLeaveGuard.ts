import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Chromium Navigation API 的最小结构。TS 内置 DOM lib 尚未收录该实验 API，
 * 这里只声明用到的字段，避免全局类型污染或依赖 @types 补丁。
 */
interface NavigateEventLike {
  navigationType: 'push' | 'replace' | 'reload' | 'traverse';
  canIntercept: boolean;
  destination: { url: string };
  preventDefault: () => void;
}

interface NavigationLike {
  addEventListener: (type: 'navigate', listener: (event: NavigateEventLike) => void) => void;
  removeEventListener: (type: 'navigate', listener: (event: NavigateEventLike) => void) => void;
}

export interface StructureLeaveGuard {
  /** 包装一次显式离开动作：结构未保存时先弹确认框，否则直接执行 */
  requestLeave: (run: () => void) => void;
  /** 待确认的离开动作；null 表示当前没有离开拦截 */
  pendingLeave: { run: () => void } | null;
  /** 用户确认放弃未保存改动并离开 */
  confirmLeave: () => void;
  /** 用户选择留在此页 */
  cancelLeave: () => void;
}

/**
 * 图谱编辑器「结构未保存离开」守卫。
 *
 * 只拦截三类离开：画布左列返回、左上角本体图标（与返回共用 requestLeave）、
 * 以及浏览器前进/后退。节点位置不属于结构改动（由布局接口独立自动保存），
 * 因此只有模型结构 isDirty 时才拦。
 *
 * 浏览器前进/后退走 Chromium Navigation API 的 same-document navigate 事件：
 * 在事件里 preventDefault() 可以取消 hash 路由的历史穿越，页面不会卸载、
 * 画布状态不闪断；确认离开时再放行一次。Firefox/Safari 无该 API 时降级为
 * 不拦截 SPA 返回（刷新/关闭仍由 GraphWorkspace 的 beforeunload 兜底）。
 *
 * 另需注意：显式离开动作用 history/navigate(-1) 触发时同样会产生 traverse
 * 事件，因此放行计数（bypass）必须先加一，否则会把自己拦下来。
 */
export function useStructureLeaveGuard(enabled: boolean, dirty: boolean): StructureLeaveGuard {
  const dirtyRef = useRef(dirty);
  const bypassRef = useRef(0);
  const bypassResetTimer = useRef<number | null>(null);
  const [pending, setPending] = useState<{ run: () => void } | null>(null);
  const pendingRef = useRef<{ run: () => void } | null>(null);

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => () => {
    if (bypassResetTimer.current !== null) window.clearTimeout(bypassResetTimer.current);
  }, []);

  /** 放行下一次历史穿越；目标条目不存在时会留下计数，定时清零避免守卫被永久绕过 */
  const releaseNextTraverse = useCallback(() => {
    bypassRef.current += 1;
    if (bypassResetTimer.current !== null) window.clearTimeout(bypassResetTimer.current);
    bypassResetTimer.current = window.setTimeout(() => {
      bypassRef.current = 0;
      bypassResetTimer.current = null;
    }, 1000);
  }, []);

  const arm = useCallback((run: () => void) => {
    const entry = { run };
    pendingRef.current = entry;
    setPending(entry);
  }, []);

  const cancelLeave = useCallback(() => {
    pendingRef.current = null;
    setPending(null);
  }, []);

  const confirmLeave = useCallback(() => {
    const entry = pendingRef.current;
    pendingRef.current = null;
    setPending(null);
    entry?.run();
  }, []);

  const requestLeave = useCallback((run: () => void) => {
    // 显式离开动作同样可能触发 history 穿越（navigate(-1)），确认后执行前必须
    // 放行一次，否则会被本守卫再次拦下、弹窗反复弹出。
    const execute = () => {
      releaseNextTraverse();
      run();
    };
    if (enabled && dirtyRef.current) {
      arm(execute);
      return;
    }
    execute();
  }, [enabled, arm, releaseNextTraverse]);

  // 浏览器前进/后退拦截：仅在可编辑（草稿）模式下生效
  useEffect(() => {
    if (!enabled) return;
    const nav = (window as unknown as { navigation?: NavigationLike }).navigation;
    if (!nav || typeof nav.addEventListener !== 'function') return;
    const onNavigate = (event: NavigateEventLike) => {
      if (event.navigationType !== 'traverse' || !event.canIntercept) return;
      if (bypassRef.current > 0) {
        bypassRef.current -= 1;
        return;
      }
      if (!dirtyRef.current) return;
      event.preventDefault();
      arm(() => {
        releaseNextTraverse();
        window.history.back();
      });
    };
    nav.addEventListener('navigate', onNavigate);
    return () => nav.removeEventListener('navigate', onNavigate);
  }, [arm, enabled, releaseNextTraverse]);

  // 守卫关闭或结构已保存（例如弹窗打开时 Ctrl+S 成功）时收起弹窗，避免过期拦截
  useEffect(() => {
    if (!enabled || !dirty) cancelLeave();
  }, [cancelLeave, dirty, enabled]);

  return { requestLeave, pendingLeave: pending, confirmLeave, cancelLeave };
}
