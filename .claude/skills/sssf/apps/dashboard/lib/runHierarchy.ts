import { runHref } from "@/lib/href";
import { isInFlight } from "@/lib/runVisibility";
import type { MaestroNode, MaestroRunDetail } from "@/lib/types";

export type RunHierarchyRole =
  | "builder"
  | "tester"
  | "test-reviewer"
  | "code-reviewer"
  | "integration-reviewer"
  | "reviewer";

export interface RunHierarchyAgent {
  id: string;
  label: string;
  role: RunHierarchyRole;
  state: string;
  href: string;
}

export interface RunHierarchyLane {
  id: string;
  label: string;
  state: string;
  href: string;
  agents: RunHierarchyAgent[];
}

export interface RunHierarchy {
  label: string;
  is_live: boolean;
  href: string;
  agents: RunHierarchyAgent[];
  lanes: RunHierarchyLane[];
}

function laneHref(sourceId: string, runId: string, nodeId: string): string {
  return runHref(sourceId, runId, `/lanes/${encodeURIComponent(nodeId)}`);
}

function attemptLabel(role: "builder" | "tester", node: MaestroNode): string {
  return node.attempt_no > 0 ? `${role}-a${node.attempt_no}` : role;
}

const FACTORY_TEST_ROLE_STAGES: ReadonlyArray<
  readonly [RunHierarchyRole, string]
> = [
  ["tester", "WRITING_TESTS"],
  ["test-reviewer", "REVIEWING_TESTS"],
];

const FACTORY_BUILD_ROLE_STAGES: ReadonlyArray<
  readonly [RunHierarchyRole, string]
> = [
  ["builder", "BUILDING"],
  ["code-reviewer", "REVIEWING_CODE"],
];

const FACTORY_LEGACY_ROLE_STAGES = [
  ...FACTORY_TEST_ROLE_STAGES,
  ...FACTORY_BUILD_ROLE_STAGES,
];

const FACTORY_STAGE_ORDER = [
  "PLANNED",
  "WRITING_TESTS",
  "REVIEWING_TESTS",
  "TESTS_SEALED",
  "BUILDING",
  "REVIEWING_CODE",
  "READY_TO_MERGE",
  "MERGED",
];

function factoryRoleState(roleStage: string, laneStage: string): string {
  if (roleStage === laneStage) return "ACTIVE";
  const roleIndex = FACTORY_STAGE_ORDER.indexOf(roleStage);
  const laneIndex = FACTORY_STAGE_ORDER.indexOf(laneStage);
  return roleIndex >= 0 && laneIndex > roleIndex ? "COMPLETE" : "WAITING";
}

function factoryLaneRoleStages(
  kind: string | null,
): ReadonlyArray<readonly [RunHierarchyRole, string]> {
  if (kind === "tests") return FACTORY_TEST_ROLE_STAGES;
  if (kind === "build") return FACTORY_BUILD_ROLE_STAGES;
  return FACTORY_LEGACY_ROLE_STAGES;
}

function factoryFinalReviewState(runState: string): string {
  if (runState === "INTEGRATION_REVIEW_PENDING") return "ACTIVE";
  if (runState === "PUBLISHABLE" || runState === "COMPLETE") return "COMPLETE";
  return "WAITING";
}

function projectArtifactFactoryHierarchy(
  run: MaestroRunDetail,
  sourceId: string,
): RunHierarchy {
  const href = runHref(sourceId, run.run_id);
  return {
    label: run.plan_name ?? run.run_id,
    is_live: isInFlight(run.state),
    href,
    agents: [
      {
        id: `${run.run_id}:integration-reviewer`,
        label: "integration-reviewer",
        role: "integration-reviewer",
        state: factoryFinalReviewState(run.state),
        href,
      },
    ],
    lanes: run.nodes.map((lane) => ({
      id: lane.node_id,
      label: lane.node_id,
      state: lane.lane_phase ?? lane.state,
      href: laneHref(sourceId, run.run_id, lane.node_id),
      agents: factoryLaneRoleStages(lane.kind).map(([role, stage]) => ({
        id: `${lane.node_id}:${role}`,
        label: role,
        role,
        state: factoryRoleState(stage, lane.lane_phase ?? lane.state),
        href: laneHref(sourceId, run.run_id, lane.node_id),
      })),
    })),
  };
}

