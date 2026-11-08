import { useState, type FormEvent } from "react";
import type { ExperimentRun, Lease, ParameterSchema, StreamDescriptor, Workbench } from "./api";
import { copy } from "./copy";
import { formatClock, secondsUntil, useNow } from "./useLab";

// --------------------------------------------------------------------------- Welcome

export function Welcome({ onSubmit }: { onSubmit: (name: string) => Promise<void> }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await onSubmit(name.trim());
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="welcome">
      <Logo />
      <h1>{copy.welcome.heading}</h1>
      <p className="muted">{copy.welcome.body}</p>
      <form onSubmit={submit} className="card stack">
        <label htmlFor="name" className="label">
          {copy.welcome.nameLabel}
        </label>
        <input
          id="name"
          value={name}
          maxLength={40}
          autoComplete="nickname"
          placeholder={copy.welcome.namePlaceholder}
          onChange={(e) => setName(e.target.value)}
        />
        <button className="primary" disabled={busy || !name.trim()}>
          {copy.welcome.continue}
        </button>
      </form>
    </main>
  );
}

export function Logo() {
  return (
    <div className="logo">
      <svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true">
        <rect width="32" height="32" rx="8" fill="var(--surface-2)" />
        <circle cx="16" cy="16" r="9" fill="none" stroke="var(--accent)" strokeWidth="2" />
        <path
          d="M9 16c2-3 4-3 7 0s5 3 7 0"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
      <span>{copy.appName}</span>
    </div>
  );
}

// --------------------------------------------------------------------------- Stream

