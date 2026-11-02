import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { parseLimit } from "../key-limits";
import { usePreferences } from "../preferences";

export type KeyLimitFieldsProps = {
  idPrefix: string;
  requestLimit: string;
  tokenLimit: string;
  disabled?: boolean;
  onRequestLimitChange: (value: string) => void;
  onTokenLimitChange: (value: string) => void;
};

export function KeyLimitFields({
  idPrefix,
  requestLimit,
  tokenLimit,
  disabled,
  onRequestLimitChange,
  onTokenLimitChange,
}: KeyLimitFieldsProps) {
  const { t } = usePreferences();
  const fields = [
    { id: `${idPrefix}-request-limit`, label: t("keys.limit.requests"), value: requestLimit, onChange: onRequestLimitChange },
    { id: `${idPrefix}-token-limit`, label: t("keys.limit.tokens"), value: tokenLimit, onChange: onTokenLimitChange },
  ];
  const invalid = fields.some((field) => !parseLimit(field.value).ok);
  return (
    <div className="grid gap-2">
      <div className="grid gap-3 sm:grid-cols-2">
        {fields.map((field) => (
          <div key={field.id} className="grid gap-2">
            <Label htmlFor={field.id}>{field.label}</Label>
            <Input
              id={field.id}
              inputMode="numeric"
              autoComplete="off"
              value={field.value}
              disabled={disabled}
              placeholder={t("keys.limit.unlimited")}
              aria-invalid={!parseLimit(field.value).ok}
              onChange={(event) => field.onChange(event.target.value)}
            />
          </div>
        ))}
      </div>
      <p className={`text-xs ${invalid ? "text-destructive" : "text-muted-foreground"}`}>
        {invalid ? t("keys.limit.invalid") : t("keys.limit.hint")}
      </p>
    </div>
  );
}
