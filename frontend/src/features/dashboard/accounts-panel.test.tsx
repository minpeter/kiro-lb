import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { groupAccounts } from "./components/account-groups";
import { AccountsPanel } from "./components/accounts-panel";
import accountMessages from "./i18n/accounts";
import type { Account } from "./types";

const mockAccount: Account = {
  id: "acc_test12345",
  initialized: true,
  routingState: "available",
  eligibleInSeconds: 0,
  requests: 10,
  failures: 0,
  cooldownSeconds: 0,
  deletable: false,
};

const deletableAccount: Account = {
  id: "acc_deletable1",
  initialized: true,
  routingState: "available",
  eligibleInSeconds: 0,
  requests: 10,
  failures: 0,
  cooldownSeconds: 0,
  deletable: true,
};

const nonDeletableAccount: Account = {
  id: "acc_nondeletable2",
  initialized: true,
  routingState: "available",
  eligibleInSeconds: 0,
  requests: 5,
  failures: 0,
  cooldownSeconds: 0,
  deletable: false,
};

describe("AccountsPanel", () => {
  it("renders unchanged AccountsPanel with a known account id", () => {
    const html = renderToString(
      <AccountsPanel accounts={[mockAccount]} isLoading={false} />
    );
    expect(html).toContain("acc_test12345");
  });

  it("renders delete trigger only for deletable accounts", () => {
    const html = renderToString(
      <AccountsPanel accounts={[deletableAccount, nonDeletableAccount]} isLoading={false} />
    );
    expect(html).toContain('aria-label="Delete account acc_deletable1"');
    expect(html).not.toContain('aria-label="Delete account acc_nondeletable2"');
  });

  it("disables delete trigger while mutating", () => {
    const html = renderToString(
      <AccountsPanel accounts={[deletableAccount]} isLoading={false} isMutating={true} />
    );
    expect(html).toContain('aria-label="Delete account acc_deletable1"');
    expect(html).toContain('disabled=""');
  });

  it("keeps a paused account's snapshot visible and offers a clear resume action", () => {
    const paused: Account = {
      ...deletableAccount,
      enabled: false,
      routingState: "disabled",
      requests: 41,
      failures: 3,
      sessions: 1,
      usage: {
        email: "paused@example.com",
        subscriptionTitle: "Kiro Pro",
        usagePercent: 42,
        currentUsage: 420,
        usageLimit: 1000,
      },
    };
    const html = renderToString(
      <AccountsPanel accounts={[paused]} isLoading={false} onToggleAccount={() => undefined} />
    );

    expect(html).toContain("Paused accounts");
    expect(html).toContain("last known details are kept");
    expect(html).toContain("paused@example.com");
    expect(html).toContain("Kiro Pro");
    expect(html).toContain("42.00%");
    expect(html).toContain(">41<");
    expect(html).toContain(">3<");
    expect(html).toContain(">Resume<");
  });
});

