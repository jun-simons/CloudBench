import { useState, type CSSProperties, type ReactNode } from "react";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  Experiment,
  Lease,
  ParameterSchema,
  ParameterValue,
  ParameterValues,
  Preset,
  StreamDescriptor,
  Workbench,
} from "./api";
import { copy } from "./copy";
import { CollapsibleCard, Segmented, Switch } from "./ui";
import { formatClock, secondsUntil, useNow } from "./useLab";
import { matchesPreset, type LiveControlState } from "./useLiveControl";

// --------------------------------------------------------------------------- formatting

export function formatValue(schema: ParameterSchema | undefined, value: ParameterValue): string {
  if (typeof value === "string") return copy.shapes[value as keyof typeof copy.shapes] ?? value;
  if (schema?.["x-format"] === "percent") return `${Math.round(value * 100)}%`;
  const unit = schema?.["x-unit"] ? ` ${schema["x-unit"]}` : "";
  // Up to the step's precision, without trailing zeros: 46 Hz, 46.5 Hz.
  return `${Number(value.toFixed(3))}${unit}`;
}

function presetSummary(experiment: Experiment, preset: Preset): string {
  return Object.entries(preset.values)
    .map(([name, value]) => formatValue(experiment.parameters[name], value))
    .join(" · ");
}

// --------------------------------------------------------------------------- lease

