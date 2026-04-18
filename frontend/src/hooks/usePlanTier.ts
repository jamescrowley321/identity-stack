import { useEffect, useState } from "react";
import { useApiClient } from "./useApiClient";
import { useRBAC } from "./useRBAC";

/**
 * Hook to fetch and expose the current tenant's plan tier for feature gating.
 *
 * Returns loading state plus plan tier helpers.
 * Fetches from the backend (tenant custom attributes are not in the JWT).
 */
export function usePlanTier() {
  const { apiFetch } = useApiClient();
  const { currentTenantId } = useRBAC();
  const [planTier, setPlanTier] = useState<string>("free");
  const [loading, setLoading] = useState(() => Boolean(currentTenantId));

  useEffect(() => {
    if (!currentTenantId) {
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- standard data-fetching pattern: signal loading before async work
    setLoading(true);
    apiFetch("/api/tenants/current/settings")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.custom_attributes?.plan_tier) {
          setPlanTier(String(data.custom_attributes.plan_tier));
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [currentTenantId, apiFetch]);

  return {
    planTier,
    isPro: planTier === "pro" || planTier === "enterprise",
    isEnterprise: planTier === "enterprise",
    loading,
  };
}
