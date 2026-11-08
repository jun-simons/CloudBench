import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import { copy } from "./copy";
import {
  CameraCard,
  GuidePane,
  LeaseControl,
  StreamGrid,
  VibratorCard,
  type StreamSource,
} from "./panes";
import { ExperimentMenu, Logo, ThemeToggle, Welcome } from "./ui";
import { useMediaQuery, useTheme } from "./useDisplay";
import { useLab, type LabState } from "./useLab";
import { useLiveControl } from "./useLiveControl";
import { experimentPath, useRoute } from "./useRoute";

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
  const { slug, navigate } = useRoute();
  const lab = useLab(slug);
  const { experiments, experiment, workbench, lease } = lab;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const control = useLiveControl(lab, setError);

  const streams = useStreams(lab);
  const desktop = useMediaQuery("(min-width: 960px)");
  const wide = useMediaQuery("(min-width: 1280px)");
  const [theme, toggleTheme] = useTheme();

  // "/" and unknown addresses go to the first experiment; there is no list page yet.
  const first = experiments[0];
  useEffect(() => {
    if (first && !experiment) navigate(experimentPath(first.slug), { replace: true });
  }, [first, experiment, navigate]);

  useEffect(() => {
    document.title = experiment ? `${experiment.title} · ${copy.appName}` : copy.appName;
  }, [experiment]);

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

  const user = (
    <div className="topbar-user">
      <ThemeToggle theme={theme} onToggle={toggleTheme} />
      <span className="muted small">{name}</span>
      <span
        className={`conn ${lab.connected ? "ok" : ""}`}
        title={lab.connected ? "Connected" : "Reconnecting"}
      />
    </div>
  );
  const menu = (
    <ExperimentMenu
      experiments={experiments}
      current={experiment}
      onSelect={(e) => navigate(experimentPath(e.slug))}
    />
  );
  const leaseControl = (
    <LeaseControl
      workbench={workbench}
      lease={lease}
      onJoin={join}
      onAccept={accept}
      onRelease={release}
      busy={busy}
    />
  );
  const vibrator = experiment && <VibratorCard experiment={experiment} control={control} />;
  const guide = experiment && <GuidePane experiment={experiment} control={control} />;
  const toast = shownError && (
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
  );

  if (desktop) {
    // A workstation layout: controls on the left, every stream in the middle, the guide
    // on the right (under the controls on narrower screens). Each column scrolls alone.
    return (
      <div className="app desktop">
        <header className="topbar">
          <Logo />
          <div className="topbar-title">
            <h1>{menu}</h1>
            {experiment?.summary && <span className="muted">{experiment.summary}</span>}
          </div>
          {leaseControl}
          {user}
        </header>
        <main className={`workspace ${wide ? "wide" : ""}`}>
          <aside className="column column-side">
            {vibrator}
            {!wide && guide}
          </aside>
          <section className="column-streams">
            <StreamGrid streams={streams} workbench={workbench} />
          </section>
          {wide && <aside className="column column-guide">{guide}</aside>}
        </main>
        {toast}
      </div>
    );
  }

  const [primary = null, ...others] = streams;
  return (
    <div className="app">
      <header className="topbar">
        <Logo />
        {user}
      </header>

      <main className="layout">
        {experiment && (
          <div className="page-title heading">
            <h1>{menu}</h1>
            {experiment.summary && <p className="muted">{experiment.summary}</p>}
          </div>
        )}

        <div className="pane pane-controls">
          {leaseControl}
          <CameraCard source={primary} workbench={workbench} />
          {vibrator}
          {/* Extra outputs (e.g. computer vision overlays) come after the controls, collapsed,
              so the camera and vibrator stay together on a phone. */}
          {others.map((source) => (
            <CameraCard key={source.key} source={source} workbench={workbench} />
          ))}
        </div>

        <div className="pane pane-guide">{guide}</div>
      </main>

      {toast}
    </div>
  );
}

/** Title for a stream that is not the experiment's main view, from its name: "edge_map" → "Edge map". */
function streamTitle(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Every stream on the bench, the experiment's main view first. The main view comes from
 * the experiment's first "show" step, via its role binding. If the bench does not (yet)
 * match the experiment, e.g. its agent is an older version, the bench's first stream is
 * the main view: the video is still worth watching.
 */
function useStreams({ experiment, workbench }: LabState): StreamSource[] {
  return useMemo(() => {
    if (!workbench) return [];
    const show = experiment?.procedure.find((s) => "show" in s);
    const bindings = experiment?.compatible_workbenches.find(
      (c) => c.workbench_id === workbench.id,
    )?.bindings;
    const [role, streamName] = show && "show" in show ? show.show.split(".") : [];
    const bound = workbench.tools.find((t) => t.name === (role ? bindings?.[role] : undefined));
    const mainTool = bound ?? workbench.tools.find((t) => t.streams.length > 0);
    const mainStream = mainTool?.streams.find((s) => s.name === streamName) ?? mainTool?.streams[0];

    const all = workbench.tools.flatMap((tool) =>
      tool.streams.map((descriptor) => {
        const primary = tool === mainTool && descriptor === mainStream;
        return {
          key: `${tool.name}/${descriptor.name}`,
          title: primary ? copy.camera.title : streamTitle(descriptor.name),
          descriptor,
          url: api.authedUrl(
            `/v1/workbenches/${workbench.id}/tools/${tool.name}/streams/${descriptor.name}`,
          ),
          primary,
        };
      }),
    );
    return [...all.filter((s) => s.primary), ...all.filter((s) => !s.primary)];
  }, [experiment, workbench]);
}
