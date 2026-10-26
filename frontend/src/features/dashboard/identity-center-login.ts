import type { IdentityCenterLoginConfig } from "./types";

export function identityCenterLoginError(config: IdentityCenterLoginConfig): "startUrl" | "region" | null {
  try {
    const startUrl = config.startUrl.trim();
    const url = new URL(startUrl);
    if (
      !/^https:\/\//i.test(startUrl) ||
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.href.includes("?") ||
      url.href.includes("#")
    ) return "startUrl";
  } catch {
    return "startUrl";
  }
  const region = config.region.trim();
  return region.length <= 63 && /^[a-z]{2,}(?:-[a-z0-9]+)+-[1-9]\d*$/.test(region) ? null : "region";
}
