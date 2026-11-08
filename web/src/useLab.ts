import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Experiment, type ExperimentRun, type Lease, type Workbench } from "./api";

export interface LabState {
  experiment: Experiment | null;
  workbench: Workbench | null;
  lease: Lease | null;
  experimentRun: ExperimentRun | null;
  connected: boolean;
  error: string | null;
}

const TERMINAL_LEASE = new Set(["released", "expired", "revoked"]);

/**
 * Loads the experiment, workbench, the user's lease and latest experiment run,
 * and keeps them fresh from the gateway's event stream. On every (re)connect it
 * refetches everything, so missed events while the phone slept do no harm.
 */
export function useLab(enabled: boolean) {
  const [state, setState] = useState<LabState>({
    experiment: null,
    workbench: null,
    lease: null,
    experimentRun: null,
    connected: false,
    error: null,
  });
  const workbenchId = useRef<string | null>(null);

  const patch = useCallback((update: Partial<LabState>) => {
    setState((s) => ({ ...s, ...update }));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const experiments = await api.experiments();
      const experiment = experiments[0] ?? null;
      const wbId =
        experiment?.compatible_workbenches[0]?.workbench_id ?? workbenchId.current ?? "wb_faraday";
      workbenchId.current = wbId;
      const [workbench, leases, runs] = await Promise.all([
        api.workbench(wbId).catch(() => null),
        api.myOpenLeases(),
        api.myExperimentRuns(),
      ]);
      const lease = leases.find((l) => l.workbench_id === wbId) ?? null;
      setState((s) => ({
        ...s,
        experiment,
        workbench,
        // Keep showing a lease that just ended so the user sees why.
        lease: lease ?? (s.lease && TERMINAL_LEASE.has(s.lease.state) ? s.lease : null),
        experimentRun: lease ? (runs.find((r) => r.lease_id === lease.id) ?? null) : null,
        error: null,
      }));
    } catch (e) {
      patch({ error: e instanceof Error ? e.message : String(e) });
    }
  }, [patch]);

  useEffect(() => {
    if (!enabled) return;
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
        if (lease.workbench_id !== workbenchId.current) return;
        setState((s) => ({
          ...s,
          lease,
          experimentRun: s.experimentRun?.lease_id === lease.id ? s.experimentRun : null,
        }));
      });
      source.addEventListener("experiment_run.updated", (e) => {
        const run = JSON.parse((e as MessageEvent).data) as ExperimentRun;
        setState((s) => (s.lease?.id === run.lease_id ? { ...s, experimentRun: run } : s));
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
  }, [enabled, patch, refresh]);

  return { ...state, setLease: (lease: Lease | null) => patch({ lease }), patch, refresh };
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
