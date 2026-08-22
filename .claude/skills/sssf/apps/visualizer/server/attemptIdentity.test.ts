import { afterEach, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  loadDeclaredRoles,
  parseDeclaredRoles,
  readObservedModel,
  resolveAttemptIdentity,
  roleForNode,
} from "./attemptIdentity.ts";

const scratch: string[] = [];

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "attempt-identity-"));
  scratch.push(dir);
  return dir;
}

afterEach(() => {
  while (scratch.length) {
    const dir = scratch.pop();
    if (dir) rmSync(dir, { recursive: true, force: true });
  }
});

describe("roleForNode", () => {
  test("typed NodeKind wins over an id prefix", () => {
    expect(roleForNode("review-lane", "agent")).toBe("execution");
    expect(roleForNode("author-notes", "agent")).toBe("execution");
    expect(roleForNode("lane-build", "review")).toBe("reviewer");
    expect(roleForNode("lane-build", "tests")).toBe("tester");
    expect(roleForNode("lane-build", "code")).toBeNull();
  });

  test("unknown typed kind is not guessed", () => {
    expect(roleForNode("review-lane", "reviewer")).toBeNull();
    expect(roleForNode("author-notes", "author")).toBeNull();
    expect(roleForNode("tester-1", "tester")).toBeNull();
  });

  test("id prefix is used only when kind is absent", () => {
    expect(roleForNode("review-lane", null)).toBe("reviewer");
    expect(roleForNode("author-notes", "")).toBe("author");
    expect(roleForNode("test-red", null)).toBe("tester");
    expect(roleForNode("lane-p5-gap-policy", null)).toBe("execution");
    expect(roleForNode("misc", null)).toBeNull();
  });
});

describe("parseDeclaredRoles", () => {
  test("strips a trailing comment before quotes", () => {
    const roles = parseDeclaredRoles(
      [
        "execution:",
        '  model: "opus"  # note',
        "  vendor: xai",
        "reviewer:",
        "  model: openai-codex/gpt-5.6-sol",
        "  vendor: openai",
      ].join("\n"),
    );
    expect(roles.execution).toEqual({ model: "opus", vendor: "xai" });
    expect(roles.reviewer).toEqual({
      model: "openai-codex/gpt-5.6-sol",
      vendor: "openai",
    });
  });
});

describe("loadDeclaredRoles", () => {
  test("records the config path that answered", () => {
    const root = tempDir();
    const plansDir = join(root, "adws", "plans");
    const configPath = join(root, "adws", "maestro.config.yaml");
    mkdirSync(plansDir, { recursive: true });
    writeFileSync(
      configPath,
      ["execution:", "  model: xai-oauth/grok-4.6", "  vendor: xai"].join("\n"),
    );
    const loaded = loadDeclaredRoles(join(root, "state", "repo", "lifecycle.sqlite3"), plansDir);
    expect(loaded.path).toBe(configPath);
    expect(loaded.roles.execution?.model).toBe("xai-oauth/grok-4.6");
  });
});

describe("readObservedModel", () => {
  test("a file that fits in the head window is observed in full", () => {
    const dir = tempDir();
    const session = join(dir, "session.jsonl");
    writeFileSync(
      session,
      [
        JSON.stringify({ type: "model_change", model: "first" }),
        JSON.stringify({ type: "model_change", model: "second" }),
      ].join("\n"),
    );
    expect(readObservedModel(session)).toEqual({
      model: "second",
      vendor: null,
      window: "full",
    });
  });

  test("a later tail model_change wins over the head", () => {
    const dir = tempDir();
    const session = join(dir, "session.jsonl");
    const head = `${JSON.stringify({ type: "model_change", model: "head-model" })}\n`;
    const pad = "x".repeat(70 * 1024);
    const tail = `\n${JSON.stringify({ type: "model_change", model: "tail-model" })}\n`;
    writeFileSync(session, head + pad + tail);
    expect(readObservedModel(session)).toEqual({
      model: "tail-model",
      vendor: null,
      window: "head_and_tail",
    });
  });

  test("a head-only answer is labelled head, not observed", () => {
    const dir = tempDir();
    const session = join(dir, "session.jsonl");
    const head = `${JSON.stringify({ type: "model_change", model: "head-only" })}\n`;
    writeFileSync(session, head + "y".repeat(70 * 1024));
    expect(readObservedModel(session)).toEqual({
      model: "head-only",
      vendor: null,
      window: "head",
    });
  });
});

describe("resolveAttemptIdentity", () => {
  test("observed session beats declared config", () => {
    const dir = tempDir();
    const session = join(dir, "session.jsonl");
    writeFileSync(session, `${JSON.stringify({ type: "model_change", model: "live" })}\n`);
    const cache = new Map();
    expect(
      resolveAttemptIdentity(
        session,
        { model: "declared-model", vendor: "xai" },
        cache,
        "/tmp/maestro.config.yaml",
      ),
    ).toEqual({
      model: "live",
      vendor: null,
      model_source: "observed",
      vendor_source: "not_recorded",
      declared_config_path: "/tmp/maestro.config.yaml",
    });
  });

  test("head-window-only model is not labelled observed", () => {
    const dir = tempDir();
    const session = join(dir, "session.jsonl");
    writeFileSync(
      session,
      `${JSON.stringify({ type: "model_change", model: "stale-head" })}\n${"z".repeat(70 * 1024)}`,
    );
    expect(resolveAttemptIdentity(session, null, new Map(), null)).toEqual({
      model: "stale-head",
      vendor: null,
      model_source: "observed_head",
      vendor_source: "not_recorded",
      declared_config_path: null,
    });
  });

  test("declared fallback records the config path", () => {
    expect(
      resolveAttemptIdentity(
        null,
        { model: "opus", vendor: "anthropic" },
        new Map(),
        "/deploy/adws/maestro.config.yaml",
      ),
    ).toEqual({
      model: "opus",
      vendor: "anthropic",
      model_source: "declared",
      vendor_source: "declared",
      declared_config_path: "/deploy/adws/maestro.config.yaml",
    });
  });

  test("nothing recorded stays not_recorded", () => {
    expect(resolveAttemptIdentity(null, null, new Map(), null)).toEqual({
      model: null,
      vendor: null,
      model_source: "not_recorded",
      vendor_source: "not_recorded",
      declared_config_path: null,
    });
  });
});
