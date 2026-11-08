// Every user-facing string in the app lives here, so the copy pass happens in one file.
// Descriptive text is a "TODO:text" placeholder until that pass; short functional
// labels (buttons, states) have working defaults so the app is usable meanwhile.
// Experiment-specific text (title, parameter labels, presets, notes) lives in the
// experiment file instead.

export const copy = {
  appName: "CloudBench",

  theme: {
    toLight: "Switch to light theme",
    toDark: "Switch to dark theme",
  },

  welcome: {
    heading: "TODO:text - welcome heading",
    body: "TODO:text - one or two sentences on what CloudBench is and what you are about to do",
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
    offline: "TODO:text - the workbench is offline right now",
    maintenance: "TODO:text - the workbench is under maintenance",
  },

  lease: {
    idleHeading: "TODO:text - call to action to take control of the bench",
    joinQueue: "Join queue",
    benchFree: "TODO:text - the bench is free, start right away",
    waiting: (n: number) =>
      n === 0 ? "Nobody waiting" : n === 1 ? "1 person waiting" : `${n} people waiting`,
    queued: "You're in the queue",
    position: (n: number) => `#${n}`,
    queuedHint: "TODO:text - keep this page open; we'll let you know when it's your turn",
    leaveQueue: "Leave",
    offered: "It's your turn",
    offeredHint: (seconds: number) => `Start within ${seconds} s or the next person goes`,
    start: "Start",
    decline: "Skip",
    active: "You're in control",
    remaining: (time: string) => `${time} left`,
    release: "Release",
    ended: {
      released: "TODO:text - you released the bench",
      expired: "TODO:text - your time ran out",
      revoked: "TODO:text - your session was ended by the lab",
    },
    again: "Join again",
  },

  controls: {
    title: "Vibrator",
    start: "Start",
    stop: "Stop",
    needLease: "TODO:text - hint: join the queue to control the vibrator; you can set it up now",
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
