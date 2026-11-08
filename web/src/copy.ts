// Every user-facing string in the app lives here, so the copy pass happens in one file.
// Descriptive text is a "TODO:text" placeholder until that pass; short functional
// labels (buttons, states) have working defaults so the app is usable meanwhile.
// Experiment-specific text (title, parameter labels, presets, notes) lives in the
// experiment file instead.

export const copy = {
  appName: "CloudBench",

  experiments: {
    /** Heading of the experiment menu. */
    menu: "Experiments",
    choose: "Choose an experiment",
  },

  theme: {
    toLight: "Switch to light theme",
    toDark: "Switch to dark theme",
  },

  welcome: {
    heading: "CloudBench: Your lab in the cloud",
    body: "Join our HackRPI demo to create Faraday waves from your device.",
    nameLabel: "Your name",
    namePlaceholder: "e.g. Ada",
    continue: "Continue",
  },

  camera: {
    title: "Camera",
    alt: "Live view of the experiment",
  },

  stream: {
    live: "Live",
    connecting: "Connecting to camera…",
    offline: "Offline",
    inUseBy: (name: string) => `In use by ${name}`,
    resetting: "Resetting",
    /** Technical details shown in a stream pane's header on desktop, e.g. "1280×720 · 30 fps". */
    meta: (width: number | null, height: number | null, fps: number | null) =>
      [width && height ? `${width}×${height}` : null, fps ? `${fps} fps` : null]
        .filter(Boolean)
        .join(" · "),
  },

  bench: {
    offline: "This workbench is offline right now",
    maintenance: "This workbench is under maintenance",
  },

  lease: {
    /** Accessible name of the bench control (join, leave, take control, release). */
    label: "Bench session",
    joinQueue: "Join queue",
    waiting: (n: number) =>
      n === 0 ? "Nobody waiting" : n === 1 ? "1 person waiting" : `${n} people waiting`,
    position: (n: number, total: number) => `#${n} of ${total}`,
    leaveQueue: "Leave",
    yourTurn: (seconds: number) => `Your turn · ${seconds} s`,
    accept: "Take control",
    decline: "Skip",
    remaining: (time: string) => `${time} left`,
    release: "Release",
    ended: {
      released: "Released",
      expired: "Time's up",
      revoked: "Session ended",
    },
    again: "Join again",
    offline: "Offline",
    maintenance: "Maintenance",
  },

  controls: {
    title: "Vibrator",
    start: "Start",
    stop: "Stop",
    needLease: "Join the queue to enable this control",
    unsupported:
      "This bench can't be controlled from here yet: its agent needs updating to the latest version.",
    about: (name: string) => `About ${name}`,
    typeValue: (name: string) => `${name}: type a value`,
    increase: (name: string) => `Increase ${name}`,
    decrease: (name: string) => `Decrease ${name}`,
  },

  shapes: {
    sine: "Sine",
    square: "Square",
    triangle: "Triangle",
    sawtooth: "Sawtooth",
  },

  error: "Something went wrong",
  dismiss: "Dismiss",
} as const;
