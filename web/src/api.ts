// Typed client for the CloudBench gateway API. Shapes mirror the gateway's
// response models (see /docs on a running gateway).

export type LeaseState = "queued" | "offered" | "active" | "released" | "expired" | "revoked";
export type RunState = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export interface Principal {
  id: string;
  display_name: string;
  kind: string;
}

export interface Session {
  token: string;
  principal: Principal;
}

export interface StreamDescriptor {
  name: string;
  kind: "mjpeg";
  access: "public" | "lease";
  width: number | null;
  height: number | null;
  fps: number | null;
}

export interface Tool {
  name: string;
  type: string;
  title: string;
  description: string;
  status: "idle" | "busy" | "fault" | null;
  streams: StreamDescriptor[];
}

export interface Workbench {
  id: string;
  name: string;
  description: string;
  status: "online" | "offline" | "maintenance";
  resetting: boolean;
  queue_length: number;
  available: boolean;
  current_holder: string | null;
  tools: Tool[];
}

export interface Lease {
  id: string;
  workbench_id: string;
  holder: string;
  state: LeaseState;
  queue_position: number | null;
  queue_length: number;
  created_at: string;
  offer_expires_at: string | null;
  granted_at: string | null;
  expires_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
}

export interface ErrorInfo {
  code: string;
  message: string;
}

export interface Run {
  id: string;
  workbench_id: string;
  lease_id: string;
  tool: string;
  action: string;
  state: RunState;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error: ErrorInfo | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ParameterSchema {
  type: "number" | "integer" | "string";
  title?: string;
  description?: string;
  minimum?: number;
  maximum?: number;
  default?: number | string;
  enum?: string[];
  "x-unit"?: string;
  "x-step"?: number;
  /** "percent": a 0..1 value shown as 0..100 %. */
  "x-format"?: "percent";
}

export type ParameterValue = number | string;
export type ParameterValues = Record<string, ParameterValue>;

export interface Preset {
  label: string;
  description: string;
  values: ParameterValues;
}

/** Which tool role and actions the live controls drive (see the experiment file). */
export interface LiveControl {
  role: string;
  parameters: string[];
  start: string;
  update: string;
  stop: string;
}

export type Step = { show: string } | { run: string; input: Record<string, unknown> };

export interface Experiment {
  id: string;
  version: number;
  title: string;
  summary: string;
  notes: string;
  estimated_duration_s: number | null;
  parameters: Record<string, ParameterSchema>;
  procedure: Step[];
  presets: Record<string, Preset>;
  live: LiveControl | null;
  compatible_workbenches: { workbench_id: string; bindings: Record<string, string> }[];
}

export interface ExperimentRun {
  id: string;
  experiment_id: string;
  lease_id: string;
  parameter_values: Record<string, number | string>;
  state: RunState;
  error: ErrorInfo | null;
  created_at: string;
  finished_at: string | null;
  runs: Run[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

const TOKEN_KEY = "cloudbench.session";

function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(TOKEN_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

function saveSession(session: Session | null): void {
  try {
    if (session) localStorage.setItem(TOKEN_KEY, JSON.stringify(session));
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Private browsing or storage disabled: the session lasts for this page only.
  }
}

export class Api {
  session: Session | null = loadSession();
  onSessionLost: (() => void) | null = null;

  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (this.session) headers.Authorization = `Bearer ${this.session.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 204) return undefined as T;
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const error = data?.error ?? { code: "http_error", message: response.statusText };
      if (response.status === 401 && this.session) {
        this.setSession(null);
        this.onSessionLost?.();
      }
      throw new ApiError(response.status, error.code, error.message);
    }
    return data as T;
  }

  setSession(session: Session | null): void {
    this.session = session;
    saveSession(session);
  }

  async createSession(displayName: string): Promise<Session> {
    const session = await this.request<Session>("POST", "/v1/sessions", {
      display_name: displayName,
    });
    this.setSession(session);
    return session;
  }

  /** URL with the session token as a query parameter, for EventSource and <img>. */
  authedUrl(path: string): string {
    if (!this.session) return path;
    const sep = path.includes("?") ? "&" : "?";
    return `${path}${sep}access_token=${encodeURIComponent(this.session.token)}`;
  }

  experiments = () => this.request<Experiment[]>("GET", "/v1/experiments");
  workbench = (id: string) => this.request<Workbench>("GET", `/v1/workbenches/${id}`);
  requestLease = (workbenchId: string) =>
    this.request<Lease>("POST", `/v1/workbenches/${workbenchId}/leases`);
  getLease = (id: string) => this.request<Lease>("GET", `/v1/leases/${id}`);
  acceptLease = (id: string) => this.request<Lease>("POST", `/v1/leases/${id}/accept`);
  releaseLease = (id: string) => this.request<Lease>("DELETE", `/v1/leases/${id}`);
  startExperiment = (experimentId: string, leaseId: string, parameters: Record<string, unknown>) =>
    this.request<ExperimentRun>("POST", `/v1/experiments/${experimentId}/runs`, {
      lease_id: leaseId,
      parameters,
    });
  cancelExperimentRun = (id: string) =>
    this.request<ExperimentRun>("POST", `/v1/experiment-runs/${id}/cancel`);
  myExperimentRuns = () => this.request<ExperimentRun[]>("GET", "/v1/me/experiment-runs");
  myOpenLeases = () => this.request<Lease[]>("GET", "/v1/me/leases");
  myRuns = () => this.request<Run[]>("GET", "/v1/me/runs");
  runAction = (
    workbenchId: string,
    tool: string,
    action: string,
    leaseId: string,
    input: Record<string, unknown>,
  ) =>
    this.request<Run>(
      "POST",
      `/v1/workbenches/${workbenchId}/tools/${tool}/actions/${action}/runs`,
      { lease_id: leaseId, input },
    );
}

export const api = new Api();
