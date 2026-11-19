import { describe, it, expect } from "vitest";
import {
  Capability,
  NO_CAPABILITIES,
  RELATIONSHIP_STORE,
  SYNC_PROVIDER,
  parseCapabilities,
  servedBy,
  type Requirement,
} from "../capabilities";
import { CAPABILITY_SETS, capabilitiesBody, type CapabilitySetName } from "@/test/capabilitySets";

const { MACHINE_CREDENTIALS, CUSTOM_ATTRIBUTES } = Capability;

describe("parseCapabilities", () => {
  it.each(Object.keys(CAPABILITY_SETS) as CapabilitySetName[])("reads the backend's answer for %s", (name) => {
    expect(parseCapabilities(capabilitiesBody(CAPABILITY_SETS[name]))).toEqual(CAPABILITY_SETS[name]);
  });

  it("drops a capability or a store kind the SPA does not know", () => {
    expect(
      parseCapabilities({
        capabilities: ["teleport", MACHINE_CREDENTIALS],
        relationship_store: "keto",
        sync_provider_principal: true,
      }),
    ).toEqual(CAPABILITY_SETS.machineCredentialsOnly);
  });

  it.each([undefined, null, "true", 1])("reads a sync_provider_principal of %j as false", (flag) => {
    expect(
      parseCapabilities({ capabilities: [], relationship_store: "openfga", sync_provider_principal: flag }),
    ).toEqual(CAPABILITY_SETS.notTheSyncProvider);
  });

  it.each([null, "nope", 3, [], {}, { capabilities: MACHINE_CREDENTIALS }])("fails closed on %j", (body) => {
    expect(parseCapabilities(body)).toEqual(NO_CAPABILITIES);
  });
});

const REQUIREMENTS: Requirement[] = [MACHINE_CREDENTIALS, CUSTOM_ATTRIBUTES, RELATIONSHIP_STORE, SYNC_PROVIDER];

describe("servedBy", () => {
  it.each([
    ["descope", [MACHINE_CREDENTIALS, CUSTOM_ATTRIBUTES, RELATIONSHIP_STORE, SYNC_PROVIDER]],
    ["none", [SYNC_PROVIDER]],
    ["machineCredentialsOnly", [MACHINE_CREDENTIALS, SYNC_PROVIDER]],
    ["customAttributesOnly", [CUSTOM_ATTRIBUTES, SYNC_PROVIDER]],
    ["relationshipStoreOnly", [RELATIONSHIP_STORE, SYNC_PROVIDER]],
    ["openfga", [RELATIONSHIP_STORE, SYNC_PROVIDER]],
    ["notTheSyncProvider", [RELATIONSHIP_STORE]],
  ] as [CapabilitySetName, Requirement[]][])("%s", (name, served) => {
    expect(REQUIREMENTS.filter((r) => servedBy(CAPABILITY_SETS[name], r))).toEqual(served);
  });
});
