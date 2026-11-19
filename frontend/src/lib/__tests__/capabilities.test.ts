import { describe, it, expect } from "vitest";
import { NO_CAPABILITIES, parseCapabilities, servedBy } from "../capabilities";
import { CAPABILITY_SETS } from "@/test/capabilitySets";

describe("parseCapabilities", () => {
  it("reads the backend's answer", () => {
    expect(
      parseCapabilities({ capabilities: ["custom_attributes", "machine_credentials"], relationship_store: "descope" }),
    ).toEqual(CAPABILITY_SETS.descope);
  });

  it("reads a deployment with nothing optional", () => {
    expect(parseCapabilities({ capabilities: [], relationship_store: "none" })).toEqual(CAPABILITY_SETS.none);
  });

  it("drops a capability or a store kind the SPA does not know", () => {
    expect(parseCapabilities({ capabilities: ["teleport", "machine_credentials"], relationship_store: "keto" })).toEqual(
      CAPABILITY_SETS.machineCredentialsOnly,
    );
  });

  it.each([null, "nope", 3, [], {}, { capabilities: "machine_credentials" }])("fails closed on %j", (body) => {
    expect(parseCapabilities(body)).toEqual(NO_CAPABILITIES);
  });
});

describe("servedBy", () => {
  it.each([
    ["descope", { machine_credentials: true, custom_attributes: true, relationship_store: true }],
    ["none", { machine_credentials: false, custom_attributes: false, relationship_store: false }],
    ["machineCredentialsOnly", { machine_credentials: true, custom_attributes: false, relationship_store: false }],
    ["customAttributesOnly", { machine_credentials: false, custom_attributes: true, relationship_store: false }],
    ["relationshipStoreOnly", { machine_credentials: false, custom_attributes: false, relationship_store: true }],
  ] as const)("%s", (name, expected) => {
    const set = CAPABILITY_SETS[name];
    expect({
      machine_credentials: servedBy(set, "machine_credentials"),
      custom_attributes: servedBy(set, "custom_attributes"),
      relationship_store: servedBy(set, "relationship_store"),
    }).toEqual(expected);
  });
});
