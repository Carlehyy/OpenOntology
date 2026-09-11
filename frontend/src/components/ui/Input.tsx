import * as React from "react"
import { cn } from "@/lib/utils"

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  error?: string
  label?: string
}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, error, label, id, ...props }, ref) => {
    const generatedId = React.useId()
    const inputId = id || (label ? generatedId : undefined)

    return (
      <div className="w-full">
        {label && (
          <div className="mb-1.5 flex text-sm font-medium text-[var(--color-text-primary)]">
            <label htmlFor={inputId}>{label}</label>
            {props.required && <span aria-hidden="true" className="text-[var(--color-danger)] ml-0.5">*</span>}
          </div>
        )}
        <input
          id={inputId}
          type={type}
          className={cn(
            "flex h-9 w-full rounded-md border bg-[var(--color-bg-elevated)] px-3 py-2 text-sm shadow-sm transition-colors",
            "placeholder:text-[var(--color-text-tertiary)]",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring",
            "disabled:cursor-not-allowed disabled:opacity-50 disabled:bg-[var(--color-bg-hover)]",
            error
              ? "border-[var(--color-danger)] focus-visible:border-[var(--color-danger)]"
              : "border-[var(--color-border)]",
            className
          )}
          ref={ref}
          {...props}
        />
        {error && (
          <p className="mt-1 text-xs text-[var(--color-danger)]">{error}</p>
        )}
      </div>
    )
  }
)
Input.displayName = "Input"

export { Input }
