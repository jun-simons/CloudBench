import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import { copy } from "./copy";
import { CameraCard, GuidePane, LeasePanel, VibratorCard } from "./panes";
import { Logo, Welcome } from "./ui";
import { useLab } from "./useLab";
import { useLiveControl } from "./useLiveControl";

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
  return <Lab name={session.principal.display_name} />;
}

function Lab({ name }: { name: string }) {
  const lab = useLab();
  const { experiment, workbench, lease } = lab;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const control = useLiveControl(lab, setError);

  // The stream to show comes from the experiment's first "show" step, via its role binding.
  // If the bench does not (yet) match the experiment, e.g. its agent is an older version,
  // fall back to the bench's first stream: the video is still worth watching.
  const stream = useMemo(() => {
    if (!experiment || !workbench) return null;
    const show = experiment.procedure.find((s) => "show" in s);
    const bindings = experiment.compatible_workbenches.find(
      (c) => c.workbench_id === workbench.id,
    )?.bindings;
    const [role, streamName] = show && "show" in show ? show.show.split(".") : [];
    const bound = workbench.tools.find((t) => t.name === (role ? bindings?.[role] : undefined));
    const tool = bound ?? workbench.tools.find((t) => t.streams.length > 0);
    const descriptor = tool?.streams.find((s) => s.name === streamName) ?? tool?.streams[0] ?? null;
    if (!tool || !descriptor) return null;
    return {
      descriptor,
      url: api.authedUrl(
        `/v1/workbenches/${workbench.id}/tools/${tool.name}/streams/${descriptor.name}`,
      ),
    };
  }, [experiment, workbench]);

  const act = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : copy.error);
    } finally {
      setBusy(false);
    }
  }, []);

  const join = () =>
    act(async () => {
      if (workbench) lab.patch({ lease: await api.requestLease(workbench.id) });
    });
  const accept = () =>
    act(async () => lease && lab.patch({ lease: await api.acceptLease(lease.id) }));
  const release = () =>
    act(async () => lease && lab.patch({ lease: await api.releaseLease(lease.id) }));

  const shownError = error ?? lab.error;

  return (
    <div className="app">
      <header className="topbar">
        <Logo />
        <div className="topbar-user">
          <span className="muted small">{name}</span>
          <span
            className={`conn ${lab.connected ? "ok" : ""}`}
            title={lab.connected ? "Connected" : "Reconnecting"}
          />
        </div>
      </header>

      <main className="layout">
        {experiment && (
          <div className="page-title heading">
            <h1>{experiment.title}</h1>
            {experiment.summary && <p className="muted">{experiment.summary}</p>}
          </div>
        )}

        <div className="pane pane-controls">
          <LeasePanel
            workbench={workbench}
            lease={lease}
            onJoin={join}
            onAccept={accept}
            onRelease={release}
            busy={busy}
          />
          <CameraCard
            url={stream?.url ?? null}
            stream={stream?.descriptor ?? null}
            workbench={workbench}
          />
          {experiment && <VibratorCard experiment={experiment} control={control} />}
        </div>

        <div className="pane pane-guide">
          {experiment && <GuidePane experiment={experiment} control={control} />}
        </div>
      </main>

      {shownError && (
        <div className="toast" role="alert">
          <span className="grow">{shownError}</span>
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
