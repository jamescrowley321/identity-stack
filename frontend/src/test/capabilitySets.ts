import {
  Capability,
  RelationshipStoreKind,
  servedBy,
  type Capabilities,
  type Requirement,
} from "@/lib/capabilities";

function set(capabilities: Capability[], relationshipStore: RelationshipStoreKind): Capabilities {
  return { capabilities: new Set(capabilities), relationshipStore };
}

const { MACHINE_CREDENTIALS, CUSTOM_ATTRIBUTES } = Capability;

/** The capability sets a deployment can report, by name. */
export const CAPABILITY_SETS = {
  descope: set([MACHINE_CREDENTIALS, CUSTOM_ATTRIBUTES], RelationshipStoreKind.DESCOPE),
  none: set([], RelationshipStoreKind.NONE),
  machineCredentialsOnly: set([MACHINE_CREDENTIALS], RelationshipStoreKind.NONE),
  customAttributesOnly: set([CUSTOM_ATTRIBUTES], RelationshipStoreKind.NONE),
  relationshipStoreOnly: set([], RelationshipStoreKind.DESCOPE),
} as const;

export type CapabilitySetName = keyof typeof CAPABILITY_SETS;

/** The backend's answer for a set. */
export function capabilitiesBody(capabilities: Capabilities) {
  return { capabilities: [...capabilities.capabilities].sort(), relationship_store: capabilities.relationshipStore };
}

/** What `useCapabilities` returns for a loaded set. */
export function capabilitiesValue(capabilities: Capabilities) {
  return { ...capabilities, loading: false, serves: (r: Requirement) => servedBy(capabilities, r) };
}
