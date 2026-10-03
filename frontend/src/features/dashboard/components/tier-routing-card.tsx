import { useCallback, useEffect, useState } from "react";
import { Scale } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { dashboardApi } from "../api";
import { pushError } from "../alerts";
import { usePreferences } from "../preferences";
import { TIER_ROUTING_MODES, summarizeTiers, tierModeHelpKey, tierModeLabelKey } from "../tier-routing";
import type { TierRoutingMode, TierRoutingResponse } from "../types";

const MODELS_SHOWN = 8;

export function TierRoutingCard({ onNotice }: { onNotice: (message: string) => void }) {
  const { t } = usePreferences();
  const [data, setData] = useState<TierRoutingResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await dashboardApi.tierRouting());
    } catch (e) {
      pushError(e);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (patch: { excludePaidModelsFromFreeAccounts?: boolean; excludeFreeModelsFromPaidAccounts?: TierRoutingMode }) => {
    setBusy(true);
    try {
      const saved = await dashboardApi.saveTierRouting(patch);
      setData((current) => (current ? { ...current, settings: saved.settings } : current));
      onNotice(t("settings.savedNotice"));
    } catch (e) {
      pushError(e);
    } finally {
      setBusy(false);
    }
  };

  const settings = data?.settings;
  const mode = settings?.excludeFreeModelsFromPaidAccounts ?? "off";
  const summary = summarizeTiers(data?.derived);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Scale size={16} aria-hidden /> {t("settings.tierRoutingTitle")}
        </CardTitle>
        <CardDescription>{t("settings.tierRoutingDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={settings?.excludePaidModelsFromFreeAccounts ?? false}
              disabled={busy || !settings}
              onChange={(event) => void save({ excludePaidModelsFromFreeAccounts: event.target.checked })}
            />
            {t("settings.tierFreeModelsRule")}
          </label>
          <p className="text-xs text-muted-foreground">{t("settings.tierFreeModelsHelp")}</p>
        </div>

        <div className="space-y-1">
          <Label htmlFor="tier-paid-models">{t("settings.tierPaidModelsRule")}</Label>
          <Select
            value={mode}
            disabled={busy || !settings}
            onValueChange={(value) => void save({ excludeFreeModelsFromPaidAccounts: value as TierRoutingMode })}
          >
            <SelectTrigger id="tier-paid-models" className="w-80">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(data?.freeModelModes ?? TIER_ROUTING_MODES).map((option) => (
                <SelectItem key={option} value={option}>
                  {t(tierModeLabelKey(option))}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">{t(tierModeHelpKey(mode))}</p>
        </div>

        <div className="space-y-1 text-xs text-muted-foreground">
          <p>
            {t("settings.tierFreeModelList", {
              n: summary.freeModels.length,
              models: summary.freeModels.slice(0, MODELS_SHOWN).join(", ") || "—",
            })}
          </p>
          <p>
            {t("settings.tierAccountSplit", {
              free: summary.freeAccounts,
              paid: summary.paidAccounts,
              unknown: summary.unknownAccounts,
            })}
          </p>
          {summary.inactive && <p>{t("settings.tierInactive")}</p>}
          {summary.unknownAccounts > 0 && <p>{t("settings.tierUnknownHint")}</p>}
        </div>
      </CardContent>
    </Card>
  );
}
