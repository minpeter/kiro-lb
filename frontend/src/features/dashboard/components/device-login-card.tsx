import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { ComponentType } from "react";
import { ExternalLink, Link2, LogIn, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DashboardApiError, dashboardApi } from "../api";
import { dismissAlert, pushAlert, pushError } from "../alerts";
import { isBrowserCallback, usesBrowserSignIn, type SocialLoginMode } from "../browser-login";
import { registrationMessage } from "../device-login-result";
import type { BrowserLoginFlow, DeviceLoginFlow, DeviceLoginProvider } from "../types";
import { AwsMark, GithubMark, GoogleMark } from "./provider-marks";
import { usePreferences } from "../preferences";

const POLL_INTERVAL_MS = 2500;

const PROVIDERS: { id: DeviceLoginProvider; label: string; mark: ComponentType<{ size?: number }> }[] = [
  { id: "builder-id", label: "AWS Builder ID", mark: AwsMark },
  { id: "google", label: "Google", mark: GoogleMark },
  { id: "github", label: "GitHub", mark: GithubMark },
];

type ActiveFlow = { kind: "device"; flow: DeviceLoginFlow } | { kind: "browser"; flow: BrowserLoginFlow };

const flowApi = {
  device: {
    poll: dashboardApi.pollDeviceLogin,
    register: dashboardApi.registerDeviceLogin,
    cancel: dashboardApi.cancelDeviceLogin,
  },
  browser: {
    poll: dashboardApi.pollBrowserLogin,
    register: dashboardApi.registerBrowserLogin,
    cancel: dashboardApi.cancelBrowserLogin,
  },
};

