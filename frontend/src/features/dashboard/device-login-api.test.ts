import { afterEach, describe, expect, it, vi } from "vitest";
import { dashboardApi } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("device login request payloads", () => {
  it("posts the organization configuration to the existing device endpoint", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);

    await dashboardApi.startDeviceLogin("idc", { startUrl: "https://example.awsapps.com/start", region: "us-east-1" });

    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/dashboard/accounts/device-login", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ provider: "idc", startUrl: "https://example.awsapps.com/start", region: "us-east-1" }),
    }));
  });

  it("preserves the original provider-only request", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);

    await dashboardApi.startDeviceLogin("builder-id");

    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/dashboard/accounts/device-login", expect.objectContaining({
      body: JSON.stringify({ provider: "builder-id" }),
    }));
  });

  it("posts the selected profile ARN during registration", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    const profileArn = "arn:aws:codewhisperer:us-east-1:123456789012:profile/second";

    await dashboardApi.registerDeviceLogin("organization", profileArn);

    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/dashboard/accounts/device-login/organization/register", expect.objectContaining({
      method: "POST", body: JSON.stringify({ profileArn }),
    }));
  });

  it("leaves automatic registration requests without a profile body", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);

    await dashboardApi.registerDeviceLogin("single-profile");

    expect(fetch.mock.calls[0][1]).not.toHaveProperty("body");
  });
});
