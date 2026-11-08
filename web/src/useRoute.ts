import { useCallback, useEffect, useState } from "react";

/** The app's addresses: /experiments/<slug>; anything else has no experiment. */
export function experimentPath(slug: string): string {
  return `/experiments/${encodeURIComponent(slug)}`;
}

function slugOf(pathname: string): string | null {
  const match = /^\/experiments\/([^/]+)\/?$/.exec(pathname);
  return match?.[1] ? decodeURIComponent(match[1]) : null;
}

/**
 * The experiment slug in the address bar, and a way to change it without reloading.
 * Back and forward work as usual.
 */
export function useRoute(): {
  slug: string | null;
  navigate: (path: string, options?: { replace?: boolean }) => void;
} {
  const [pathname, setPathname] = useState(() => window.location.pathname);

  useEffect(() => {
    const onPop = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((path: string, options?: { replace?: boolean }) => {
    if (path === window.location.pathname) return;
    if (options?.replace) window.history.replaceState(null, "", path);
    else window.history.pushState(null, "", path);
    setPathname(path);
  }, []);

  return { slug: slugOf(pathname), navigate };
}