/** Project the persisted runtime hierarchy used by Herdr: plan → lane → actor. */
export function projectRunHierarchy(
  run: MaestroRunDetail,
  sourceId: string,
): RunHierarchy {
  if (run.schema_version === "artifact-factory.v1") {
    return projectArtifactFactoryHierarchy(run, sourceId);
  }
  const buildNodes = run.nodes.filter((node) => node.kind === "agent");
  const testNodes = run.nodes.filter((node) => node.kind === "tests");
  const reviewNodes = run.nodes.filter(
    (node) => node.kind === "review" || node.node_id.endsWith("::review"),
  );
  const buildById = new Map(buildNodes.map((node) => [node.node_id, node]));
  const testOwners = new Map<string, string[]>();

  for (const test of testNodes) testOwners.set(test.node_id, []);
  for (const build of buildNodes) {
    for (const dependency of build.needs) {
      const owners = testOwners.get(dependency);
      if (owners) owners.push(build.node_id);
    }
  }

  const laneOrder = buildNodes.map((node) => node.node_id);
  for (const test of testNodes) {
    const owners = testOwners.get(test.node_id) ?? [];
    if (owners.length !== 1) laneOrder.push(test.node_id);
  }

  const testsByLane = new Map<string, MaestroNode[]>();
  for (const test of testNodes) {
    const owners = testOwners.get(test.node_id) ?? [];
    const laneId = owners.length === 1 ? owners[0] : test.node_id;
    const tests = testsByLane.get(laneId);
    if (tests) tests.push(test);
    else testsByLane.set(laneId, [test]);
  }

  const reviewsByLane = new Map<string, MaestroNode[]>();
  for (const review of reviewNodes) {
    const laneId = review.node_id.endsWith("::review")
      ? review.node_id.slice(0, -"::review".length)
      : review.needs.find((dependency) => buildById.has(dependency));
    if (!laneId || !buildById.has(laneId)) continue;
    const reviews = reviewsByLane.get(laneId);
    if (reviews) reviews.push(review);
    else reviewsByLane.set(laneId, [review]);
  }

  const latestActors = new Map<
    string,
    MaestroRunDetail["actor_sessions"][number]
  >();
  for (const actor of run.actor_sessions ?? []) {
    const key = `${actor.build_node_id}:${actor.actor_role}`;
    const current = latestActors.get(key);
    if (!current || actor.generation > current.generation) {
      latestActors.set(key, actor);
    }
  }

  const lanes = laneOrder.map((laneId): RunHierarchyLane => {
    const build = buildById.get(laneId);
    const tests = testsByLane.get(laneId) ?? [];
    const agents: RunHierarchyAgent[] = [];
    for (const test of tests) {
      agents.push({
        id: test.node_id,
        label: attemptLabel("tester", test),
        role: "tester",
        state: test.state,
        href: laneHref(sourceId, run.run_id, test.node_id),
      });
    }
    if (build) {
      const actor = latestActors.get(`${laneId}:builder`);
      agents.push({
        id: actor
          ? `${build.node_id}:builder:${actor.generation}`
          : build.node_id,
        label: actor
          ? `builder-a${actor.generation}`
          : attemptLabel("builder", build),
        role: "builder",
        state: actor?.state ?? build.state,
        href: laneHref(sourceId, run.run_id, build.node_id),
      });
    }
    const reviewNodesForLane = reviewsByLane.get(laneId) ?? [];
    const reviewer = latestActors.get(`${laneId}:reviewer`);
    if (reviewer) {
      const reviewNode = reviewNodesForLane.at(-1);
      agents.push({
        id: `${laneId}:reviewer:${reviewer.generation}`,
        label: `reviewer-a${reviewer.generation}`,
        role: "reviewer",
        state: reviewer.state,
        href: laneHref(
          sourceId,
          run.run_id,
          reviewNode?.node_id ?? laneId,
        ),
      });
    } else {
      for (const review of reviewNodesForLane) {
        agents.push({
          id: review.node_id,
          label: "reviewer",
          role: "reviewer",
          state: review.state,
          href: laneHref(sourceId, run.run_id, review.node_id),
        });
      }
    }
    const authority = build ?? tests[0];
    return {
      id: laneId,
      label: laneId,
      state: build?.lane_phase ?? authority?.state ?? "PENDING",
      href: laneHref(sourceId, run.run_id, laneId),
      agents,
    };
  });

  return {
    label: run.plan_name ?? run.run_id,
    is_live: isInFlight(run.state),
    href: runHref(sourceId, run.run_id),
    lanes,
    agents: [],
  };
}
