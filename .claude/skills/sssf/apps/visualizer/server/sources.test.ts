import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { registeredInstallations } from "./sources.ts";

describe("Maestro installation registry", () => {
  let root = "";
  let registry = "";

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "maestro-registry-"));
    registry = join(root, "registry.json");
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  test("reads multiple installations and tolerates missing optional fields", () => {
    writeFileSync(
      registry,
      JSON.stringify({
        schema: "maestro-registry.v1",
        future: { preserved: true },
        installations: [
          {
            database: "/state/one/lifecycle.sqlite3",
            plans_dir: "/repos/one/.maestro/plans",
            repository: "/repos/one",
            state: "/state/one",
          },
          { database: "/state/two/lifecycle.sqlite3", future_field: 7 },
          { database: "", repository: "/ignored" },
          { repository: "/also-ignored" },
        ],
      }),
    );

    expect(registeredInstallations(registry)).toEqual([
      {
        db: "/state/one/lifecycle.sqlite3",
        plansDir: "/repos/one/.maestro/plans",
        repository: "/repos/one",
      },
      {
        db: "/state/two/lifecycle.sqlite3",
        plansDir: null,
        repository: null,
      },
    ]);
  });

  test("malformed registry fails open", () => {
    writeFileSync(registry, "{not-json");
    const warning = spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(registeredInstallations(registry)).toEqual([]);
      expect(warning).toHaveBeenCalledTimes(1);
    } finally {
      warning.mockRestore();
    }
  });
});
