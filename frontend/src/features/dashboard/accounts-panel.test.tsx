import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AccountsPanel } from "./components/accounts-panel";
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
});
