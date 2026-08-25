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

export type CreateKeyDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Creates the key; resolves with the one-time plaintext value. */
  onCreate: (name: string) => Promise<string>;
};

/**
 * Two-step key creation: name entry, then the one-time reveal of the
 * plaintext key. The dialog owns the pending/error/copy state; the caller
 * only supplies the create action.
 */
export function CreateKeyDialog({ open, onOpenChange, onCreate }: CreateKeyDialogProps) {
  const [name, setName] = useState("");
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
      setError("Enter a key name.");
      return;
    }
    setIsPending(true);
    setError(null);
    try {
      setCreatedKey(await onCreate(normalized));
    } catch (err) {
      // Keep the entered name so a retry is one click.
      setError(err instanceof Error ? err.message : "Failed to create the key.");
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
              <DialogTitle>Create API key</DialogTitle>
              <DialogDescription>
                The plaintext key is shown once, right after creation.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-2">
              <Label htmlFor="create-key-name">Key name</Label>
              <Input
                id="create-key-name"
                value={name}
                maxLength={API_KEY_NAME_MAX}
                required
                autoFocus
                autoComplete="off"
                placeholder="e.g. ci-pipeline"
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
            <DialogFooter>
              <Button type="button" variant="outline" disabled={isPending} onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={isPending || normalizeKeyName(name) === null}>
                {isPending ? "Creating…" : "Create"}
              </Button>
            </DialogFooter>
          </form>
        ) : (
          <div className="grid gap-4">
            <DialogHeader>
              <DialogTitle>Key created</DialogTitle>
              <DialogDescription>
                Copy this key now. It is shown only once and cannot be recovered later.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-2">
              <Label htmlFor="created-key-value">API key</Label>
              <div className="flex items-center gap-2">
                <Input
                  id="created-key-value"
                  readOnly
                  value={createdKey}
                  className="font-mono text-xs"
                  onFocus={(event) => event.target.select()}
                  aria-label="New API key, shown only once"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  aria-label={copied ? "API key copied" : "Copy API key"}
                  onClick={handleCopy}
                >
                  {copied ? <Check className="text-success" /> : <Copy />}
                </Button>
              </div>
              <p className="flex items-center gap-1.5 text-xs text-warning">
                <TriangleAlert size={13} aria-hidden />
                Store it somewhere safe; it will not be displayed again.
              </p>
            </div>
            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                Done
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
