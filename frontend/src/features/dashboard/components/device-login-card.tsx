import { useCallback, useEffect, useRef, useState } from "react";
import type { ComponentType } from "react";
import { Check, Copy, ExternalLink, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { dashboardApi } from "../api";
import { copyCodeAriaLabel, copyUserCode } from "../copy-user-code";
import type { DeviceLoginFlow, DeviceLoginProvider } from "../types";
import { AwsMark, GithubMark, GoogleMark } from "./provider-marks";

const POLL_INTERVAL_MS = 2500;

const PROVIDERS: { id: DeviceLoginProvider; label: string; mark: ComponentType<{ size?: number }> }[] = [
  { id: "builder-id", label: "AWS Builder ID", mark: AwsMark },
  { id: "google", label: "Google", mark: GoogleMark },
  { id: "github", label: "GitHub", mark: GithubMark },
];

export function DeviceLoginCard({ onRegistered }: { onRegistered: () => Promise<void> }) {
  const [flow, setFlow] = useState<DeviceLoginFlow>();
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string }>();
  const registering = useRef(false);
  const copiedTimer = useRef<number | undefined>(undefined);

  const start = async (provider: DeviceLoginProvider) => {
    setBusy(true);
    setMessage(undefined);
    setCopied(false);
    try {
      const started = await dashboardApi.startDeviceLogin(provider);
      setFlow(started);
      window.open(started.verificationUriComplete, "_blank", "noopener");
    } catch (cause) {
      setMessage({ tone: "error", text: (cause as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const cancel = useCallback(async () => {
    if (flow) await dashboardApi.cancelDeviceLogin(flow.flowId).catch(() => undefined);
    setFlow(undefined);
    setCopied(false);
  }, [flow]);

  const copyCode = async () => {
    if (!flow) return;
    await copyUserCode(flow.userCode);
    setCopied(true);
    window.clearTimeout(copiedTimer.current);
    copiedTimer.current = window.setTimeout(() => setCopied(false), 1500);
  };

  // Registration is triggered by the approval itself, so the operator only ever
  // clicks once. The ref guards against a second poll landing mid-registration.
  const registerApproved = useCallback(
    async (flowId: string) => {
      if (registering.current) return;
      registering.current = true;
      try {
        const result = await dashboardApi.registerDeviceLogin(flowId);
        setMessage({
          tone: "ok",
          text: result.initialized
            ? `Account ${result.accountId} added and initialized.`
            : `Account ${result.accountId} added, but it could not be initialized yet.`,
        });
        setFlow(undefined);
        await onRegistered();
      } catch (cause) {
        setMessage({ tone: "error", text: (cause as Error).message });
        setFlow(undefined);
      } finally {
        registering.current = false;
      }
    },
    [onRegistered],
  );

  useEffect(() => {
    if (!flow || flow.status !== "pending") return;

    let stopped = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const next = await dashboardApi.pollDeviceLogin(flow.flowId);
        if (stopped) return;
        setFlow(next);
        if (next.status === "approved") {
          await registerApproved(next.flowId);
          return;
        }
        if (next.status !== "pending") {
          setMessage({ tone: "error", text: next.detail ?? `Login ${next.status}` });
          setFlow(undefined);
          return;
        }
      } catch (cause) {
        if (stopped) return;
        setMessage({ tone: "error", text: (cause as Error).message });
        setFlow(undefined);
        return;
      }
      if (!stopped) timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    };

    timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [flow, registerApproved]);

  useEffect(() => () => window.clearTimeout(copiedTimer.current), []);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Add an account by signing in</CardTitle>
        <CardDescription>
          Approve in the browser and the account is added here. Only the refresh token is stored; the approval link works
          once and expires in five minutes.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {flow && flow.status === "pending" ? (
          <div className="space-y-3 rounded-lg border p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <p className="text-sm">
                  Waiting for approval · code{" "}
                  <span className="inline-flex items-center gap-1">
                    <span className="font-mono font-medium">{flow.userCode}</span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={copyCodeAriaLabel(copied)}
                      onClick={() => void copyCode()}
                    >
                      {copied ? <Check /> : <Copy />}
                    </Button>
                  </span>
                </p>
                <p className="text-xs text-muted-foreground">
                  Expires in {Math.floor(flow.expiresInSeconds / 60)}m {flow.expiresInSeconds % 60}s
                </p>
              </div>
              <Badge variant="secondary">{flow.provider}</Badge>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button asChild size="sm" variant="outline">
                <a href={flow.verificationUriComplete} target="_blank" rel="noreferrer">
                  <ExternalLink />
                  Reopen approval link
                </a>
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void cancel()}>
                <X />
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div className="grid gap-2 sm:grid-cols-3">
            {PROVIDERS.map(({ id, label, mark: Mark }) => (
              <Button
                key={id}
                variant="outline"
                disabled={busy}
                onClick={() => void start(id)}
                className="h-11 justify-center gap-2.5 font-medium"
              >
                <Mark />
                Continue with {label}
              </Button>
            ))}
          </div>
        )}

        {message ? (
          <p
            role={message.tone === "error" ? "alert" : "status"}
            className={`flex items-center gap-1.5 text-sm ${
              message.tone === "error" ? "text-destructive" : "text-success"
            }`}
          >
            {message.tone === "ok" && <Check size={14} />}
            {message.text}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
