import { Children, isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardApiError } from "./api";
import type { BrowserLoginFlow } from "./types";

type Effect = () => void | (() => void);
type Props = {
  children?: ReactNode;
  onClick?: () => void;
  onChange?: (event: { currentTarget: { value: string } }) => void;
  onSubmit?: (event: { preventDefault: () => void }) => void;
  disabled?: boolean;
  type?: string;
};

const harness = vi.hoisted(() => ({
  states: [] as unknown[],
  stateIndex: 0,
  refs: [] as { current: unknown }[],
  refIndex: 0,
  effects: [] as Effect[],
  timers: [] as (() => Promise<void>)[],
  api: {
    startBrowserLogin: vi.fn(),
    pollBrowserLogin: vi.fn(),
    completeBrowserLogin: vi.fn(),
    registerBrowserLogin: vi.fn(),
    cancelBrowserLogin: vi.fn(),
    startDeviceLogin: vi.fn(),
    pollDeviceLogin: vi.fn(),
    registerDeviceLogin: vi.fn(),
    cancelDeviceLogin: vi.fn(),
  },
  pushAlert: vi.fn(),
  pushError: vi.fn(),
}));

// Like use-dashboard.test.ts, exercise the component's async handlers with
// persistent hook state and explicitly ordered network responses.
vi.mock("react", async (importOriginal) => ({
  ...await importOriginal<typeof import("react")>(),
  useState: (initial: unknown) => {
    const index = harness.stateIndex++;
    if (!(index in harness.states)) harness.states[index] = typeof initial === "function" ? initial() : initial;
    return [harness.states[index], (value: unknown) => {
      harness.states[index] = typeof value === "function" ? value(harness.states[index]) : value;
    }];
  },
  useRef: (initial: unknown) => harness.refs[harness.refIndex++] ??= { current: initial },
  useCallback: <T,>(callback: T) => callback,
  useEffect: (effect: Effect) => { harness.effects.push(effect); },
  useId: () => "callback-url",
}));
vi.mock("./api", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api")>(),
  dashboardApi: harness.api,
}));
vi.mock("./alerts", () => ({ pushAlert: harness.pushAlert, pushError: harness.pushError, dismissAlert: vi.fn() }));
vi.mock("./preferences", async (importOriginal) => {
  const original = await importOriginal<typeof import("./preferences")>();
  return {
    ...original,
    usePreferences: () => ({
      language: "en-US",
      t: (key: Parameters<typeof original.translate>[1], vars?: Record<string, string | number>) =>
        original.translate("en-US", key, vars),
    }),
  };
});

import { DeviceLoginCard } from "./components/device-login-card";

const flow: BrowserLoginFlow = {
  flowId: "first",
  provider: "Google",
  status: "pending",
  detail: null,
  authorizationUrl: "https://app.kiro.dev/signin?state=first",
  callbackUri: "http://localhost:3128/oauth/callback",
  listening: false,
  expiresInSeconds: 600,
};
const approved: BrowserLoginFlow = { ...flow, status: "approved" };
const onRegistered = vi.fn();
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

function render() {
  harness.stateIndex = 0;
  harness.refIndex = 0;
  harness.effects = [];
  return DeviceLoginCard({ onRegistered });
}

function find(node: ReactNode, predicate: (props: Props) => boolean): ReactElement<Props> | undefined {
  for (const child of Children.toArray(node)) {
    if (!isValidElement<Props>(child)) continue;
    if (predicate(child.props)) return child;
    const nested = find(child.props.children, predicate);
    if (nested) return nested;
  }
}

function button(label: string) {
  const found = find(render(), (props) => Boolean(props.onClick) && Children.toArray(props.children).includes(label));
  expect(found, label).toBeDefined();
  return found!.props;
}

async function start(provider = "Google") {
  button(`Continue with ${provider}`).onClick!();
  await flush();
}

function paste() {
  find(render(), (props) => Boolean(props.onChange))!.props.onChange!({
    currentTarget: { value: "http://localhost:3128/oauth/callback?code=good&state=first&login_option=google" },
  });
  find(render(), (props) => Boolean(props.onSubmit))!.props.onSubmit!({ preventDefault: () => undefined });
}

function poll() {
  render();
  harness.effects[0]!();
  return harness.timers.at(-1)!();
}

