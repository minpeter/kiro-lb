import { useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { dashboardApi } from "../api";
import type { CredentialSource, RegistrationForm } from "../types";

const SOURCES: [CredentialSource, string, string][] = [
  ["sqlite", "Kiro CLI SQLite", "/host/kiro-cli/data.sqlite3"],
  ["json", "Kiro / SSO JSON", "~/.aws/sso/cache/kiro-auth-token.json"],
  ["refresh_token", "Refresh token", ""],
];

const EMPTY_FORM: RegistrationForm = {
  type: "sqlite",
  path: "",
  refreshToken: "",
  profileArn: "",
  region: "",
  apiRegion: "",
};

export function RegisterAccountCard({ onRegistered }: { onRegistered: () => Promise<void> }) {
  const [form, setForm] = useState<RegistrationForm>(EMPTY_FORM);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string }>();
  const [submitting, setSubmitting] = useState(false);
  const placeholder = SOURCES.find(([type]) => type === form.type)?.[2] ?? "";
  const canSubmit = form.type === "refresh_token" ? form.refreshToken.trim().length > 0 : form.path.trim().length > 0;

  const submit = async () => {
    setSubmitting(true);
    setMessage(undefined);
    try {
      const result = await dashboardApi.registerAccount(form);
      setMessage({
        tone: "ok",
        text: result.initialized
          ? "Account registered and initialized."
          : "Account saved, but it could not be initialized yet.",
      });
      setForm({ ...EMPTY_FORM, type: form.type });
      await onRegistered();
    } catch (cause) {
      setMessage({ tone: "error", text: (cause as Error).message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Register a Kiro account</CardTitle>
        <CardDescription>
          Credentials are written to the server credential store only; the API never returns them.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Credential source</legend>
            <div className="flex flex-wrap gap-2">
              {SOURCES.map(([type, label]) => (
                <Button
                  key={type}
                  type="button"
                  size="sm"
                  variant={form.type === type ? "default" : "outline"}
                  onClick={() => setForm({ ...form, type })}
                >
                  {label}
                </Button>
              ))}
            </div>
          </fieldset>

          {form.type === "refresh_token" ? (
            <div className="space-y-2">
              <Label htmlFor="refreshToken">Refresh token</Label>
              <Input
                id="refreshToken"
                type="password"
                autoComplete="off"
                value={form.refreshToken}
                onChange={(event) => setForm({ ...form, refreshToken: event.target.value })}
              />
            </div>
          ) : (
            <div className="space-y-2">
              <Label htmlFor="path">Server-side credential path</Label>
              <Input
                id="path"
                value={form.path}
                placeholder={placeholder}
                onChange={(event) => setForm({ ...form, path: event.target.value })}
              />
              <p className="text-xs text-muted-foreground">The path must be readable inside the Kiro-LB container.</p>
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="profileArn">Profile ARN</Label>
              <Input
                id="profileArn"
                value={form.profileArn}
                onChange={(event) => setForm({ ...form, profileArn: event.target.value })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="region">SSO region</Label>
              <Input
                id="region"
                placeholder="us-east-1"
                value={form.region}
                onChange={(event) => setForm({ ...form, region: event.target.value })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="apiRegion">API region</Label>
              <Input
                id="apiRegion"
                placeholder="us-east-1"
                value={form.apiRegion}
                onChange={(event) => setForm({ ...form, apiRegion: event.target.value })}
              />
            </div>
          </div>

          {message ? (
            <p
              role={message.tone === "error" ? "alert" : "status"}
              className={message.tone === "error" ? "text-sm text-destructive" : "text-sm text-emerald-500"}
            >
              {message.text}
            </p>
          ) : null}

          <Button type="submit" disabled={submitting || !canSubmit}>
            <Plus />
            Register account
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
