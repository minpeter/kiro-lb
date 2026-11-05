import { describe, expect, it } from "vitest";
import { DashboardApiError } from "./api";
import { isProfileChoiceError } from "./identity-center-login";

describe("isProfileChoiceError", () => {
  it("keeps the approved login only for a wrong or missing profile choice", () => {
    expect(isProfileChoiceError(new DashboardApiError("Select a profile returned by this login", 400))).toBe(true);
    expect(isProfileChoiceError(new DashboardApiError("Select an Identity Center profile", 400))).toBe(true);
    expect(isProfileChoiceError(new DashboardApiError("Login is not waiting for profile selection", 400))).toBe(false);
    expect(isProfileChoiceError(new DashboardApiError("Login is expired, not approved yet", 409))).toBe(false);
    expect(isProfileChoiceError(new DashboardApiError("Select a profile returned by this login", 500))).toBe(false);
    expect(isProfileChoiceError(new Error("Select a profile returned by this login"))).toBe(false);
  });
});
