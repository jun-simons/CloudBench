import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type ParameterSchema } from "./api";
import { Controls, LeasePanel, LiveStream, Logo, Welcome } from "./components";
import { copy } from "./copy";
import { useLab } from "./useLab";

function defaults(parameters: Record<string, ParameterSchema>): Record<string, number | string> {
  return Object.fromEntries(
    Object.entries(parameters).map(([name, schema]) => [
      name,
      schema.default ?? schema.minimum ?? 0,
    ]),
  );
}

export default function App() {
  const [session, setSession] = useState(api.session);
  useEffect(() => {
    api.onSessionLost = () => setSession(null);
  }, []);

  if (!session) {
    return (
      <Welcome
        onSubmit={async (name) => {
          setSession(await api.createSession(name));
        }}
      />
    );
  }
  return <Lab />;
}

function Lab() {
  const lab = useLab(true);
  const { experiment, workbench, lease, experimentRun } = lab;
  // Only the values the user changed; the rest come from the experiment's defaults.
  const [overrides, setOverrides] = useState<Record<string, number | string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const values = useMemo(
    () => ({ ...(experiment ? defaults(experiment.parameters) : {}), ...overrides }),
    [experiment, overrides],
  );

  // The stream to show comes from the experiment's first "show" step, via its role binding.
  const stream = useMemo(() => {
    if (!experiment || !workbench) return null;
    const show = experiment.procedure.find((s) => "show" in s);
    const bindings = experiment.compatible_workbenches.find(
      (c) => c.workbench_id === workbench.id,
    )?.bindings;
    if (!show || !("show" in show) || !bindings) return null;
    const [role, streamName] = show.show.split(".");
    const toolName = role ? bindings[role] : undefined;
    const tool = workbench.tools.find((t) => t.name === toolName);
    const descriptor = tool?.streams.find((s) => s.name === streamName) ?? null;
    if (!tool || !descriptor) return null;
    return {
      descriptor,
      url: api.authedUrl(
        `/v1/workbenches/${workbench.id}/tools/${tool.name}/streams/${descriptor.name}`,
      ),
    };
  }, [experiment, workbench]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : copy.error);
    } finally {
      setBusy(false);
    }
  };

  const join = () =>
    act(async () => {
      if (workbench) lab.setLease(await api.requestLease(workbench.id));
    });
  const accept = () => act(async () => lease && lab.setLease(await api.acceptLease(lease.id)));
  const release = () => act(async () => lease && lab.setLease(await api.releaseLease(lease.id)));
  const runExperiment = () =>
    act(async () => {
      if (!experiment || !lease) return;
      lab.patch({ experimentRun: await api.startExperiment(experiment.id, lease.id, values) });
    });
  const stop = () =>
    act(async () => {
      if (experimentRun)
        lab.patch({ experimentRun: await api.cancelExperimentRun(experimentRun.id) });
    });

  return (
    <div className="app">
      <header className="topbar">
        <Logo />
        <span
          className={`conn ${lab.connected ? "ok" : ""}`}
          title={lab.connected ? "Connected" : "Reconnecting"}
        />
      </header>

      <main className="content stack">
        {experiment && (
          <div className="heading">
            <h1>{experiment.title}</h1>
            {experiment.summary && <p className="muted">{experiment.summary}</p>}
          </div>
        )}

        <LiveStream
          url={stream?.url ?? ""}
          stream={stream?.descriptor ?? null}
          workbench={workbench}
        />

        {lease?.state === "active" && experiment ? (
          <Controls
            lease={lease}
            parameters={experiment.parameters}
            values={values}
            onChange={(name, value) => setOverrides((v) => ({ ...v, [name]: value }))}
            run={experimentRun}
            onRun={runExperiment}
            onStop={stop}
            onRelease={release}
            busy={busy}
          />
        ) : (
          <LeasePanel
            workbench={workbench}
            lease={lease}
            onJoin={join}
            onAccept={accept}
            onRelease={release}
            busy={busy}
          />
        )}

        {experiment?.notes && (
          <section className="notes">
            <h2>{copy.notesHeading}</h2>
            <p className="muted pre">{experiment.notes}</p>
          </section>
        )}
      </main>

      {(error ?? lab.error) && (
        <div className="toast" role="alert">
          <span className="grow">{error ?? lab.error}</span>
          <button
            className="link"
            onClick={() => {
              setError(null);
              lab.patch({ error: null });
            }}
          >
            {copy.dismiss}
          </button>
        </div>
      )}
    </div>
  );
}