export function DeviceLoginCard({ onRegistered }: { onRegistered: () => Promise<void> }) {
  const { t, language } = usePreferences();
  const [active, setActive] = useState<ActiveFlow>();
  const [mode, setMode] = useState<SocialLoginMode>("browser");
  const [starting, setStarting] = useState(false);
  const [submittingFlowId, setSubmittingFlowId] = useState<string>();
  const [pasted, setPasted] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const [deadline, setDeadline] = useState(0);
  // True while an approved flow is being registered: the pending card stays up
  // (no new sign-in can start) and polling stops.
  const [registeringFlow, setRegisteringFlow] = useState(false);
  // Consume the id before registration or cancellation. Late responses from
  // either polling or a pasted callback must not revive a finished flow.
  const pendingFlowId = useRef<string | undefined>(undefined);
  const linkAlert = useRef<number | undefined>(undefined);
  const pasteId = useId();

  const copyLink = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      linkAlert.current = pushAlert({ tone: "success", key: "accounts.login.linkCopied" });
    } catch {
      linkAlert.current = pushAlert({ tone: "warning", key: "accounts.login.copyFailed" });
    }
  };

  const start = async (provider: DeviceLoginProvider) => {
    setStarting(true);
    setPasted("");
    try {
      if (usesBrowserSignIn(provider, mode)) {
        const started = await dashboardApi.startBrowserLogin(provider);
        pendingFlowId.current = started.flowId;
        setActive({ kind: "browser", flow: started });
        setDeadline(Date.now() + started.expiresInSeconds * 1000);
      } else {
        const started = await dashboardApi.startDeviceLogin(provider);
        pendingFlowId.current = started.flowId;
        setActive({ kind: "device", flow: started });
        setDeadline(Date.now() + started.expiresInSeconds * 1000);
        await copyLink(started.verificationUriComplete);
      }
      setNow(Date.now());
    } catch (cause) {
      pushError(cause);
    } finally {
      setStarting(false);
    }
  };

  const cancel = useCallback(async () => {
    if (!active || pendingFlowId.current !== active.flow.flowId) return;
    pendingFlowId.current = undefined;
    setActive(undefined);
    if (linkAlert.current !== undefined) dismissAlert(linkAlert.current);
    await flowApi[active.kind].cancel(active.flow.flowId).catch(() => undefined);
  }, [active]);

  // Registration is triggered by the approval itself, so the operator only ever
  // clicks once. Claim the flow before awaiting, even if another response arrives
  // after registration has already finished.
  const registerApproved = useCallback(
    async (kind: ActiveFlow["kind"], flowId: string) => {
      if (pendingFlowId.current !== flowId) return;
      pendingFlowId.current = undefined;
      setRegisteringFlow(true);
      try {
        const result = await flowApi[kind].register(flowId);
        const registered = registrationMessage(language, result);
        pushAlert({ tone: registered.tone === "ok" ? "success" : "warning", text: registered.text });
        setActive(undefined);
        await onRegistered();
      } catch (cause) {
        pushError(cause);
        setActive(undefined);
      } finally {
        setRegisteringFlow(false);
      }
    },
    [onRegistered, language],
  );

  const settle = useCallback(
    async (next: ActiveFlow) => {
      if (pendingFlowId.current !== next.flow.flowId) return true;
      if (next.flow.status === "approved") {
        // Keep showing the pending flow until registration has finished.
        await registerApproved(next.kind, next.flow.flowId);
        return true;
      }
      setActive(next);
      if (next.flow.status !== "pending") {
        pendingFlowId.current = undefined;
        pushAlert(
          next.flow.detail
            ? { tone: "error", error: next.flow.detail }
            : { tone: "error", key: "accounts.login.status", vars: { status: next.flow.status } },
        );
        setActive(undefined);
        return true;
      }
      return false;
    },
    [registerApproved],
  );

  const submitPasted = async () => {
    if (active?.kind !== "browser") return;
    const flowId = active.flow.flowId;
    if (pendingFlowId.current !== flowId) return;
    setSubmittingFlowId(flowId);
    try {
      const next = await dashboardApi.completeBrowserLogin(flowId, pasted.trim());
      await settle({ kind: "browser", flow: next });
    } catch (cause) {
      if (pendingFlowId.current === flowId) pushError(cause);
    } finally {
      // A finished flow must neither block a new sign-in nor unlock its form.
      setSubmittingFlowId((current) => current === flowId ? undefined : current);
    }
  };

  const polling = active?.flow.status === "pending" && !registeringFlow;
  const pendingKind = polling ? active.kind : undefined;
  const pendingId = polling ? active.flow.flowId : undefined;

  useEffect(() => {
    if (!pendingKind || !pendingId) return;

    let stopped = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const flow = await flowApi[pendingKind].poll(pendingId);
        if (stopped) return;
        const next = { kind: pendingKind, flow } as ActiveFlow;
        if (await settle(next)) return;
      } catch (cause) {
        if (stopped || pendingFlowId.current !== pendingId) return;
        pushError(cause);
        if (cause instanceof DashboardApiError && cause.status === 404) {
          pendingFlowId.current = undefined;
          setActive(undefined);
          return;
        }
        // A failed status request does not end the sign-in. Retry while a
        // pasted callback may still be completing successfully in parallel.
      }
      if (!stopped) timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    };

    timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [pendingKind, pendingId, settle]);

  useEffect(() => {
    if (!pendingKind) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [pendingKind]);

  const remaining = Math.max(0, Math.ceil((deadline - now) / 1000));
  const remainingLabel = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;

  const pendingBody = () => {
    if (!active || active.flow.status !== "pending") return null;
    const link = active.kind === "browser" ? active.flow.authorizationUrl : active.flow.verificationUriComplete;
    return (
      <div className="space-y-3 rounded-lg border p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm">
            {t(active.kind === "browser" ? "accounts.login.waitingSignIn" : "accounts.login.waiting", {
              time: remainingLabel,
            })}
          </p>
          <Badge variant="secondary">{active.flow.provider}</Badge>
        </div>
        <a
          href={link}
          target="_blank"
          rel="noreferrer"
          className="block break-all font-mono text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          {link}
        </a>
        <div className="flex flex-wrap gap-2">
          {active.kind === "browser" ? (
            <Button size="sm" asChild>
              <a href={link} target="_blank" rel="noreferrer">
                <ExternalLink />
                {t("accounts.login.openSignIn")}
              </a>
            </Button>
          ) : null}
          <Button size="sm" variant="outline" onClick={() => void copyLink(link)}>
            <Link2 />
            {t("accounts.login.copyLink")}
          </Button>
          <Button size="sm" variant="outline" disabled={registeringFlow} onClick={() => void cancel()}>
            <X />
            {t("accounts.cancel")}
          </Button>
        </div>
        {active.kind === "browser" ? (
          <form
            className="space-y-2 border-t pt-3"
            onSubmit={(event) => {
              event.preventDefault();
              void submitPasted();
            }}
          >
            <Label htmlFor={pasteId}>{t("accounts.login.pasteLabel")}</Label>
            <p className="text-xs text-muted-foreground">
              {t(active.flow.listening ? "accounts.login.pasteHint" : "accounts.login.pasteNotListening")}
            </p>
            <div className="flex flex-wrap gap-2 sm:flex-nowrap">
              <Input
                id={pasteId}
                value={pasted}
                onChange={(event) => setPasted(event.currentTarget.value)}
                placeholder={`${active.flow.callbackUri}?code=…`}
                autoComplete="off"
                spellCheck={false}
                className="font-mono text-xs"
              />
              <Button
                type="submit"
                size="sm"
                variant="outline"
                disabled={submittingFlowId === active.flow.flowId || registeringFlow || !isBrowserCallback(pasted)}
              >
                {t("accounts.login.pasteSubmit")}
              </Button>
            </div>
          </form>
        ) : null}
      </div>
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <LogIn size={16} aria-hidden /> {t("accounts.login.title")}
        </CardTitle>
        <CardDescription>
          {t(mode === "browser" ? "accounts.login.browserDescription" : "accounts.login.deviceDescription")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {pendingBody() ?? (
          <div className="space-y-2">
            <div className="grid gap-2 sm:grid-cols-3">
              {PROVIDERS.map(({ id, label, mark: Mark }) => (
                <Button
                  key={id}
                  variant="outline"
                  disabled={starting || registeringFlow}
                  onClick={() => void start(id)}
                  className="h-11 justify-center gap-2.5 font-medium"
                >
                  <Mark />
                  {t("accounts.login.continueWith", { provider: label })}
                </Button>
              ))}
            </div>
            <Button
              variant="link"
              size="sm"
              className="h-auto px-0 text-xs"
              onClick={() => setMode(mode === "browser" ? "device" : "browser")}
            >
              {t(mode === "browser" ? "accounts.login.useDevice" : "accounts.login.useBrowser")}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
