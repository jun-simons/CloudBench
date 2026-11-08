import { useCallback, useEffect, useState } from "react";
import { usePersistentState } from "./useLab";

/** Whether a CSS media query currently matches; updates when it changes. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

export type Theme = "light" | "dark";

export const THEME_KEY = "cloudbench.theme";

/**
 * The color theme: the viewer's own choice if they made one, else the system's.
 * Sets `data-theme` on <html>; index.html does the same before first paint.
 */
export function useTheme(): [Theme, () => void] {
  const systemLight = useMediaQuery("(prefers-color-scheme: light)");
  const [chosen, setChosen] = usePersistentState<Theme | null>(THEME_KEY, null);
  const theme: Theme = chosen ?? (systemLight ? "light" : "dark");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    const background = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", background);
  }, [theme]);

  const toggle = useCallback(
    () => setChosen(theme === "dark" ? "light" : "dark"),
    [theme, setChosen],
  );
  return [theme, toggle];
}