describe("excluded account grouping", () => {
  const banned: Account = {
    ...deletableAccount,
    id: "acc_banned_first",
    enabled: true,
    routingState: "suspended",
    requests: 73,
    failures: 5,
    usage: {
      email: "banned@example.com",
      subscriptionTitle: "Kiro Pro",
      usagePercent: 37,
      currentUsage: 370,
      usageLimit: 1000,
    },
  };
  const authDead: Account = {
    ...deletableAccount,
    id: "acc_auth_dead_first",
    enabled: true,
    routingState: "auth_dead",
    requests: 29,
    failures: 2,
    sessions: 3,
    usage: {
      email: "auth-dead@example.com",
      subscriptionTitle: "Kiro Free",
      usagePercent: 12,
      currentUsage: 6,
      usageLimit: 50,
      error: "Credential rejected",
    },
  };
  const paused: Account = { ...mockAccount, id: "acc_paused", enabled: false, routingState: "disabled" };
  const otherStates: Account[] = ["rate_limited", "quota_depleted", "cooling_down", "quota_exhausted", "uninitialized"].map(
    (state) => ({ ...mockAccount, id: `acc_${state}`, routingState: state as Account["routingState"] }),
  );

  it("partitions without mutating the input or replacing account objects", () => {
    const secondBanned = { ...banned, id: "acc_banned_second", enabled: false };
    const secondAuthDead = { ...authDead, id: "acc_auth_dead_second", enabled: false };
    const accounts = Object.freeze([banned, authDead, paused, ...otherStates, secondBanned, secondAuthDead, mockAccount]);
    const groups = groupAccounts(accounts);

    expect(groups).toEqual({
      activeAccounts: [mockAccount],
      unavailableAccounts: otherStates,
      pausedAccounts: [paused, secondBanned, secondAuthDead],
      authDeadAccounts: [authDead],
      bannedAccounts: [banned],
      accountIssueAccounts: [],
      displayedAccounts: [mockAccount, ...otherStates, paused, secondBanned, secondAuthDead, authDead, banned],
    });
    expect(groups.pausedAccounts[0]).toBe(paused);
    expect(groups.pausedAccounts[2]).toBe(secondAuthDead);
    expect(groups.authDeadAccounts[0]).toBe(authDead);
    expect(groups.bannedAccounts[0]).toBe(banned);
  });

  it("returns empty groups for an empty pool", () => {
    expect(groupAccounts([])).toEqual({
      activeAccounts: [],
      unavailableAccounts: [],
      pausedAccounts: [],
      authDeadAccounts: [],
      bannedAccounts: [],
      accountIssueAccounts: [],
      displayedAccounts: [],
    });
  });

  it.each([true, false, undefined])("partitions every routing state exactly once with enabled=%s", (enabled) => {
    const expectedGroups = {
      available: "activeAccounts",
      rate_limited: "unavailableAccounts",
      cooling_down: "unavailableAccounts",
      quota_depleted: "unavailableAccounts",
      quota_exhausted: "unavailableAccounts",
      uninitialized: "unavailableAccounts",
      disabled: "pausedAccounts",
      auth_dead: "authDeadAccounts",
      suspended: "bannedAccounts",
      account_issue: "accountIssueAccounts",
    } as const satisfies Record<Account["routingState"], keyof ReturnType<typeof groupAccounts>>;
    for (const [state, expectedGroup] of Object.entries(expectedGroups)) {
      const account = { ...mockAccount, enabled, routingState: state as Account["routingState"] };
      const groups = groupAccounts([account]);
      expect(groups[enabled === false ? "pausedAccounts" : expectedGroup]).toEqual([account]);
      expect(groups.displayedAccounts).toEqual([account]);
    }
  });

  it("does not treat an unknown upstream state as ready", () => {
    const unknown = { ...mockAccount, routingState: "new_exclusion" as Account["routingState"] };
    const groups = groupAccounts([unknown, mockAccount]);
    expect(groups.activeAccounts).toEqual([mockAccount]);
    expect(groups.unavailableAccounts).toEqual([unknown]);
    expect(groups.displayedAccounts).toEqual([mockAccount, unknown]);
  });

  it("returns a recovered account to the top only when it is enabled and ready", () => {
    expect(groupAccounts([{ ...authDead, enabled: false }]).pausedAccounts).toHaveLength(1);
    expect(groupAccounts([authDead]).authDeadAccounts).toHaveLength(1);
    expect(groupAccounts([{ ...authDead, enabled: false, routingState: "available" }]).activeAccounts).toEqual([]);
    const recovered: Account = { ...authDead, routingState: "available" };
    expect(groupAccounts([banned, recovered]).activeAccounts).toEqual([recovered]);
  });

  it("orders ready, unavailable, paused, auth-dead and banned accounts in both layouts", () => {
    const secondBanned = { ...banned, id: "acc_banned_second" };
    const secondAuthDead = { ...authDead, id: "acc_auth_dead_second" };
    const html = renderToString(
      <AccountsPanel accounts={[banned, authDead, paused, ...otherStates, secondBanned, secondAuthDead, mockAccount]} isLoading={false} />,
    );
    const [cards, table] = html.split("<table");
    for (const layout of [cards, table]) {
      expect(layout.match(/Unavailable accounts/g)).toHaveLength(1);
      expect(layout.match(/Paused accounts/g)).toHaveLength(1);
      expect(layout.match(/Authentication failures/g)).toHaveLength(1);
      expect(layout.match(/Temporarily suspended accounts/g)).toHaveLength(1);
      expect(layout.lastIndexOf(mockAccount.id)).toBeLessThan(layout.indexOf("Unavailable accounts"));
      for (const account of otherStates) {
        expect(layout).toContain(account.id);
        expect(layout.indexOf("Unavailable accounts")).toBeLessThan(layout.indexOf(account.id));
        expect(layout.lastIndexOf(account.id)).toBeLessThan(layout.indexOf("Paused accounts"));
      }
      expect(layout.indexOf("Paused accounts")).toBeLessThan(layout.indexOf(paused.id));
      expect(layout.lastIndexOf(paused.id)).toBeLessThan(layout.indexOf("Authentication failures"));
      expect(layout.indexOf("Authentication failures")).toBeLessThan(layout.indexOf(authDead.id));
      expect(layout.lastIndexOf(authDead.id)).toBeLessThan(layout.indexOf(secondAuthDead.id));
      expect(layout.lastIndexOf(secondAuthDead.id)).toBeLessThan(layout.indexOf("Temporarily suspended accounts"));
      expect(layout.indexOf("Temporarily suspended accounts")).toBeLessThan(layout.indexOf(banned.id));
      expect(layout.lastIndexOf(banned.id)).toBeLessThan(layout.indexOf(secondBanned.id));
    }
    expect(cards.match(/<article /g)).toHaveLength(11);
    expect(table.match(/<tr /g)).toHaveLength(16); // Header, four dividers, eleven accounts.
  });

  it("shows unavailable accounts even when no account is ready", () => {
    const html = renderToString(<AccountsPanel accounts={otherStates} isLoading={false} />);
    const [cards, table] = html.split("<table");
    for (const layout of [cards, table]) {
      expect(layout.match(/Unavailable accounts/g)).toHaveLength(1);
      expect(layout).not.toContain("No accounts registered");
      expect(layout).not.toContain("Paused accounts");
      for (const account of otherStates) {
        expect(layout.indexOf(account.id)).toBeGreaterThan(layout.indexOf("Unavailable accounts"));
      }
    }
  });

  it("does not show empty sections for a ready-only pool", () => {
    const html = renderToString(<AccountsPanel accounts={[mockAccount]} isLoading={false} />);
    for (const label of ["Unavailable accounts", "Paused accounts", "Authentication failures", "Temporarily suspended accounts", "AWS account issues"]) {
      expect(html).not.toContain(label);
    }
  });

  it.each([true, false, undefined])("shows an auth-dead account under the correct section with enabled=%s, keeping snapshots and actions", (enabled) => {
    const html = renderToString(
      <AccountsPanel accounts={[{ ...authDead, enabled }]} isLoading={false} onToggleAccount={() => undefined} />,
    );
    const [cards, table] = html.split("<table");
    for (const layout of [cards, table]) {
      expect(layout.match(enabled === false ? /Paused accounts/g : /Authentication failures/g)).toHaveLength(1);
      expect(layout).not.toContain(enabled === false ? "Authentication failures" : "Paused accounts");
      expect(layout).not.toContain("Unavailable accounts");
      expect(layout).not.toContain("Temporarily suspended accounts");
      expect(layout).not.toContain("No accounts registered");
      expect(layout).toContain("auth-dead@example.com");
      expect(layout).toContain("Kiro Free");
      expect(layout).toContain("12.00%");
      expect(layout).toContain(">29<");
      expect(layout).toContain(">2<");
      expect(layout).toContain(">3<");
      expect(layout).toContain("Previous reading");
      expect(layout).not.toContain("last check failed");
      expect(layout).toContain('aria-haspopup="dialog"');
      expect(layout).not.toContain("contact support");
    }
    expect(cards).toContain("Delete");
    expect(table).toContain('aria-label="Delete account acc_auth_dead_first"');
    if (enabled !== undefined) {
      expect(cards).toContain(enabled ? ">Pause<" : ">Resume<");
      expect(table).toContain(`aria-label="${enabled ? "Disable" : "Enable"} account acc_auth_dead_first"`);
    }
  });

  it("shows the section even when every account is banned, keeping snapshots and actions", () => {
    const html = renderToString(
      <AccountsPanel accounts={[banned]} isLoading={false} onToggleAccount={() => undefined} />,
    );
    const [cards, table] = html.split("<table");
    for (const layout of [cards, table]) {
      expect(layout).toContain("Temporarily suspended accounts");
      expect(layout).not.toContain("Paused accounts");
      expect(layout).not.toContain("Authentication failures");
      expect(layout).not.toContain("No accounts registered");
      expect(layout).toContain("last known details are kept");
      expect(layout).toContain("banned@example.com");
      expect(layout).toContain("Kiro Pro");
      expect(layout).toContain("37.00%");
      expect(layout).toContain(">73<");
      expect(layout).toContain(">5<");
      expect(layout).toContain('aria-haspopup="dialog"');
    }
    expect(cards).toContain(">Pause<");
    expect(cards).toContain("Delete");
    expect(table).toContain('aria-label="Delete account acc_banned_first"');
  });

  it("does not create auth-dead or banned sections for other exclusions or paused accounts", () => {
    const html = renderToString(
      <AccountsPanel accounts={[paused, ...otherStates]} isLoading={false} />,
    );
    expect(html).toContain("Paused accounts");
    expect(html).not.toContain("Authentication failures");
    expect(html).not.toContain("Temporarily suspended accounts");
  });

  it.each([null, "https://hub.example/dashboard/#accounts"])("keeps AWS issues separate and links only a configured Token Hub dashboard (%s)", (dashboardUrl) => {
    const issue: Account = {
      ...authDead,
      id: "confirmed_aws",
      routingState: "account_issue",
      awsLoginIssueAt: 1791200000,
      awsLoginDiagnostic: { result: "ERR-837", checkedAt: 1791200000 },
    };
    const inconclusiveAuth: Account = {
      ...authDead,
      id: "inconclusive_auth",
      awsLoginDiagnostic: { result: "inconclusive", checkedAt: 1791203600 },
    };
    const groups = groupAccounts([issue, banned, inconclusiveAuth]);
    expect(groups.accountIssueAccounts).toEqual([issue]);
    expect(groups.authDeadAccounts).toEqual([inconclusiveAuth]);
    expect(groups.bannedAccounts).toEqual([banned]);
    const html = renderToString(<AccountsPanel accounts={[issue, banned, inconclusiveAuth]} tokenHubDashboardUrl={dashboardUrl} isLoading={false} />);
    for (const layout of html.split("<table")) {
      expect(layout).toContain("AWS account issues");
      expect(layout).toContain("ERR-837 observed by automatic check");
      expect(layout).toContain("Token Hub");
      expect(layout).toContain('viewBox="0 0 64 64"');
      if (dashboardUrl) {
        expect(layout).toContain('href="https://hub.example/dashboard/#accounts"');
        expect(layout).toContain('target="_blank"');
        expect(layout).toContain('rel="noopener noreferrer"');
        expect(layout).toContain('aria-label="Open Token Hub dashboard (new tab)"');
      } else {
        expect(layout).not.toContain('href="');
        expect(layout).toContain("Token Hub dashboard URL is not configured");
      }
      expect(layout).toContain('aria-expanded="false"');
      expect(layout).not.toContain("permanent ban");
    }
  });

  it("defines automated diagnostic copy without manual status controls", () => {
    const messages = accountMessages["en-US"] as Record<string, string>;
    expect(messages["accounts.diagnostic.password_required"]).toContain("does not prove the account is healthy");
    expect(messages["accounts.diagnostic.inconclusive"]).toContain("existing classification is unchanged");
    expect(messages["accounts.lastConfirmedAt"]).not.toBe(messages["accounts.lastCheckedAt"]);
    expect(Object.keys(messages).some((key) => key.includes("recordLoginIssue") || key.includes("confirmLoginIssue"))).toBe(false);
  });
});

