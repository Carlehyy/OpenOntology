/**
 * Vendored from beUI (github.com/starc007/ui-components @ afba7fa055dd, MIT © 2026 Saurabh Chauhan).
 * 平台适配：仅导入路径（ease/cn 随 motion-ui 内聚）。
 */

import { animate, useInView, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { EASE_OUT } from "./ease";
import { cn } from "@/lib/utils";

export interface AnimatedNumberProps {
  value: number;
  duration?: number;
  format?: (n: number) => string;
  className?: string;
  startOnView?: boolean;
}

export function AnimatedNumber({
  value,
  duration = 1.2,
  format = (n) => Math.round(n).toLocaleString(),
  className,
  startOnView = true,
}: AnimatedNumberProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, amount: 0.6 });
  const reduce = useReducedMotion();
  // 首帧即真实值：KPI 数字可信优先，禁止先显示 0 再滚动（会被读成假数据）。
  const [display, setDisplay] = useState(value);
  const fromRef = useRef(value);
  // 数值变化时的补间上限 200ms：只做轻微过渡，不做长滚动动画。
  const DURATION_CAP = 0.2;

  useEffect(() => {
    if (startOnView && !inView) {
      fromRef.current = value;
      setDisplay(value);
      return;
    }
    if (reduce) {
      fromRef.current = value;
      setDisplay(value);
      return;
    }
    const controls = animate(fromRef.current, value, {
      duration: Math.min(duration, DURATION_CAP),
      ease: EASE_OUT,
      onUpdate: (v) => setDisplay(v),
    });
    fromRef.current = value;
    return () => controls.stop();
  }, [value, duration, inView, startOnView, reduce]);

  return (
    <span ref={ref} className={cn("tabular-nums", className)}>
      {format(display)}
    </span>
  );
}
