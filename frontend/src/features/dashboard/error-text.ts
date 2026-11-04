import { translate, type Language } from "./preferences";

const EXACT: Record<string, string> = {
  "Failed to fetch": "error.network",
  "Load failed": "error.network",
  "NetworkError when attempting to fetch resource.": "error.network",
  "Request failed": "error.requestFailed",
  "Action failed": "error.actionFailed",
  "Dashboard authentication required": "error.authRequired",
  "DASHBOARD_PASSWORD is not configured": "error.passwordNotConfigured",
  "Invalid password": "error.invalidPassword",
  "Invalid or missing API Key": "error.invalidApiKey",
  "Could not create API key": "error.keyCreateFailed",
  "API key not found": "error.keyNotFound",
  "The root key is set in the environment and cannot be deleted here": "error.rootKeyDelete",
  "The root key is set in the environment and cannot be renamed here": "error.rootKeyRename",
  "Expected a confirmed release version": "error.updateVersion",
  "This deployment must be updated externally": "error.updateExternal",
  "Unknown account label": "error.unknownAccount",
  "Account has multiple direct credentials entries": "error.multipleCredentials",
  "Cannot remove the last account": "error.lastAccount",
  "Cannot disable the last enabled account": "error.lastEnabledAccount",
  "Could not preserve the account state": "error.preserveAccount",
  "This credential source is already registered": "error.alreadyRegistered",
  "Kiro approved the login without a refresh token": "error.loginNoToken",
  "Unknown request log": "error.unknownLog",
  "Could not read the request log": "error.readLog",
  "Not Found": "error.notFound",
  "No Kiro profiles are available for this organization. Ask your administrator to assign access.":
    "error.noOrgProfiles",
  "Could not discover Identity Center profiles. Try signing in again.": "error.orgProfileDiscovery",
  "Could not contact the Identity Center profile service": "error.orgProfileDiscovery",
  "Select an Identity Center profile": "error.selectOrgProfile",
  "Select a profile returned by this login": "error.selectOrgProfile",
  "Identity Center login requires a profile": "error.selectOrgProfile",
  "Login is not waiting for profile selection": "error.profileSelectionClosed",
};

const PREFIXES: [string, string][] = [
  ["Could not remove the account", "error.removeAccount"],
  ["Could not change the account", "error.changeAccount"],
  ["Account registration failed", "error.registerAccount"],
  ["Kiro rejected the login request", "error.loginRejected"],
  ["Login is ", "error.loginNotApproved"],
  ["Identity Center requires", "error.orgSettings"],
  ["invalid region: ", "error.invalidRegion"],
  ["Identity Center profile discovery", "error.orgProfileDiscovery"],
  ["Identity Center returned ", "error.orgProfileDiscovery"],
  ["AWS SSO OIDC rejected the organization login", "error.orgLoginRejected"],
  ["AWS SSO OIDC could not complete the organization login", "error.orgLoginRejected"],
  ["Could not persist", "error.persist"],
  ["requestLimit must", "error.invalidLimit"],
  ["tokenLimit must", "error.invalidLimit"],
  ["Expected ", "error.invalidRequest"],
  ["Runtime state write skipped", "error.slot"],
  ["slot ", "error.slot"],
  ["handoff control", "error.slot"],
];

export function errorKey(raw: string): string | undefined {
  const text = raw.trim();
  if (EXACT[text]) return EXACT[text];
  const prefix = PREFIXES.find(([start]) => text.startsWith(start));
  if (prefix) return prefix[1];
  if (/ must be (an integer|a boolean|"logs" or "usage")$/.test(text)) return "error.invalidRequest";
  return undefined;
}

export function localizeError(language: Language, raw: string | null | undefined): string {
  if (!raw) return translate(language, "error.requestFailed");
  const key = errorKey(raw);
  return key ? translate(language, key) : raw;
}
