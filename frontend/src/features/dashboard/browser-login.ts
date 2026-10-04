import type { DeviceLoginProvider } from "./types";

export type SocialLoginMode = "browser" | "device";

/** Google and GitHub use browser sign-in unless the operator picks a device code. */
export function usesBrowserSignIn(provider: DeviceLoginProvider, mode: SocialLoginMode): provider is "google" | "github" {
  return provider !== "builder-id" && mode === "browser";
}

/**
 * True when the pasted text is the loopback callback the portal redirects to:
 * `http://localhost:3128/oauth/callback?...` with a `state` and either a `code`
 * or an `error` (a denied sign-in, which the backend reports as failed).
 */
export function isBrowserCallback(text: string): boolean {
  let url: URL;
  try {
    url = new URL(text.trim());
  } catch {
    return false;
  }
  return (
    url.pathname === "/oauth/callback" &&
    Boolean(url.searchParams.get("state")) &&
    (Boolean(url.searchParams.get("code")) || Boolean(url.searchParams.get("error")))
  );
}
