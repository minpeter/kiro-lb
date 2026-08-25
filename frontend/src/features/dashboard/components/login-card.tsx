import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { passwordInputType, passwordToggleLabel } from "../login-visibility";
import { KiroLogo } from "./shell";

export function LoginCard({ error, onSignIn }: { error: string; onSignIn: (password: string) => Promise<void> }) {
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [passwordVisible, setPasswordVisible] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      await onSignIn(password);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <KiroLogo size={40} />
          <CardTitle className="mt-3">Kiro-LB</CardTitle>
          <CardDescription>Private Kiro router operations console</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <div className="space-y-2">
              <Label htmlFor="dashboard-password">Dashboard password</Label>
              <div className="relative">
                <Input
                  id="dashboard-password"
                  autoFocus
                  required
                  type={passwordInputType(passwordVisible)}
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  className="pr-9"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  className="absolute top-1/2 right-0.5 -translate-y-1/2 text-muted-foreground"
                  aria-label={passwordToggleLabel(passwordVisible)}
                  onClick={() => setPasswordVisible((visible) => !visible)}
                >
                  {passwordVisible ? <EyeOff /> : <Eye />}
                </Button>
              </div>
            </div>
            {error ? (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            ) : null}
            <Button type="submit" className="w-full" disabled={submitting}>
              Sign in
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
