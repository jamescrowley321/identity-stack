/** The deployment's optional capabilities, as `GET /api/capabilities` reports them. */

/** An optional capability of the deployment's sync adapter (the backend's `Capability`). */
export const Capability = {
  MACHINE_CREDENTIALS: "machine_credentials",
  CUSTOM_ATTRIBUTES: "custom_attributes",
} as const;
export type Capability = (typeof Capability)[keyof typeof Capability];
const CAPABILITIES: readonly Capability[] = Object.values(Capability);

/** The relationship store serving the caller (the backend's `RelationshipStoreKind`); `none` serves no FGA. */
export const RelationshipStoreKind = {
  DESCOPE: "descope",
  OPENFGA: "openfga",
  NONE: "none",
} as const;
export type RelationshipStoreKind = (typeof RelationshipStoreKind)[keyof typeof RelationshipStoreKind];
const RELATIONSHIP_STORES: readonly RelationshipStoreKind[] = Object.values(RelationshipStoreKind);

/** What a page needs: a capability, or a relationship store of any kind. */
export const RELATIONSHIP_STORE = "relationship_store";
export type Requirement = Capability | typeof RELATIONSHIP_STORE;

/** The requirement of each page that has one. Every other page is always shown. */
export const PAGE_REQUIREMENTS: Readonly<Record<string, Requirement>> = {
  "/keys": Capability.MACHINE_CREDENTIALS,
  "/fga": RELATIONSHIP_STORE,
  "/settings": Capability.CUSTOM_ATTRIBUTES,
};

export interface Capabilities {
  capabilities: ReadonlySet<Capability>;
  relationshipStore: RelationshipStoreKind;
}

/** Nothing optional: what the SPA assumes until, or unless, the backend says otherwise. */
export const NO_CAPABILITIES: Capabilities = { capabilities: new Set(), relationshipStore: RelationshipStoreKind.NONE };

/** Read the backend's answer, keeping only the values the SPA knows. */
export function parseCapabilities(body: unknown): Capabilities {
  if (typeof body !== "object" || body === null) return NO_CAPABILITIES;
  const { capabilities, relationship_store } = body as Record<string, unknown>;
  const known = Array.isArray(capabilities)
    ? capabilities.filter((c): c is Capability => CAPABILITIES.includes(c as Capability))
    : [];
  const store = RELATIONSHIP_STORES.includes(relationship_store as RelationshipStoreKind)
    ? (relationship_store as RelationshipStoreKind)
    : RelationshipStoreKind.NONE;
  return { capabilities: new Set(known), relationshipStore: store };
}

export function servedBy(capabilities: Capabilities, requirement: Requirement): boolean {
  if (requirement === RELATIONSHIP_STORE) return capabilities.relationshipStore !== RelationshipStoreKind.NONE;
  return capabilities.capabilities.has(requirement);
}
