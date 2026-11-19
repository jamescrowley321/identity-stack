import { servedBy, type Capabilities, type Capability, type RelationshipStoreKind } from "@/lib/capabilities";

function set(capabilities: Capability[], relationshipStore: RelationshipStoreKind): Capabilities {
  return { capabilities: new Set(capabilities), relationshipStore };
}

/** The capability sets a deployment can report, by name. */
export const CAPABILITY_SETS = {
  descope: set(["machine_credentials", "custom_attributes"], "descope"),
  none: set([], "none"),
  machineCredentialsOnly: set(["machine_credentials"], "none"),
  customAttributesOnly: set(["custom_attributes"], "none"),
  relationshipStoreOnly: set([], "descope"),
} as const;

export type CapabilitySetName = keyof typeof CAPABILITY_SETS;

/** What `useCapabilities` returns for a loaded set. */
export function capabilitiesValue(capabilities: Capabilities) {
  return { ...capabilities, loading: false, serves: (r: Parameters<typeof servedBy>[1]) => servedBy(capabilities, r) };
}
