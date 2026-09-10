import { describe, expect, test } from "bun:test";

import {
  branchSummary,
  entriesOf,
  isBranch,
  leafText,
  leafTone,
  opensByDefault,
} from "./jsonTree";

describe("isBranch", () => {
  test("objects and arrays are descended into, scalars are not", () => {
    expect(isBranch({ a: 1 })).toBe(true);
    expect(isBranch([1])).toBe(true);
    expect(isBranch("")).toBe(false);
    expect(isBranch(0)).toBe(false);
    expect(isBranch(false)).toBe(false);
  });

  test("null is a leaf, not an empty object", () => {
    expect(isBranch(null)).toBe(false);
  });
});

describe("entriesOf", () => {
  test("keeps the payload's own key order", () => {
    expect(entriesOf({ verdict: "REVISE", findings: [] })).toEqual([
      { key: "verdict", value: "REVISE" },
      { key: "findings", value: [] },
    ]);
  });

  test("names array children by index", () => {
    expect(entriesOf(["a", "b"])).toEqual([
      { key: "0", value: "a" },
      { key: "1", value: "b" },
    ]);
  });
});

describe("branchSummary", () => {
  test("says how much a closed branch is hiding", () => {
    expect(branchSummary({ a: 1, b: 2 })).toBe("2 keys");
    expect(branchSummary({ a: 1 })).toBe("1 key");
    expect(branchSummary(["x"])).toBe("1 item");
    expect(branchSummary(["x", "y"])).toBe("2 items");
  });

  test("an empty branch says so rather than promising content", () => {
    expect(branchSummary([])).toBe("0 items");
    expect(branchSummary({})).toBe("0 keys");
  });
});

describe("leafText", () => {
  test("a string is written as itself, without quotes", () => {
    expect(leafText("refs/maestro/candidates/abc")).toBe("refs/maestro/candidates/abc");
  });

  test("null, false, 0 and an empty string stay distinguishable", () => {
    expect(leafText(null)).toBe("null");
    expect(leafText(false)).toBe("false");
    expect(leafText(0)).toBe("0");
    expect(leafText("")).toBe("");
  });
});

describe("leafTone", () => {
  test("null is its own tone, not an object's", () => {
    expect(leafTone(null)).toBe("null");
    expect(leafTone("a")).toBe("string");
    expect(leafTone(1)).toBe("number");
    expect(leafTone(true)).toBe("boolean");
    expect(leafTone(undefined)).toBe("other");
  });
});

describe("opensByDefault", () => {
  test("the payload's own keys are visible without a click", () => {
    expect(opensByDefault(0, "public_contract")).toBe(true);
  });

  test("a contract or a tree delta stays shut until asked for", () => {
    expect(opensByDefault(1, "0", "public_contract")).toBe(false);
    expect(opensByDefault(1, "0", "tree_delta")).toBe(false);
  });

  test("the first finding opens, because it is why the artifact exists", () => {
    expect(opensByDefault(1, "0", "findings")).toBe(true);
    expect(opensByDefault(1, "0", "advisory_findings")).toBe(true);
  });

  test("only the first finding opens; the rest stay a list", () => {
    expect(opensByDefault(1, "1", "findings")).toBe(false);
    expect(opensByDefault(1, "6", "findings")).toBe(false);
  });

  test("nothing opens itself three levels down", () => {
    expect(opensByDefault(2, "0", "findings")).toBe(false);
  });
});
