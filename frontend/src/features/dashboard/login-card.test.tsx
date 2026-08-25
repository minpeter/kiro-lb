import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LoginCard } from "./components/login-card";
import { passwordInputType, passwordToggleLabel } from "./login-visibility";

describe("password visibility", () => {
  it("uses password type and Show password until revealed", () => {
    expect(passwordInputType(false)).toBe("password");
    expect(passwordToggleLabel(false)).toBe("Show password");
  });

  it("uses text type and Hide password once revealed", () => {
    expect(passwordInputType(true)).toBe("text");
    expect(passwordToggleLabel(true)).toBe("Hide password");
  });
});

describe("LoginCard", () => {
  it("keeps Sign in enabled when the password is empty and marks the field required", () => {
    const html = renderToString(<LoginCard error="" onSignIn={async () => undefined} />);
    expect(html).toContain("required");
    expect(html).toContain("Show password");
    expect(html).toContain('type="password"');
    expect(html).not.toMatch(/\sdisabled(=|\s|>)/);
  });
});