export function LiveStream({
  url,
  stream,
  workbench,
}: {
  url: string;
  stream: StreamDescriptor | null;
  workbench: Workbench | null;
}) {
  // Bumping the key remounts the <img>, which reconnects a dropped stream. The <img>
  // is also unmounted while the bench is offline, so it reconnects when it returns.
  const [attempt, setAttempt] = useState(0);
  const online = workbench?.status === "online";

  const ratio = stream?.width && stream?.height ? `${stream.width} / ${stream.height}` : "16 / 9";
  return (
    <div className="stream" style={{ aspectRatio: ratio }}>
      <div className="stream-placeholder">
        {online ? copy.stream.connecting : copy.stream.offline}
      </div>
      {online && (
        <img
          key={attempt}
          src={`${url}${url.includes("?") ? "&" : "?"}attempt=${attempt}`}
          alt=""
          onError={() => window.setTimeout(() => setAttempt((a) => a + 1), 2000)}
        />
      )}
      <div className="stream-badges">
        {online && (
          <span className="badge live">
            <span className="dot" /> {copy.stream.live}
          </span>
        )}
        {workbench?.resetting && <span className="badge">{copy.stream.resetting}</span>}
        {workbench?.current_holder && (
          <span className="badge">{copy.stream.inUseBy(workbench.current_holder)}</span>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- Lease

interface LeasePanelProps {
  workbench: Workbench | null;
  lease: Lease | null;
  onJoin: () => void;
  onAccept: () => void;
  onRelease: () => void;
  busy: boolean;
}

export function LeasePanel({
  workbench,
  lease,
  onJoin,
  onAccept,
  onRelease,
  busy,
}: LeasePanelProps) {
  const now = useNow();

  if (workbench && workbench.status !== "online" && lease?.state !== "queued") {
    const text = workbench.status === "maintenance" ? copy.bench.maintenance : copy.bench.offline;
    return (
      <section className="card">
        <p className="muted">{text}</p>
      </section>
    );
  }

  switch (lease?.state) {
    case "queued":
      return (
        <section className="card stack">
          <div className="row">
            <div className="queue-pos">{copy.lease.position(lease.queue_position ?? 0)}</div>
            <div className="heading grow">
              <h2>{copy.lease.queued}</h2>
              <p className="muted small">{copy.lease.ofTotal(lease.queue_length)}</p>
            </div>
          </div>
          <p className="muted small">{copy.lease.queuedHint}</p>
          <button className="ghost" onClick={onRelease} disabled={busy}>
            {copy.lease.leaveQueue}
          </button>
        </section>
      );
    case "offered":
      return (
        <section className="card highlight stack">
          <div className="heading">
            <h2>{copy.lease.offered}</h2>
            <p className="muted small">
              {copy.lease.offeredHint(secondsUntil(lease.offer_expires_at, now))}
            </p>
          </div>
          <div className="row">
            <button className="primary grow" onClick={onAccept} disabled={busy}>
              {copy.lease.start}
            </button>
            <button className="ghost" onClick={onRelease} disabled={busy}>
              {copy.lease.decline}
            </button>
          </div>
        </section>
      );
    case "active":
      return null; // The controls card takes over; see Controls.
    default: {
      const waiting = workbench?.queue_length ?? 0;
      const free = workbench?.available ?? false;
      const ended =
        lease && lease.state in copy.lease.ended
          ? copy.lease.ended[lease.state as keyof typeof copy.lease.ended]
          : null;
      return (
        <section className="card stack">
          {ended && <p className="muted small">{ended}</p>}
          <button className="primary" onClick={onJoin} disabled={busy || !workbench}>
            {ended ? copy.lease.again : copy.lease.joinQueue}
          </button>
          <p className="muted small center">
            {free ? copy.lease.benchFree : copy.lease.waiting(waiting)}
          </p>
        </section>
      );
    }
  }
}

// --------------------------------------------------------------------------- Controls

interface ControlsProps {
  lease: Lease;
  parameters: Record<string, ParameterSchema>;
  values: Record<string, number | string>;
  onChange: (name: string, value: number | string) => void;
  run: ExperimentRun | null;
  onRun: () => void;
  onStop: () => void;
  onRelease: () => void;
  busy: boolean;
}

export function Controls(props: ControlsProps) {
  const { lease, parameters, values, onChange, run, onRun, onStop, onRelease, busy } = props;
  const now = useNow();
  const remaining = secondsUntil(lease.expires_at, now);
  const running = run !== null && (run.state === "pending" || run.state === "running");

  return (
    <section className="card stack">
      <div className="row">
        <h2 className="grow">{copy.lease.active}</h2>
        <span className={`timer ${remaining <= 30 ? "warn" : ""}`}>
          {copy.lease.remaining(formatClock(remaining))}
        </span>
      </div>

      {Object.entries(parameters).map(([name, schema]) => (
        <ParameterControl
          key={name}
          name={name}
          schema={schema}
          value={values[name] ?? schema.default ?? ""}
          disabled={running}
          onChange={(v) => onChange(name, v)}
        />
      ))}

      <RunStatus run={run} durationS={Number(values.duration_s) || null} now={now} />

      {running ? (
        <button className="danger" onClick={onStop} disabled={busy}>
          {copy.controls.stop}
        </button>
      ) : (
        <button className="primary" onClick={onRun} disabled={busy}>
          {copy.controls.run}
        </button>
      )}
      <button className="ghost" onClick={onRelease} disabled={busy}>
        {copy.lease.release}
      </button>
    </section>
  );
}

function ParameterControl({
  name,
  schema,
  value,
  disabled,
  onChange,
}: {
  name: string;
  schema: ParameterSchema;
  value: number | string;
  disabled: boolean;
  onChange: (value: number | string) => void;
}) {
  const id = `param-${name}`;
  const label = schema.title ?? name;
  if (schema.enum) {
    return (
      <div className="field">
        <label htmlFor={id} className="label">
          {label}
        </label>
        <select
          id={id}
          value={String(value)}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          {schema.enum.map((option) => (
            <option key={option}>{option}</option>
          ))}
        </select>
      </div>
    );
  }
  const step = schema["x-step"] ?? (schema.type === "integer" ? 1 : 0.01);
  const unit = schema["x-unit"] ? ` ${schema["x-unit"]}` : "";
  const decimals = step < 1 ? Math.min(3, String(step).split(".")[1]?.length ?? 2) : 0;
  return (
    <div className="field">
      <div className="row">
        <label htmlFor={id} className="label grow">
          {label}
        </label>
        <output htmlFor={id} className="value">
          {Number(value).toFixed(decimals)}
          {unit}
        </output>
      </div>
      <input
        id={id}
        type="range"
        min={schema.minimum}
        max={schema.maximum}
        step={step}
        value={Number(value)}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {schema.description && <p className="muted small">{schema.description}</p>}
    </div>
  );
}

function RunStatus({
  run,
  durationS,
  now,
}: {
  run: ExperimentRun | null;
  durationS: number | null;
  now: number;
}) {
  if (!run) return null;
  const step = run.runs.at(-1);
  if (run.state === "pending" || run.state === "running") {
    const started = step?.started_at ? Date.parse(step.started_at) : null;
    const total = Number(run.parameter_values.duration_s) || durationS || 0;
    const progress = started && total ? Math.min(1, (now - started) / (total * 1000)) : 0;
    return (
      <div className="run-status">
        <div className="row small">
          <span className="grow">{copy.controls.running}</span>
          {started && total ? (
            <span className="muted">{formatClock(Math.ceil(total * (1 - progress)))}</span>
          ) : null}
        </div>
        <div className="progress">
          <div style={{ width: `${progress * 100}%` }} />
        </div>
      </div>
    );
  }
  const label =
    run.state === "succeeded"
      ? copy.controls.succeeded
      : run.state === "cancelled"
        ? copy.controls.cancelled
        : copy.controls.failed;
  return (
    <p className={`run-result small ${run.state}`}>
      {label}
      {run.state === "failed" && run.error ? `: ${run.error.message}` : ""}
    </p>
  );
}
