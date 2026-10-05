import type { Account } from "../types";

export function groupAccounts(accounts: readonly Account[]) {
  const activeAccounts: Account[] = [];
  const unavailableAccounts: Account[] = [];
  const pausedAccounts: Account[] = [];
  const authDeadAccounts: Account[] = [];
  const bannedAccounts: Account[] = [];
  const accountIssueAccounts: Account[] = [];

  for (const account of accounts) {
    if (account.enabled === false || account.routingState === "disabled") {
      pausedAccounts.push(account);
    } else if (account.routingState === "account_issue") {
      accountIssueAccounts.push(account);
    } else if (account.routingState === "auth_dead") {
      authDeadAccounts.push(account);
    } else if (account.routingState === "suspended") {
      bannedAccounts.push(account);
    } else if (account.routingState === "available") {
      activeAccounts.push(account);
    } else {
      unavailableAccounts.push(account);
    }
  }

  return {
    activeAccounts,
    unavailableAccounts,
    pausedAccounts,
    authDeadAccounts,
    bannedAccounts,
    accountIssueAccounts,
    displayedAccounts: [...activeAccounts, ...unavailableAccounts, ...pausedAccounts, ...authDeadAccounts, ...bannedAccounts, ...accountIssueAccounts],
  };
}
