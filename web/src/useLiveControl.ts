import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type ParameterValues, type Preset } from "./api";
import { isRunActive, type LabState } from "./useLab";

/** Minimum time between live updates sent while the user drags a control. */
const UPDATE_INTERVAL_MS = 120;

function defaultsOf(experiment: LabState["experiment"]): ParameterValues {
  if (!experiment) return {};
  return Object.fromEntries(
    Object.entries(experiment.parameters).map(([name, schema]) => [
      name,
      schema.default ?? schema.minimum ?? schema.enum?.[0] ?? 0,
    ]),
  );
}

export function matchesPreset(values: ParameterValues, preset: Preset): boolean {
  return Object.entries(preset.values).every(([name, value]) => values[name] === value);
}

/**
 * State and commands for the experiment's live controls (see `live` in the
 * experiment file): parameter values, power on/off, presets, and streaming
 * changes to the playing tone.
 */
export function useLiveControl(lab: LabState, onError: (message: string) => void) {
  const { experiment, workbench, lease, liveRun } = lab;
  const live = experiment?.live ?? null;
  const tool = useMemo(() => {
    if (!live || !experiment || !workbench) return null;
    const binding = experiment.compatible_workbenches.find((c) => c.workbench_id === workbench.id);
    return binding?.bindings[live.role] ?? null;
  }, [experiment, workbench, live]);

  const [overrides, setOverrides] = useState<ParameterValues>({});
  const values = useMemo(
    () => ({ ...defaultsOf(experiment), ...overrides }),
    [experiment, overrides],
  );
  const liveValues = useMemo(
    () => Object.fromEntries((live?.parameters ?? []).map((name) => [name, values[name]])),
    [live, values],
  );

  const canControl =
    lease?.state === "active" && workbench?.status === "online" && tool !== null && live !== null;
  const on = isRunActive(liveRun);
  const [busy, setBusy] = useState(false);

  const call = useCallback(
    async (action: string, input: Record<string, unknown>) => {
      if (!workbench || !lease || !tool) throw new Error("No active lease");
      return api.runAction(workbench.id, tool, action, lease.id, input);
    },
    [workbench, lease, tool],
  );

  // While the tone plays, send changed values, at most every UPDATE_INTERVAL_MS.
  const sent = useRef({ key: "", at: 0 });
  const running = liveRun?.state === "running";
  const valuesKey = JSON.stringify(liveValues);
  useEffect(() => {
    if (!running || !live || valuesKey === sent.current.key) return;
    const delay = Math.max(0, sent.current.at + UPDATE_INTERVAL_MS - Date.now());
    const timer = window.setTimeout(() => {
      sent.current = { key: valuesKey, at: Date.now() };
      call(live.update, JSON.parse(valuesKey) as Record<string, unknown>).catch((e: unknown) => {
        // The tone may have just ended; anything else is worth showing.
        if (!(e instanceof ApiError && e.status === 409)) {
          onError(e instanceof Error ? e.message : String(e));
        }
      });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [running, live, valuesKey, call, onError]);

  const setOn = useCallback(
    async (next: boolean) => {
      if (!live) return;
      setBusy(true);
      try {
        if (next) {
          sent.current = { key: valuesKey, at: Date.now() };
          await call(live.start, JSON.parse(valuesKey) as Record<string, unknown>);
        } else {
          await call(live.stop, {});
        }
      } catch (e) {
        onError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [live, call, valuesKey, onError],
  );

  const setValue = useCallback((name: string, value: ParameterValues[string]) => {
    setOverrides((v) => ({ ...v, [name]: value }));
  }, []);

  const applyPreset = useCallback((preset: Preset) => {
    setOverrides((v) => ({ ...v, ...preset.values }));
  }, []);

  return { live, tool, values, canControl, on, busy, setOn, setValue, applyPreset };
}

export type LiveControlState = ReturnType<typeof useLiveControl>;
