import { describe, expect, it } from "vitest";
import { errorKey, localizeError } from "./error-text";

describe("localizeError", () => {
  it("translates known gateway errors", () => {
    expect(localizeError("pt-BR", "Invalid password")).toBe("Senha incorreta.");
    expect(localizeError("en-US", "Cannot remove the last account")).toBe("The last account cannot be removed.");
  });

  it("maps dynamic messages by prefix without leaking the detail", () => {
    const text = localizeError("pt-BR", "Account registration failed: sqlite busy");
    expect(text).toBe("Não foi possível cadastrar a conta.");
    expect(text).not.toContain("sqlite");
  });

  it("treats browser fetch failures as a network error", () => {
    expect(errorKey("Failed to fetch")).toBe("error.network");
  });

  it("translates every Identity Center error the gateway sends", () => {
    const sent = [
      "Identity Center requires a start URL",
      "Identity Center requires an absolute HTTPS start URL",
      "Identity Center requires an HTTPS start URL without credentials, query, or fragment",
      "Identity Center requires a region",
      "Identity Center requires a start URL and region",
      "Could not contact the Identity Center profile service",
      "Identity Center profile discovery returned HTTP 403",
      "Identity Center returned an invalid profile list",
      "Identity Center returned an invalid profile ARN",
      "Identity Center returned a repeated profile page",
      "No Kiro profiles are available for this organization. Ask your administrator to assign access.",
      "Could not discover Identity Center profiles. Try signing in again.",
      "Login is not waiting for profile selection",
      "Select a profile returned by this login",
      "Identity Center login requires a profile",
      "Select an Identity Center profile",
      "AWS SSO OIDC rejected the organization login (InvalidRequestException)",
      "AWS SSO OIDC could not complete the organization login",
    ];
    for (const message of sent) {
      expect(errorKey(message), message).toBeDefined();
      for (const language of ["en-US", "ko-KR", "zh-CN", "pt-BR"] as const) {
        const text = localizeError(language, message);
        expect(text, `${language}: ${message}`).not.toMatch(/^error\./);
        if (language !== "en-US") expect(text, `${language}: ${message}`).not.toBe(message);
      }
    }
    expect(errorKey("Login is not waiting for profile selection")).toBe("error.profileSelectionClosed");
    expect(localizeError("pt-BR", "Identity Center profile discovery returned HTTP 403")).not.toContain("403");
  });

  it("falls back to the raw text for unknown messages and to a generic one when empty", () => {
    expect(localizeError("pt-BR", "something new")).toBe("something new");
    expect(localizeError("pt-BR", "")).toBe("A requisição falhou.");
  });
});