describe("RoutingStateCell", () => {
  const spentAccount: Account = {
    ...mockAccount,
    id: "acc_spent00001",
    routingState: "quota_depleted",
    eligibleInSeconds: 7200,
    quotaHeadroom: 0,
    quotaOverageEnabled: false,
  };

  const exhaustedAccount: Account = {
    ...mockAccount,
    id: "acc_exhausted1",
    routingState: "quota_exhausted",
    eligibleInSeconds: 7200,
  };

  it("labels a spent allowance instead of showing it as ready", () => {
    const html = renderToString(<AccountsPanel accounts={[spentAccount]} isLoading={false} />);
    expect(html).toContain("Quota Spent");
    expect(html).not.toContain("Ready");
  });

  it("does not advertise a spent account as still being tried", () => {
    const html = renderToString(<AccountsPanel accounts={[spentAccount]} isLoading={false} />);
    expect(html).not.toContain("still tried");
  });

  it("reports the reset countdown when one is known", () => {
    const html = renderToString(<AccountsPanel accounts={[spentAccount]} isLoading={false} />);
    expect(html).toContain("resets in");
  });

  it("says so plainly when no reset date is known", () => {
    const html = renderToString(
      <AccountsPanel accounts={[{ ...spentAccount, eligibleInSeconds: 0 }]} isLoading={false} />
    );
    expect(html).toContain("until it resets");
    expect(html).not.toContain("resets in");
  });

  it("renders both quota states the same way, since both exclude", () => {
    // Same evidence-independent outcome, so neither should look milder than the
    // other to whoever is reading the table.
    const spent = renderToString(<AccountsPanel accounts={[spentAccount]} isLoading={false} />);
    const exhausted = renderToString(<AccountsPanel accounts={[exhaustedAccount]} isLoading={false} />);

    // The id appears several times per row (tooltip, aria-label, visible text),
    // so the normalization has to rewrite every occurrence, not the first.
    expect(spent.replaceAll("Quota Spent", "QUOTA").replaceAll(spentAccount.id, "ID")).toBe(
      exhausted.replaceAll("Quota Exhausted", "QUOTA").replaceAll(exhaustedAccount.id, "ID")
    );
  });

  const authDeadAccount: Account = {
    ...mockAccount,
    id: "acc_authdead01",
    routingState: "auth_dead",
  };

  it("labels a rejected credential rather than reporting it as ready", () => {
    const html = renderToString(<AccountsPanel accounts={[authDeadAccount]} isLoading={false} />);
    expect(html).toContain("Authentication failed");
    expect(html).not.toContain(">ready<");
  });

  it("keeps detailed remedies out of the closed row and exposes a keyboard-accessible trigger", () => {
    const html = renderToString(<AccountsPanel accounts={[authDeadAccount]} isLoading={false} />);
    expect(html).toContain('aria-label="Authentication failed · details for acc_authdead01"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain("contact support");
    expect(html).not.toContain("ERR-837");
  });
});

describe("UsageCell error rendering", () => {
  // The exact string that broke the table: httpx's 401 message, 188 characters
  // with an embedded newline.
  const HTTPX_401 =
    "Client error '401 Unauthorized' for url 'https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken'\n" +
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401";

  const erroredAccount: Account = {
    ...mockAccount,
    id: "acc_errored001",
    usage: { error: HTTPX_401 },
  };

  it("shows a compact unavailable label instead of printing the upstream error", () => {
    const html = renderToString(<AccountsPanel accounts={[erroredAccount]} isLoading={false} />);
    expect(html).toContain(">Unavailable</p>");
    expect(html).toContain("401 Unauthorized");
    const visibleText = html.replace(/<[^>]*>/g, "");
    expect(visibleText).not.toContain("401 Unauthorized");
    expect(visibleText).not.toContain("developer.mozilla.org");
    expect(html).not.toContain('role="progressbar"');
  });

  it("constrains the cell so a long error cannot widen the table", () => {
    // The regression was a nowrap cell growing to fit an unbounded string. The
    // width cap plus wrapping is what keeps the remaining columns on screen.
    const html = renderToString(<AccountsPanel accounts={[erroredAccount]} isLoading={false} />);
    expect(html).toContain("max-w-40");
    expect(html).toContain("whitespace-normal");
    expect(html).toContain("break-words");
    expect(html).toContain("line-clamp-2");
  });

  it("keeps the full message reachable instead of truncating it away", () => {
    const html = renderToString(<AccountsPanel accounts={[erroredAccount]} isLoading={false} />);
    expect(html).toMatch(/title="[^"]*developer\.mozilla\.org/);
  });

  it.each(["available", "auth_dead", "disabled"] as const)("labels stale figures without a duplicate warning for %s accounts", (routingState) => {
    const stale: Account = {
      ...erroredAccount,
      routingState,
      enabled: routingState !== "disabled",
      usage: { usagePercent: 0, currentUsage: 0, usageLimit: 50, error: HTTPX_401 },
    };
    const html = renderToString(<AccountsPanel accounts={[stale]} isLoading={false} />);
    const [cards, table] = html.split("<table");
    for (const layout of [cards, table]) {
      expect(layout).toContain("0.00%");
      expect(layout).toContain('role="progressbar"');
      expect(layout.match(/Previous reading/g)).toHaveLength(1);
      expect(layout.replaceAll("<!-- -->", "")).toMatch(/title="[^"]*developer\.mozilla\.org[^>]*>· Previous reading<\/span>/);
      expect(layout).not.toContain("last check failed");
      expect(layout).not.toContain("text-warning");
      expect(layout).not.toContain(">Unavailable</p>");
      expect(layout.replace(/<[^>]*>/g, "")).not.toContain("401 Unauthorized");
    }
  });

  it("renders the usage bar for a healthy account, not the error path", () => {
    const healthy: Account = {
      ...mockAccount,
      usage: { usagePercent: 42, currentUsage: 420, usageLimit: 1000, error: null },
    };
    const html = renderToString(<AccountsPanel accounts={[healthy]} isLoading={false} />);
    expect(html).toContain("42.00%");
    expect(html).not.toContain("Previous reading");
    expect(html).not.toContain(">Unavailable</p>");
    expect(html).not.toContain("line-clamp-2");
  });
});
