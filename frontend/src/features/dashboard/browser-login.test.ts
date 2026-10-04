import { describe, expect, it } from "vitest";
import { isBrowserCallback, usesBrowserSignIn } from "./browser-login";

describe("usesBrowserSignIn", () => {
  it("sends Google and GitHub through the browser unless a device code is chosen", () => {
    expect(usesBrowserSignIn("google", "browser")).toBe(true);
    expect(usesBrowserSignIn("github", "browser")).toBe(true);
    expect(usesBrowserSignIn("github", "device")).toBe(false);
  });

  it("keeps Builder ID on the device flow", () => {
    expect(usesBrowserSignIn("builder-id", "browser")).toBe(false);
  });
});

describe("isBrowserCallback", () => {
  it("accepts the callback address the browser ends on", () => {
    expect(
      isBrowserCallback(" http://localhost:3128/oauth/callback?login_option=github&code=abc&state=xyz "),
    ).toBe(true);
  });

  it("accepts a denied sign-in so the dashboard can report it", () => {
    expect(isBrowserCallback("http://localhost:3128/oauth/callback?error=access_denied&state=xyz")).toBe(true);
  });

  it("rejects other addresses and incomplete callbacks", () => {
    expect(isBrowserCallback("")).toBe(false);
    expect(isBrowserCallback("not a url")).toBe(false);
    expect(isBrowserCallback("https://app.kiro.dev/signin?state=xyz")).toBe(false);
    expect(isBrowserCallback("http://localhost:3128/oauth/callback?state=xyz")).toBe(false);
    expect(isBrowserCallback("http://localhost:3128/oauth/callback?code=abc")).toBe(false);
  });
});
