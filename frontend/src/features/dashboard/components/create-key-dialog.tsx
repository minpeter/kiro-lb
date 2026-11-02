import { useEffect, useRef, useState, type FormEvent } from "react";
import { Check, Copy, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { API_KEY_NAME_MAX, normalizeKeyName } from "../api-key-display";
import { localizeError } from "../error-text";
import { parseLimit } from "../key-limits";
import type { KeyLimitsPatch } from "../types";
import { KeyLimitFields } from "./key-limit-fields";
import { KeyModelFields } from "./key-model-fields";
import { usePreferences } from "../preferences";

export type CreateKeyDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Creates the key; resolves with the one-time plaintext value. */
  onCreate: (name: string, limits: KeyLimitsPatch) => Promise<string>;
};

/**
 * Two-step key creation: name entry, then the one-time reveal of the
 * plaintext key. The dialog owns the pending/error/copy state; the caller
 * only supplies the create action.
 */
export function CreateKeyDialog({ open, onOpenChange, onCreate }: CreateKeyDialogProps) {
  const { t, language } = usePreferences();
  const [name, setName] = useState("");
  const [requestLimit, setRequestLimit] = useState("");
  const [tokenLimit, setTokenLimit] = useState("");
  const [allowedModels, setAllowedModels] = useState<string[] | null>(null);
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);

  // Re-open always starts at the entry step; a previously shown key must not
  // linger into the next creation flow.
  useEffect(() => {
    if (!open) return;
    setName("");
    setRequestLimit("");
    setTokenLimit("");
    setAllowedModels(null);
    setIsPending(false);
    setError(null);
    setCreatedKey(null);
    setCopied(false);
  }, [open]);

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isPending) return;
    const normalized = normalizeKeyName(name);
    if (!normalized) {
      setError(t("keys.create.nameRequired"));
      return;
    }
    const requests = parseLimit(requestLimit);
    const tokens = parseLimit(tokenLimit);
    if (!requests.ok || !tokens.ok) {
      setError(t("keys.limit.invalid"));
      return;
    }
    setIsPending(true);
    setError(null);
    try {
      setCreatedKey(await onCreate(normalized, { requestLimit: requests.value, tokenLimit: tokens.value, allowedModels }));
    } catch (err) {
      // Keep the entered name so a retry is one click.
      setError(err instanceof Error ? localizeError(language, err.message) : t("keys.create.failed"));
    } finally {
      setIsPending(false);
    }
  };

  const handleCopy = async () => {
    if (!createdKey) return;
    try {
      await navigator.clipboard.writeText(createdKey);
      setCopied(true);
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (permissions, insecure context): the key is
      // still selectable in the read-only field, so no error state is needed.
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {createdKey === null ? (
          <form onSubmit={handleSubmit} className="grid gap-4">
            <DialogHeader>
              <DialogTitle>{t("keys.create.title")}</DialogTitle>
              <DialogDescription>
                {t("keys.create.description")}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-2">
              <Label htmlFor="create-key-name">{t("keys.create.nameLabel")}</Label>
              <Input
                id="create-key-name"
                value={name}
                maxLength={API_KEY_NAME_MAX}
                required
                autoFocus
                autoComplete="off"
                placeholder={t("keys.create.placeholder")}
                disabled={isPending}
                aria-invalid={error !== null}
                aria-describedby={error ? "create-key-error" : undefined}
                onChange={(event) => {
                  setName(event.target.value);
                  if (error) setError(null);
                }}
              />
              {error && (
                <p id="create-key-error" role="alert" className="text-xs text-destructive">
                  {error}
                </p>
              )}
            </div>
            <KeyLimitFields
              idPrefix="create-key"
              requestLimit={requestLimit}
              tokenLimit={tokenLimit}
              disabled={isPending}
              onRequestLimitChange={setRequestLimit}
              onTokenLimitChange={setTokenLimit}
            />
            <KeyModelFields idPrefix="create-key" value={allowedModels} disabled={isPending} onChange={setAllowedModels} />
            <DialogFooter>
              <Button type="button" variant="outline" disabled={isPending} onClick={() => onOpenChange(false)}>
                {t("keys.cancel")}
              </Button>
              <Button
                type="submit"
                disabled={isPending || (allowedModels !== null && allowedModels.length === 0) || normalizeKeyName(name) === null || !parseLimit(requestLimit).ok || !parseLimit(tokenLimit).ok}
              >
                {isPending ? t("keys.create.creating") : t("keys.create.create")}
              </Button>
            </DialogFooter>
          </form>
        ) : (
          <div className="grid gap-4">
            <DialogHeader>
              <DialogTitle>{t("keys.create.createdTitle")}</DialogTitle>
              <DialogDescription>
                {t("keys.create.createdDescription")}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-2">
              <Label htmlFor="created-key-value">{t("keys.create.apiKey")}</Label>
              <div className="flex items-center gap-2">
                <Input
                  id="created-key-value"
                  readOnly
                  value={createdKey}
                  className="font-mono text-xs"
                  onFocus={(event) => event.target.select()}
                  aria-label={t("keys.create.newKeyAria")}
                />
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  aria-label={copied ? t("keys.create.copied") : t("keys.create.copy")}
                  onClick={handleCopy}
                >
                  {copied ? <Check className="text-success" /> : <Copy />}
                </Button>
              </div>
              <p className="flex items-center gap-1.5 text-xs text-warning">
                <TriangleAlert size={13} aria-hidden />
                {t("keys.create.storeWarning")}
              </p>
            </div>
            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                {t("keys.create.done")}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
