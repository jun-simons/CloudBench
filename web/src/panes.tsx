import { useRef, useState, type CSSProperties, type ReactNode } from "react";
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
import { CollapsibleCard, InfoTip, Segmented } from "./ui";
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

/** One video stream from the bench, ready to show. */
export interface StreamSource {
  /** Unique per bench: "<tool>/<stream>". */
  key: string;
  title: string;
  descriptor: StreamDescriptor;
  url: string;
  /** The stream the experiment asks to show; the others are extra outputs. */
  primary: boolean;
}

function LiveBadge({ online }: { online: boolean }) {
  return online ? (
    <span className="badge live">
      <span className="dot" /> {copy.stream.live}
    </span>
  ) : (
    <span className="badge muted-badge">{copy.stream.offline}</span>
  );
}

/**
 * The video itself. With `fill` it takes the size its container gives it (desktop
 * panes); otherwise it keeps the stream's aspect ratio (mobile cards).
 */
function StreamView({
  source,
  workbench,
  fill = false,
}: {
  source: StreamSource | null;
  workbench: Workbench | null;
  fill?: boolean;
}) {
  // Bumping the key remounts the <img>, which reconnects a dropped stream. The <img>
  // is also unmounted while the bench is offline, so it reconnects when it returns.
  const [attempt, setAttempt] = useState(0);
  // The attempt whose <img> failed; hidden until the retry, so no broken-image icon shows.
  const [failedAttempt, setFailedAttempt] = useState<number | null>(null);
  const online = workbench?.status === "online" && source !== null;
  const { width, height } = source?.descriptor ?? {};
  const ratio = width && height ? width / height : 16 / 9;
  const url = source?.url ?? "";

  return (
    <div
      className={`stream ${fill ? "fill" : ""}`}
      style={fill ? undefined : ({ aspectRatio: ratio, "--ratio": ratio } as CSSProperties)}
    >
      <div className="stream-placeholder">
        {online ? copy.stream.connecting : copy.stream.offline}
      </div>
      {online && (
        <img
          key={attempt}
          src={`${url}${url.includes("?") ? "&" : "?"}attempt=${attempt}`}
          alt={source.primary ? copy.camera.alt : source.title}
          className={failedAttempt === attempt ? "failed" : ""}
          onError={() => {
            setFailedAttempt(attempt);
            window.setTimeout(() => setAttempt((a) => a + 1), 2000);
          }}
        />
      )}
      {(source?.primary ?? true) && (
        <div className="stream-badges">
          {workbench?.resetting && <span className="badge">{copy.stream.resetting}</span>}
          {workbench?.current_holder && (
            <span className="badge">{copy.stream.inUseBy(workbench.current_holder)}</span>
          )}
        </div>
      )}
    </div>
  );
}

/** A stream as a collapsible card (mobile). */
export function CameraCard({
  source,
  workbench,
}: {
  source: StreamSource | null;
  workbench: Workbench | null;
}) {
  const online = workbench?.status === "online" && source !== null;
  return (
    <CollapsibleCard
      // The primary stream keeps the "camera" id so its remembered collapsed state carries over.
      id={!source || source.primary ? "camera" : `stream-${source.key}`}
      title={source && !source.primary ? source.title : copy.camera.title}
      badge={<LiveBadge online={online} />}
    >
      <StreamView source={source} workbench={workbench} />
    </CollapsibleCard>
  );
}

function StreamPane({
  source,
  workbench,
}: {
  source: StreamSource | null;
  workbench: Workbench | null;
}) {
  const online = workbench?.status === "online" && source !== null;
  const d = source?.descriptor;
  const meta = d ? copy.stream.meta(d.width, d.height, d.fps) : "";
  return (
    <section className="panel stream-pane">
      <header className="panel-header">
        <h2>{source?.title ?? copy.camera.title}</h2>
        <LiveBadge online={online} />
        {meta && <span className="panel-meta">{meta}</span>}
      </header>
      <StreamView source={source} workbench={workbench} fill />
    </section>
  );
}

/**
 * Every stream on the bench, for desktop: the experiment's main view large, and extra
 * outputs (e.g. computer vision overlays from the camera tool) in a row underneath.
 */
