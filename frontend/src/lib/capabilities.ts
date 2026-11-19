/** The deployment's optional capabilities, as `GET /api/capabilities` reports them. */

/** An optional capability of the deployment's sync adapter (`GET /api/capabilities`). */
export type Capability = "machine_credentials" | "custom_attributes";
const CAPABILITIES: readonly Capability[] = ["machine_credentials", "custom_attributes"];

/** The deployment's relationship store; `none` serves no FGA or documents. */
export type RelationshipStoreKind = "descope" | "none";
const RELATIONSHIP_STORES: readonly RelationshipStoreKind[] = ["descope", "none"];

/** What a page needs: a capability, or a relationship store of any kind. */
export type Requirement = Capability | "relationship_store";

/** The requirement of each page that has one. Every other page is always shown. */
export const PAGE_REQUIREMENTS: Readonly<Record<string, Requirement>> = {
  "/keys": "machine_credentials",
  "/fga": "relationship_store",
  "/settings": "custom_attributes",
};

export interface Capabilities {
  capabilities: ReadonlySet<Capability>;
  relationshipStore: RelationshipStoreKind;
}

/** Nothing optional: what the SPA assumes until, or unless, the backend says otherwise. */
export const NO_CAPABILITIES: Capabilities = { capabilities: new Set(), relationshipStore: "none" };

/** Read the backend's answer, keeping only the values the SPA knows. */
export function parseCapabilities(body: unknown): Capabilities {
  if (typeof body !== "object" || body === null) return NO_CAPABILITIES;
  const { capabilities, relationship_store } = body as Record<string, unknown>;
  const known = Array.isArray(capabilities)
    ? capabilities.filter((c): c is Capability => CAPABILITIES.includes(c as Capability))
    : [];
  const store = RELATIONSHIP_STORES.includes(relationship_store as RelationshipStoreKind)
    ? (relationship_store as RelationshipStoreKind)
    : "none";
  return { capabilities: new Set(known), relationshipStore: store };
}

export function servedBy(capabilities: Capabilities, requirement: Requirement): boolean {
  if (requirement === "relationship_store") return capabilities.relationshipStore !== "none";
  return capabilities.capabilities.has(requirement);
}
