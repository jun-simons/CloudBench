// Small, reusable UI building blocks.

import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { copy } from "./copy";
import { usePersistentState } from "./useLab";
import type { Theme } from "./useDisplay";

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
  /** Controls in the header that stay usable while collapsed (e.g. a Start/Stop button). */
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
        {actions && <div className="card-actions">{actions}</div>}
      </header>
      {/* Closed cards do not render their body, so a collapsed stream stops downloading. */}
      <div id={bodyId} className="card-body" hidden={!open}>
        {open && children}
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

/**
 * An "i" button that reveals a short explanation: on hover with a mouse, on tap on
 * touch screens. Keeps descriptive text out of the way until someone asks for it.
 */
export function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const id = useId();

  // Close on a tap or click anywhere else, and on Escape.
  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span ref={ref} className={`infotip ${open ? "open" : ""}`}>
      <button
        type="button"
        className="infotip-button"
        aria-label={label}
        aria-describedby={id}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
          <path d="M8 7.2v3.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          <circle cx="8" cy="4.9" r="0.95" fill="currentColor" />
        </svg>
      </button>
      <span role="tooltip" id={id} className="infotip-bubble">
        {children}
      </span>
    </span>
  );
}

export function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  const label = theme === "dark" ? copy.theme.toLight : copy.theme.toDark;
  return (
    <button
      type="button"
      className="icon-button"
      aria-label={label}
      title={label}
      onClick={onToggle}
    >
      {theme === "dark" ? (
        <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
          <circle cx="10" cy="10" r="3.6" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path
            d="M10 1.8v2M10 16.2v2M1.8 10h2M16.2 10h2M4.2 4.2l1.4 1.4M14.4 14.4l1.4 1.4M4.2 15.8l1.4-1.4M14.4 5.6l1.4-1.4"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
          <path
            d="M16.5 12.3A7 7 0 0 1 7.7 3.5a7 7 0 1 0 8.8 8.8Z"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
        </svg>
      )}
    </button>
  );
}
