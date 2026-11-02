import { useEffect, useState } from "react";
import { dashboardApi } from "../api";
import { usePreferences } from "../preferences";
import type { DashboardModel } from "../types";

export type KeyModelFieldsProps = {
  idPrefix: string;
  value: string[] | null;
  disabled?: boolean;
  onChange: (value: string[] | null) => void;
};

export function KeyModelFields({ idPrefix, value, disabled, onChange }: KeyModelFieldsProps) {
  const { t } = usePreferences();
  const [models, setModels] = useState<DashboardModel[]>([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    dashboardApi
      .dashboardModels()
      .then((data) => active && setModels(data.models))
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, []);

  const selected = new Set(value ?? []);
  const known = new Set(models.map((model) => model.key ?? model.id));
  const extra = (value ?? []).filter((key) => !known.has(key));
  const options = [
    ...models.map((model) => ({ key: model.key ?? model.id, label: model.listedAs ?? model.id })),
    ...extra.map((key) => ({ key, label: key })),
  ];
  const toggle = (key: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(key);
    else next.delete(key);
    onChange([...next]);
  };

  return (
    <fieldset className="grid gap-2" disabled={disabled}>
      <legend className="mb-1 text-sm font-medium">{t("keys.models.title")}</legend>
      <div className="flex flex-wrap gap-4 text-sm">
        <label className="flex items-center gap-2">
          <input type="radio" name={`${idPrefix}-scope`} checked={value === null} onChange={() => onChange(null)} />
          {t("keys.models.all")}
        </label>
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name={`${idPrefix}-scope`}
            checked={value !== null}
            onChange={() => onChange(value ?? [])}
          />
          {t("keys.models.selected")}
        </label>
      </div>
      {value !== null && (
        <>
          <div
            className="grid max-h-44 gap-1 overflow-y-auto rounded-md border p-2 sm:grid-cols-2"
            role="group"
            aria-label={t("keys.models.title")}
          >
            {options.map((option) => (
              <label key={option.key} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={selected.has(option.key)}
                  onChange={(event) => toggle(option.key, event.target.checked)}
                />
                <span className="truncate font-mono">{option.label}</span>
              </label>
            ))}
            {options.length === 0 && (
              <p className="text-xs text-muted-foreground">
                {failed ? t("keys.models.failed") : t("keys.models.loading")}
              </p>
            )}
          </div>
          <p className={`text-xs ${value.length === 0 ? "text-destructive" : "text-muted-foreground"}`}>
            {value.length === 0 ? t("keys.models.none") : t("keys.models.count", { n: value.length })}
          </p>
        </>
      )}
    </fieldset>
  );
}
