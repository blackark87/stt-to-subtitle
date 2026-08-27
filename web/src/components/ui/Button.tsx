"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "danger";

const base =
  "inline-flex items-center justify-center gap-1.5 rounded-[var(--radius-control)] border px-3 py-1.5 text-xs font-semibold whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-50";

const variants: Record<Variant, string> = {
  primary:
    "border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-ink)] hover:brightness-110",
  secondary:
    "border-[var(--color-line)] bg-[var(--color-surface)] text-[var(--color-text)] hover:bg-[var(--color-sunken)]",
  danger:
    "border-[color-mix(in_srgb,var(--color-st-failed)_45%,var(--color-line))] bg-transparent text-[var(--color-st-failed)] hover:bg-[color-mix(in_srgb,var(--color-st-failed)_10%,var(--color-surface))]",
};

export function Button({
  variant = "secondary",
  children,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  children: ReactNode;
}) {
  return (
    <button type="button" className={`${base} ${variants[variant]} ${className}`} {...rest}>
      {children}
    </button>
  );
}
