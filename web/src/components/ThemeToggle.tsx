"use client";

import { useEffect } from "react";

type Theme = "light" | "dark";

function preferredTheme(): Theme {
  const saved = window.localStorage.getItem("stt-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeToggle() {
  useEffect(() => {
    document.documentElement.dataset.theme = preferredTheme();
  }, []);

  const toggle = () => {
    const current = document.documentElement.dataset.theme || preferredTheme();
    const next: Theme = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    window.localStorage.setItem("stt-theme", next);
  };

  return (
    <button type="button" className="btn sec sm theme-toggle" onClick={toggle}>
      화면 테마 전환
    </button>
  );
}
