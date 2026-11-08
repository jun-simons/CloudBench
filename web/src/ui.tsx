// Small, reusable UI building blocks.

import { useId, useState, type FormEvent, type ReactNode } from "react";
import { copy } from "./copy";
import { usePersistentState } from "./useLab";

/** A card with a header that collapses its body; the collapsed state is remembered. */
export function CollapsibleCard({
  id,
  title,
  badge,
  actions,
  children,
  defaultOpen = true,
}: {
  id: string;
  title: ReactNode;
  /** Shown next to the title, collapsed or not (e.g. a Live badge). */
  badge?: ReactNode;
  /** Controls in the header that stay usable while collapsed (e.g. a power switch). */
  actions?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = usePersistentState(`cloudbench.card.${id}`, defaultOpen);
  const bodyId = useId();
  return (
    <section className={`card collapsible ${open ? "open" : "closed"}`}>
      <header className="card-header">
        <button
          type="button"
          className="card-toggle"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen(!open)}
        >
          <Chevron open={open} />
          <h2>{title}</h2>
        </button>
        {badge}
        <div className="grow" />
        {actions}
      </header>
      <div id={bodyId} className="card-body" hidden={!open}>
        {children}
      </div>
    </section>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`chevron ${open ? "open" : ""}`}
      viewBox="0 0 16 16"
      width="16"
      height="16"
      aria-hidden="true"
    >
      <path
        d="M6 3.5 10.5 8 6 12.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Switch({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`switch ${checked ? "on" : ""}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-thumb" />
    </button>
  );
}

export function Segmented<T extends string>({
  label,
  options,
  value,
  disabled,
  onChange,
}: {
  label: string;
  options: { value: T; label: string; icon?: ReactNode }[];
  value: T;
  disabled?: boolean;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={option.value === value}
          className={option.value === value ? "selected" : ""}
          disabled={disabled}
          onClick={() => onChange(option.value)}
        >
          {option.icon}
          <span>{option.label}</span>
        </button>
      ))}
    </div>
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
      <div className="heading">
        <h1>{copy.welcome.heading}</h1>
        <p className="muted">{copy.welcome.body}</p>
      </div>
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
