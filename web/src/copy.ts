// Every user-facing string in the app lives here, so the copy pass happens in one file.
// Descriptive text is a "TODO:text" placeholder until that pass; short functional
// labels (buttons, states) have working defaults so the app is usable meanwhile.

export const copy = {
  appName: "CloudBench",

  welcome: {
    heading: "TODO:text - welcome heading",
    body: "TODO:text - one or two sentences on what CloudBench is and what you are about to do",
    nameLabel: "Your name",
    namePlaceholder: "e.g. Ada",
    continue: "Continue",
  },

  stream: {
    live: "Live",
    connecting: "Connecting to camera…",
    offline: "Camera offline",
    inUseBy: (name: string) => `In use by ${name}`,
    resetting: "Resetting",
  },

  bench: {
    offline: "TODO:text - the workbench is offline right now",
    maintenance: "TODO:text - the workbench is under maintenance",
  },

  lease: {
    joinQueue: "Join queue",
    benchFree: "TODO:text - the bench is free, start right away",
    waiting: (n: number) =>
      n === 0 ? "Nobody waiting" : n === 1 ? "1 person waiting" : `${n} people waiting`,
    queued: "You're in the queue",
    ofTotal: (n: number) => `of ${n} in line`,
    position: (n: number) => `#${n}`,
    queuedHint: "TODO:text - keep this page open; we'll let you know when it's your turn",
    leaveQueue: "Leave queue",
    offered: "It's your turn",
    offeredHint: (seconds: number) => `Start within ${seconds} s or the next person goes`,
    start: "Start",
    decline: "Skip",
    active: "You're in control",
    remaining: (time: string) => `${time} left`,
    release: "Done, release bench",
    ended: {
      released: "TODO:text - you released the bench",
      expired: "TODO:text - your time ran out",
      revoked: "TODO:text - your session was ended by the lab",
    },
    again: "Join again",
  },

  controls: {
    run: "Run experiment",
    stop: "Stop",
    running: "Running",
    succeeded: "Done",
    failed: "Failed",
    cancelled: "Stopped",
  },

  notesHeading: "TODO:text - notes section heading",
  error: "Something went wrong",
  dismiss: "Dismiss",
} as const;
