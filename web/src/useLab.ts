import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Experiment, type Lease, type Run, type Workbench } from "./api";

export interface LabState {
  /** Every experiment, for the experiment menu; empty until loaded. */
  experiments: Experiment[];
  /** The experiment whose slug is in the address, or null if none matches. */
  experiment: Experiment | null;
  workbench: Workbench | null;
  lease: Lease | null;
  /** The user's live tone run (the experiment's live.start action), while it lasts. */
  liveRun: Run | null;
  connected: boolean;
  error: string | null;
}

const TERMINAL_LEASE = new Set(["released", "expired", "revoked"]);
const TERMINAL_RUN = new Set(["succeeded", "failed", "cancelled"]);

export const isRunActive = (run: Run | null): run is Run =>
  run !== null && !TERMINAL_RUN.has(run.state);

/**
 * Loads the experiment with the given slug, its workbench, the user's lease and live
 * tone, and keeps them fresh from the gateway's event stream. On every (re)connect it
 * refetches everything, so events missed while the phone slept do no harm.
 */
export function useLab(slug: string | null) {
  const [state, setState] = useState<LabState>({
    experiments: [],
    experiment: null,
    workbench: null,
    lease: null,
    liveRun: null,
    connected: false,
    error: null,
  });
  const workbenchId = useRef<string | null>(null);
  const liveAction = useRef<string | null>(null);
  // The slug refresh() loads; updated (and reloaded) by the effect below.
  const slugRef = useRef(slug);

  const patch = useCallback((update: Partial<LabState>) => {
    setState((s) => ({ ...s, ...update }));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const experiments = await api.experiments();
      const experiment = experiments.find((e) => e.slug === slugRef.current) ?? null;
      const wbId =
        experiment?.compatible_workbenches[0]?.workbench_id ?? workbenchId.current ?? "wb_faraday";
      workbenchId.current = wbId;
      liveAction.current = experiment?.live?.start ?? null;
      const [workbench, leases, runs] = await Promise.all([
        api.workbench(wbId).catch(() => null),
        api.myOpenLeases(),
        api.myRuns(),
      ]);
      const lease = leases.find((l) => l.workbench_id === wbId) ?? null;
      const liveRun =
        runs.find(
          (r) => r.lease_id === lease?.id && r.action === liveAction.current && isRunActive(r),
        ) ?? null;
      setState((s) => ({
        ...s,
        experiments,
        experiment,
        workbench,
        // Keep showing a lease that just ended so the user sees why.
        lease: lease ?? (s.lease && TERMINAL_LEASE.has(s.lease.state) ? s.lease : null),
        liveRun,
        error: null,
      }));
    } catch (e) {
      patch({ error: e instanceof Error ? e.message : String(e) });
    }
  }, [patch]);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: number | undefined;

    const connect = () => {
      source = new EventSource(api.authedUrl("/v1/events"));
      source.onopen = () => {
        patch({ connected: true });
        void refresh();
      };
      source.onerror = () => {
        patch({ connected: false });
        // EventSource retries by itself unless the server closed it for good.
        if (source?.readyState === EventSource.CLOSED) {
          source.close();
          retry = window.setTimeout(connect, 2000);
        }
      };
      source.addEventListener("workbench.updated", (e) => {
        const wb = JSON.parse((e as MessageEvent).data) as Workbench;
        if (wb.id === workbenchId.current) patch({ workbench: wb });
      });
      source.addEventListener("lease.updated", (e) => {
        const lease = JSON.parse((e as MessageEvent).data) as Lease;
        if (lease.workbench_id === workbenchId.current) patch({ lease });
      });
      source.addEventListener("run.updated", (e) => {
        const run = JSON.parse((e as MessageEvent).data) as Run;
        if (run.action !== liveAction.current) return;
        setState((s) => {
          // Track the newest live run; ignore stale updates for older ones.
          if (s.liveRun && s.liveRun.id !== run.id && run.created_at < s.liveRun.created_at) {
            return s;
          }
          return { ...s, liveRun: run };
        });
      });
    };

    connect();
    // Phones suspend pages; refetch when the user comes back to the tab.
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      source?.close();
      window.clearTimeout(retry);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [patch, refresh]);

  // Another experiment was picked: load it (the event stream stays connected).
  useEffect(() => {
    if (slugRef.current === slug) return;
    slugRef.current = slug;
    void refresh();
  }, [slug, refresh]);

  return { ...state, patch, refresh };
}

/** Current time, re-rendering every `intervalMs` (for countdowns). */
export function useNow(intervalMs = 250): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function secondsUntil(iso: string | null, now: number): number {
  if (!iso) return 0;
  return Math.max(0, Math.ceil((Date.parse(iso) - now) / 1000));
}

export function formatClock(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Persisted UI preference (e.g. a collapsed card); falls back to memory only. */
export function usePersistentState<T>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (next: T) => {
      setValue(next);
      try {
        localStorage.setItem(key, JSON.stringify(next));
      } catch {
        // Storage unavailable: keep the value for this page only.
      }
    },
    [key],
  );
  return [value, set];
}