beforeEach(() => {
  vi.clearAllMocks();
  harness.states = [];
  harness.refs = [];
  harness.timers = [];
  Object.values(harness.api).forEach((method) => method.mockReset());
  harness.api.startBrowserLogin.mockResolvedValue(flow);
  harness.api.pollBrowserLogin.mockResolvedValue(approved);
  harness.api.registerBrowserLogin.mockResolvedValue({ accountId: "account", initialized: true, signedOut: [] });
  harness.api.cancelBrowserLogin.mockResolvedValue({ ok: true });
  onRegistered.mockResolvedValue(undefined);
  vi.stubGlobal("window", {
    setTimeout: (tick: () => Promise<void>) => harness.timers.push(tick),
    clearTimeout: vi.fn(),
  });
  vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("browser sign-in response ordering", () => {
  it.each(["approved", "pending", "failed", "error"] as const)(
    "ignores a late %s paste response after polling registered the account",
    async (status) => {
      const callback = deferred<BrowserLoginFlow>();
      harness.api.completeBrowserLogin.mockReturnValue(callback.promise);
      await start();
      paste();
      await poll();
      expect(onRegistered).toHaveBeenCalledOnce();
      expect(button("Continue with Google").disabled).toBe(false);

      if (status === "error") callback.reject(new Error("stale callback error"));
      else callback.resolve({ ...flow, status, detail: status === "failed" ? "stale failure" : null });
      await flush();

      expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("first");
      expect(harness.pushAlert).toHaveBeenCalledExactlyOnceWith({ tone: "success", text: "Account account added and initialized." });
      expect(harness.pushError).not.toHaveBeenCalled();
      expect(button("Continue with Google").disabled).toBe(false);
    },
  );

  it("ignores an older poll after a pasted callback registered the account", async () => {
    const pendingPoll = deferred<BrowserLoginFlow>();
    harness.api.pollBrowserLogin.mockReturnValue(pendingPoll.promise);
    harness.api.completeBrowserLogin.mockResolvedValue(approved);
    await start();
    const polling = poll();
    paste();
    await flush();
    pendingPoll.resolve(approved);
    await polling;
    expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("first");
    expect(onRegistered).toHaveBeenCalledOnce();
    expect(harness.pushError).not.toHaveBeenCalled();
  });

  it.each(["approved", "error"] as const)("ignores the %s response as soon as cancellation starts", async (status) => {
    const callback = deferred<BrowserLoginFlow>();
    const cancel = deferred<{ ok: boolean }>();
    harness.api.completeBrowserLogin.mockReturnValue(callback.promise);
    harness.api.cancelBrowserLogin.mockReturnValue(cancel.promise);
    await start();
    paste();
    button("Cancel").onClick!();
    expect(button("Continue with Google").disabled).toBe(false);
    if (status === "error") callback.reject(new Error("cancelled callback"));
    else callback.resolve(approved);
    await flush();
    expect(harness.api.registerBrowserLogin).not.toHaveBeenCalled();
    expect(harness.pushError).not.toHaveBeenCalled();
    expect(harness.pushAlert).not.toHaveBeenCalled();
    cancel.resolve({ ok: true });
    await flush();

    // Ignoring old results must not prevent the next flow from registering.
    harness.api.startBrowserLogin.mockResolvedValue({ ...flow, flowId: "second" });
    harness.api.completeBrowserLogin.mockResolvedValue({ ...approved, flowId: "second" });
    await start();
    paste();
    await flush();
    expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("second");
    expect(onRegistered).toHaveBeenCalledOnce();
  });

  it("reports a current callback error and allows another paste for the same flow", async () => {
    const error = new Error("The callback address belongs to a different sign-in");
    harness.api.completeBrowserLogin.mockRejectedValueOnce(error).mockResolvedValueOnce(approved);
    await start();
    paste();
    await flush();
    expect(harness.pushError).toHaveBeenCalledExactlyOnceWith(error);
    expect(harness.api.registerBrowserLogin).not.toHaveBeenCalled();
    paste();
    await flush();
    expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("first");
  });

  it("prevents a new sign-in until the post-registration reload finishes", async () => {
    const reload = deferred<void>();
    const callback = deferred<BrowserLoginFlow>();
    harness.api.completeBrowserLogin.mockReturnValue(callback.promise);
    onRegistered.mockReturnValueOnce(reload.promise);
    await start();
    paste();
    const polling = poll();
    await flush();
    expect(button("Continue with Google").disabled).toBe(true);
    reload.resolve();
    await polling;
    expect(button("Continue with Google").disabled).toBe(false);
  });

  it("still registers Builder ID through the device flow", async () => {
    const device = {
      ...flow, flowId: "builder", provider: "BuilderId", userCode: "ABCD",
      verificationUri: "https://device.example.com/", verificationUriComplete: "https://device.example.com/?code=ABCD",
    };
    harness.api.startDeviceLogin.mockResolvedValue(device);
    harness.api.pollDeviceLogin.mockResolvedValue({ ...device, status: "approved" });
    harness.api.registerDeviceLogin.mockResolvedValue({ accountId: "builder-account", initialized: true });
    await start("AWS Builder ID");
    await poll();
    expect(harness.api.startBrowserLogin).not.toHaveBeenCalled();
    expect(harness.api.registerDeviceLogin).toHaveBeenCalledExactlyOnceWith("builder");
    expect(onRegistered).toHaveBeenCalledOnce();
    expect(harness.pushError).not.toHaveBeenCalled();
  });

  it.each([
    new TypeError("Failed to fetch"),
    new DashboardApiError("Status endpoint unavailable", 502),
  ])("keeps an in-flight callback after a temporary poll failure: %s", async (error) => {
    const callback = deferred<BrowserLoginFlow>();
    harness.api.completeBrowserLogin.mockReturnValue(callback.promise);
    harness.api.pollBrowserLogin.mockRejectedValueOnce(error);
    await start();
    paste();
    await poll();
    expect(harness.pushError).toHaveBeenCalledExactlyOnceWith(error);
    expect(button("Cancel")).toBeDefined();
    callback.resolve(approved);
    await flush();
    expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("first");
    expect(onRegistered).toHaveBeenCalledOnce();
  });

  it("retries a temporary poll failure without requiring a pasted callback", async () => {
    harness.api.pollBrowserLogin.mockRejectedValueOnce(new DashboardApiError("Try again", 503));
    await start();
    await poll();
    expect(harness.timers).toHaveLength(2);
    await harness.timers[1]!();
    expect(harness.api.pollBrowserLogin).toHaveBeenCalledTimes(2);
    expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("first");
  });

  it("stops polling when the server no longer has the flow", async () => {
    const error = new DashboardApiError("Unknown or expired sign-in", 404);
    const callback = deferred<BrowserLoginFlow>();
    harness.api.completeBrowserLogin.mockReturnValue(callback.promise);
    harness.api.pollBrowserLogin.mockRejectedValueOnce(error);
    await start();
    paste();
    await poll();
    expect(harness.timers).toHaveLength(1);
    expect(harness.pushError).toHaveBeenCalledExactlyOnceWith(error);
    expect(button("Continue with Google").disabled).toBe(false);
    expect(harness.api.registerBrowserLogin).not.toHaveBeenCalled();
  });

  it.each(["success", "failure"] as const)("a late cancel %s cannot clear the next login", async (result) => {
    const cancellation = deferred<{ ok: boolean }>();
    harness.api.cancelBrowserLogin.mockReturnValue(cancellation.promise);
    await start();
    const cancel = button("Cancel").onClick!;
    cancel();
    cancel();
    expect(harness.api.cancelBrowserLogin).toHaveBeenCalledExactlyOnceWith("first");

    // The operator can start again before the previous DELETE has returned.
    harness.api.startBrowserLogin.mockResolvedValue({ ...flow, flowId: "second" });
    harness.api.pollBrowserLogin.mockResolvedValue({ ...approved, flowId: "second" });
    await start();
    if (result === "success") cancellation.resolve({ ok: true });
    else cancellation.reject(new Error("Old cancellation failed"));
    await flush();
    expect(button("Cancel")).toBeDefined();
    await poll();
    expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("second");
    expect(onRegistered).toHaveBeenCalledOnce();
    expect(harness.pushError).not.toHaveBeenCalled();
  });

  it("a cancelled callback cannot unlock a new sign-in request", async () => {
    const callback = deferred<BrowserLoginFlow>();
    const nextStart = deferred<BrowserLoginFlow>();
    harness.api.completeBrowserLogin.mockReturnValue(callback.promise);
    await start();
    paste();
    button("Cancel").onClick!();
    expect(button("Continue with Google").disabled).toBe(false);

    harness.api.startBrowserLogin.mockReturnValue(nextStart.promise);
    await start();
    expect(button("Continue with Google").disabled).toBe(true);
    callback.resolve(approved);
    await flush();
    expect(button("Continue with Google").disabled).toBe(true);
    expect(harness.api.registerBrowserLogin).not.toHaveBeenCalled();

    nextStart.resolve({ ...flow, flowId: "second" });
    await flush();
    expect(button("Cancel")).toBeDefined();
  });

  it.each(["approved", "error"] as const)("a stale %s callback cannot unlock the next callback submission", async (status) => {
    const first = deferred<BrowserLoginFlow>();
    const second = deferred<BrowserLoginFlow>();
    harness.api.completeBrowserLogin.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    await start();
    paste();
    button("Cancel").onClick!();
    expect(button("Continue with Google").disabled).toBe(false);

    harness.api.startBrowserLogin.mockResolvedValue({ ...flow, flowId: "second" });
    await start();
    paste();
    expect(find(render(), (props) => props.type === "submit")!.props.disabled).toBe(true);
    if (status === "error") first.reject(new Error("Old callback failed"));
    else first.resolve(approved);
    await flush();
    expect(find(render(), (props) => props.type === "submit")!.props.disabled).toBe(true);
    expect(harness.api.registerBrowserLogin).not.toHaveBeenCalled();
    expect(harness.pushError).not.toHaveBeenCalled();

    // A pending reply must unlock only its own form so the operator can retry.
    second.resolve({ ...flow, flowId: "second" });
    await flush();
    expect(find(render(), (props) => props.type === "submit")!.props.disabled).toBe(false);
    harness.api.completeBrowserLogin.mockResolvedValue({ ...approved, flowId: "second" });
    paste();
    await flush();
    expect(harness.api.registerBrowserLogin).toHaveBeenCalledExactlyOnceWith("second");
  });
});