export function LeasePanel({
  workbench,
  lease,
  onJoin,
  onAccept,
  onRelease,
  busy,
}: {
  workbench: Workbench | null;
  lease: Lease | null;
  onJoin: () => void;
  onAccept: () => void;
  onRelease: () => void;
  busy: boolean;
}) {
  const now = useNow();

  if (workbench && workbench.status !== "online" && lease?.state !== "queued") {
    const text = workbench.status === "maintenance" ? copy.bench.maintenance : copy.bench.offline;
    return (
      <section className="card lease">
        <p className="muted">{text}</p>
      </section>
    );
  }

  switch (lease?.state) {
    case "active": {
      const remaining = secondsUntil(lease.expires_at, now);
      return (
        <section className="card lease row">
          <span className="status-dot on" aria-hidden="true" />
          <div className="grow heading">
            <h2>{copy.lease.active}</h2>
            <p className={`small timer ${remaining <= 30 ? "warn" : ""}`}>
              {copy.lease.remaining(formatClock(remaining))}
            </p>
          </div>
          <button className="ghost compact" onClick={onRelease} disabled={busy}>
            {copy.lease.release}
          </button>
        </section>
      );
    }
    case "queued":
      return (
        <section className="card lease row">
          <div className="queue-pos">{copy.lease.position(lease.queue_position ?? 0)}</div>
          <div className="grow heading">
            <h2>{copy.lease.queued}</h2>
            <p className="muted small">{copy.lease.queuedHint}</p>
          </div>
          <button className="ghost compact" onClick={onRelease} disabled={busy}>
            {copy.lease.leaveQueue}
          </button>
        </section>
      );
    case "offered":
      return (
        <section className="card lease highlight stack">
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
    default: {
      const ended =
        lease && lease.state in copy.lease.ended
          ? copy.lease.ended[lease.state as keyof typeof copy.lease.ended]
          : null;
      const status = workbench?.available
        ? copy.lease.benchFree
        : copy.lease.waiting(workbench?.queue_length ?? 0);
      return (
        <section className="card lease row">
          <div className="grow heading">
            <h2>{ended ?? copy.lease.idleHeading}</h2>
            <p className="muted small">{status}</p>
          </div>
          <button className="primary compact" onClick={onJoin} disabled={busy || !workbench}>
            {ended ? copy.lease.again : copy.lease.joinQueue}
          </button>
        </section>
      );
    }
  }
}

// --------------------------------------------------------------------------- camera

export function CameraCard({
  url,
  stream,
  workbench,
}: {
  url: string | null;
  stream: StreamDescriptor | null;
  workbench: Workbench | null;
}) {
  // Bumping the key remounts the <img>, which reconnects a dropped stream. The <img>
  // is also unmounted while the bench is offline, so it reconnects when it returns.
  const [attempt, setAttempt] = useState(0);
  const online = workbench?.status === "online" && url !== null;
  const ratio = stream?.width && stream?.height ? stream.width / stream.height : 16 / 9;

  return (
    <CollapsibleCard
      id="camera"
      title={copy.camera.title}
      badge={
        online ? (
          <span className="badge live">
            <span className="dot" /> {copy.stream.live}
          </span>
        ) : (
          <span className="badge muted-badge">{copy.stream.offline}</span>
        )
      }
    >
      <div className="stream" style={{ aspectRatio: ratio, "--ratio": ratio } as CSSProperties}>
        <div className="stream-placeholder">
          {online ? copy.stream.connecting : copy.stream.offline}
        </div>
        {online && (
          <img
            key={attempt}
            src={`${url}${url.includes("?") ? "&" : "?"}attempt=${attempt}`}
            alt={copy.camera.alt}
            onError={() => window.setTimeout(() => setAttempt((a) => a + 1), 2000)}
          />
        )}
        <div className="stream-badges">
          {workbench?.resetting && <span className="badge">{copy.stream.resetting}</span>}
          {workbench?.current_holder && (
            <span className="badge">{copy.stream.inUseBy(workbench.current_holder)}</span>
          )}
        </div>
      </div>
    </CollapsibleCard>
  );
}

// --------------------------------------------------------------------------- vibrator

function ShapeIcon({ shape }: { shape: string }) {
  const paths: Record<string, string> = {
    sine: "M2 8c2.5-7 5.5-7 8 0s5.5 7 8 0",
    square: "M2 12V4h8v8h8V4",
    triangle: "M2 8 6 3l8 10 4-5",
    sawtooth: "M2 12 10 4v8l8-8",
  };
  return (
    <svg viewBox="0 0 20 16" width="20" height="16" aria-hidden="true">
      <path
        d={paths[shape] ?? paths.sine}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function NumberControl({
  name,
  schema,
  value,
  onChange,
}: {
  name: string;
  schema: ParameterSchema;
  value: number;
  onChange: (value: number) => void;
}) {
  const id = `param-${name}`;
  const step = schema["x-step"] ?? (schema.type === "integer" ? 1 : 0.01);
  const min = schema.minimum ?? 0;
  const max = schema.maximum ?? 1;
  const clamp = (v: number) => Math.min(max, Math.max(min, Number(v.toFixed(6))));
  // Percentages nudge by whole percent; other values by one step (or 1 for fine steps).
  const nudge = schema["x-format"] === "percent" ? 0.01 : Math.max(step, step < 1 ? 1 : step);
  const percentFill = ((value - min) / (max - min)) * 100;

  return (
    <div className="field">
      <div className="field-header">
        <label htmlFor={id} className="label">
          {schema.title ?? name}
        </label>
        <div className="stepper">
          <button
            type="button"
            aria-label={copy.controls.decrease(schema.title ?? name)}
            onClick={() => onChange(clamp(value - nudge))}
            disabled={value <= min}
          >
            −
          </button>
          <output htmlFor={id} className="value">
            {formatValue(schema, value)}
          </output>
          <button
            type="button"
            aria-label={copy.controls.increase(schema.title ?? name)}
            onClick={() => onChange(clamp(value + nudge))}
            disabled={value >= max}
          >
            +
          </button>
        </div>
      </div>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        style={{ "--fill": `${percentFill}%` } as CSSProperties}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {schema.description && <p className="field-description">{schema.description}</p>}
    </div>
  );
}

export function VibratorCard({
  experiment,
  control,
}: {
  experiment: Experiment;
  control: LiveControlState;
}) {
  const { live, values, canControl, on, busy, setOn, setValue } = control;
  if (!live) return null;

  const summary = live.parameters
    .map((name) => formatValue(experiment.parameters[name], values[name] ?? ""))
    .join(" · ");
  const hint = on
    ? null
    : canControl
      ? copy.controls.off
      : control.holdsLease && !control.tool
        ? copy.controls.unsupported
        : copy.controls.needLease;

  return (
    <CollapsibleCard
      id="vibrator"
      title={copy.controls.title}
      badge={<span className={`badge ${on ? "on-badge" : "muted-badge"}`}>{summary}</span>}
      actions={
        <Switch
          checked={on}
          disabled={!canControl || busy}
          label={copy.controls.power}
          onChange={(next) => void setOn(next)}
        />
      }
    >
      {hint && <p className="small muted vibrator-hint">{hint}</p>}
      {live.parameters.map((name) => {
        const schema = experiment.parameters[name];
        if (!schema) return null;
        const value = values[name];
        if (schema.enum) {
          return (
            <div className="field" key={name}>
              <span className="label">{schema.title ?? name}</span>
              <Segmented
                label={schema.title ?? name}
                value={String(value)}
                options={schema.enum.map((option) => ({
                  value: option,
                  label: copy.shapes[option as keyof typeof copy.shapes] ?? option,
                  icon: <ShapeIcon shape={option} />,
                }))}
                onChange={(v) => setValue(name, v)}
              />
              {schema.description && <p className="field-description">{schema.description}</p>}
            </div>
          );
        }
        return (
          <NumberControl
            key={name}
            name={name}
            schema={schema}
            value={Number(value)}
            onChange={(v) => setValue(name, v)}
          />
        );
      })}
    </CollapsibleCard>
  );
}

// --------------------------------------------------------------------------- guide

function PresetButton({
  experiment,
  preset,
  label,
  values,
  onApply,
}: {
  experiment: Experiment;
  preset: Preset;
  label: ReactNode;
  values: ParameterValues;
  onApply: (preset: Preset) => void;
}) {
  const selected = matchesPreset(values, preset);
  return (
    <button
      type="button"
      className={`preset ${selected ? "selected" : ""}`}
      aria-pressed={selected}
      onClick={() => onApply(preset)}
    >
      <span className="preset-label">{label}</span>
      <span className="preset-values">{presetSummary(experiment, preset)}</span>
    </button>
  );
}

const PRESET_PREFIX = "preset:";

export function GuidePane({
  experiment,
  control,
}: {
  experiment: Experiment;
  control: LiveControlState;
}) {
  const components: Components = {
    a({ href, children }) {
      if (href?.startsWith(PRESET_PREFIX)) {
        const preset = experiment.presets[href.slice(PRESET_PREFIX.length)];
        if (!preset) return <>{children}</>;
        const hasText = Array.isArray(children) ? children.length > 0 : Boolean(children);
        return (
          <PresetButton
            experiment={experiment}
            preset={preset}
            label={hasText ? children : preset.label}
            values={control.values}
            onApply={control.applyPreset}
          />
        );
      }
      return (
        <a href={href} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      );
    },
  };

  return (
    <article className="guide">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={components}
        urlTransform={(url) => (url.startsWith(PRESET_PREFIX) ? url : defaultUrlTransform(url))}
      >
        {experiment.notes}
      </Markdown>
    </article>
  );
}
