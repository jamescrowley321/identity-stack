import { describe, it, expect } from "vitest";
import {
  Capability,
  NO_CAPABILITIES,
  RELATIONSHIP_STORE,
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
    expect(parseCapabilities({ capabilities: ["teleport", MACHINE_CREDENTIALS], relationship_store: "keto" })).toEqual(
      CAPABILITY_SETS.machineCredentialsOnly,
    );
  });

  it.each([null, "nope", 3, [], {}, { capabilities: MACHINE_CREDENTIALS }])("fails closed on %j", (body) => {
    expect(parseCapabilities(body)).toEqual(NO_CAPABILITIES);
  });
});

const REQUIREMENTS: Requirement[] = [MACHINE_CREDENTIALS, CUSTOM_ATTRIBUTES, RELATIONSHIP_STORE];

describe("servedBy", () => {
  it.each([
    ["descope", [MACHINE_CREDENTIALS, CUSTOM_ATTRIBUTES, RELATIONSHIP_STORE]],
    ["none", []],
    ["machineCredentialsOnly", [MACHINE_CREDENTIALS]],
    ["customAttributesOnly", [CUSTOM_ATTRIBUTES]],
    ["relationshipStoreOnly", [RELATIONSHIP_STORE]],
  ] as [CapabilitySetName, Requirement[]][])("%s", (name, served) => {
    expect(REQUIREMENTS.filter((r) => servedBy(CAPABILITY_SETS[name], r))).toEqual(served);
  });
});