export function StreamGrid({
  streams,
  workbench,
}: {
  streams: StreamSource[];
  workbench: Workbench | null;
}) {
  const [primary = null, ...others] = streams;
  return (
    <div className={`stream-grid ${others.length > 0 ? "has-others" : ""}`}>
      <StreamPane source={primary} workbench={workbench} />
      {others.length > 0 && (
        <div className="stream-others">
          {others.map((source) => (
            <StreamPane key={source.key} source={source} workbench={workbench} />
          ))}
        </div>
      )}
    </div>
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

/** A parameter's label, with its description behind an info button when it has one. */
function FieldLabel({
  name,
  schema,
  htmlFor,
}: {
  name: string;
  schema: ParameterSchema;
  htmlFor?: string;
}) {
  const title = schema.title ?? name;
  return (
    <span className="field-label">
      {htmlFor ? (
        <label htmlFor={htmlFor} className="label">
          {title}
        </label>
      ) : (
        <span className="label">{title}</span>
      )}
      {schema.description && (
        <InfoTip label={copy.controls.about(title)}>{schema.description}</InfoTip>
      )}
    </span>
  );
}

/**
 * The number next to a slider, which can also be typed into. Percentages are typed as
 * whole percent. Out-of-range values are capped to the parameter's limits on commit.
 */
function ValueInput({
  schema,
  title,
  value,
  clamp,
  onChange,
}: {
  schema: ParameterSchema;
  title: string;
  value: number;
  clamp: (value: number) => number;
  onChange: (value: number) => void;
}) {
  const percent = schema["x-format"] === "percent";
  const unit = percent ? "%" : (schema["x-unit"] ?? "");
  const shown = percent ? Math.round(value * 100) : Number(value.toFixed(3));
  // Text being typed; null when not editing, so the field follows the slider.
  const [draft, setDraft] = useState<string | null>(null);
  const cancelled = useRef(false);

  const commit = () => {
    const text = draft;
    setDraft(null);
    if (cancelled.current || text === null) {
      cancelled.current = false;
      return;
    }
    const typed = Number(text.replace(",", ".").trim());
    if (text.trim() === "" || !Number.isFinite(typed)) return;
    const next = clamp(percent ? typed / 100 : typed);
    if (next !== value) onChange(next);
  };

  return (
    <label className="value">
      <input
        className="value-input"
        type="text"
        inputMode="decimal"
        enterKeyHint="done"
        autoComplete="off"
        aria-label={copy.controls.typeValue(title)}
        size={Math.max(2, String(shown).length)}
        value={draft ?? String(shown)}
        onFocus={(e) => {
          setDraft(String(shown));
          e.currentTarget.select();
        }}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") {
            cancelled.current = true;
            e.currentTarget.blur();
          }
        }}
      />
      {unit && <span className="value-unit">{unit}</span>}
    </label>
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
  const title = schema.title ?? name;
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
        <FieldLabel name={name} schema={schema} htmlFor={id} />
        <div className="stepper">
          <button
            type="button"
            aria-label={copy.controls.decrease(title)}
            onClick={() => onChange(clamp(value - nudge))}
            disabled={value <= min}
          >
            −
          </button>
          <ValueInput
            schema={schema}
            title={title}
            value={value}
            clamp={clamp}
            onChange={onChange}
          />
          <button
            type="button"
            aria-label={copy.controls.increase(title)}
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
  // Only a bench that cannot be controlled at all needs explaining in the card; why Start
  // is disabled before you have the bench is a tooltip, as the lease panel says it already.
  const unsupported = control.holdsLease && !control.tool;
  const disabledReason = canControl
    ? undefined
    : unsupported
      ? copy.controls.unsupported
      : copy.controls.needLease;

  return (
    <CollapsibleCard
      id="vibrator"
      title={copy.controls.title}
      badge={
        <span className={`badge ${on ? "on-badge" : "muted-badge"}`}>
          <span className="badge-text">{summary}</span>
        </span>
      }
      actions={
        <span title={disabledReason}>
          <button
            type="button"
            className={`compact power ${on ? "danger" : "primary"}`}
            disabled={!canControl || busy}
            onClick={() => void setOn(!on)}
          >
            <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
              {on ? (
                <rect x="1.5" y="1.5" width="9" height="9" rx="1.5" fill="currentColor" />
              ) : (
                <path
                  d="M3 1.6v8.8a.6.6 0 0 0 .9.5l7-4.4a.6.6 0 0 0 0-1L3.9 1.1a.6.6 0 0 0-.9.5Z"
                  fill="currentColor"
                />
              )}
            </svg>
            {on ? copy.controls.stop : copy.controls.start}
          </button>
        </span>
      }
    >
      {unsupported && <p className="small vibrator-warning">{copy.controls.unsupported}</p>}
      {live.parameters.map((name) => {
        const schema = experiment.parameters[name];
        if (!schema) return null;
        const value = values[name];
        if (schema.enum) {
          return (
            <div className="field" key={name}>
              <div className="field-header">
                <FieldLabel name={name} schema={schema} />
              </div>
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
