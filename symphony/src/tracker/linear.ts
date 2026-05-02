import type { Issue, TrackerConfigSchema } from "../types.js";
import type { z } from "zod";

type TrackerConfig = z.infer<typeof TrackerConfigSchema>;

const ENDPOINT = "https://api.linear.app/graphql";
const PAGE_SIZE = 50;
const TIMEOUT_MS = 30_000;

interface RawIssue {
  id: string;
  identifier: string;
  title: string;
  description: string | null;
  url: string;
  priority: number | null;
  createdAt: string;
  updatedAt: string;
  state: { name: string };
  labels: { nodes: { name: string }[] };
  inverseRelations: {
    nodes: { type: string; issue: { id: string; identifier: string; state: { name: string; type: string } } }[];
  };
}

function normalize(raw: RawIssue, terminalStates: Set<string>): Issue {
  const blocked_by: string[] = [];
  for (const rel of raw.inverseRelations?.nodes ?? []) {
    if (rel.type === "blocks") {
      const stateName = rel.issue.state?.name?.toLowerCase() ?? "";
      const stateType = rel.issue.state?.type?.toLowerCase() ?? "";
      const isTerminal =
        terminalStates.has(stateName) ||
        stateType === "completed" ||
        stateType === "canceled";
      if (!isTerminal) blocked_by.push(rel.issue.identifier);
    }
  }
  return {
    id: raw.id,
    identifier: raw.identifier,
    title: raw.title,
    description: raw.description,
    state: raw.state.name,
    priority: Number.isInteger(raw.priority) ? raw.priority : null,
    labels: (raw.labels?.nodes ?? []).map((l) => l.name.toLowerCase()),
    blocked_by,
    url: raw.url,
    created_at: raw.createdAt,
    updated_at: raw.updatedAt,
  };
}

const ISSUE_FIELDS = `
  id identifier title description url priority createdAt updatedAt
  state { name type }
  labels { nodes { name } }
  inverseRelations {
    nodes { type issue { id identifier state { name type } } }
  }
`;

async function gql<T>(apiKey: string, query: string, variables: Record<string, unknown>): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json", Authorization: apiKey },
      body: JSON.stringify({ query, variables }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`linear ${res.status}: ${text.slice(0, 500)}`);
    }
    const json = (await res.json()) as { data?: T; errors?: unknown };
    if (json.errors) throw new Error(`linear graphql errors: ${JSON.stringify(json.errors)}`);
    if (!json.data) throw new Error("linear graphql empty data");
    return json.data;
  } finally {
    clearTimeout(timer);
  }
}

export class LinearClient {
  constructor(private cfg: TrackerConfig) {}

  private terminalSet(): Set<string> {
    return new Set(this.cfg.terminal_states.map((s) => s.toLowerCase()));
  }

  async fetchCandidateIssues(): Promise<Issue[]> {
    return this.fetchByStates(this.cfg.active_states);
  }

  async fetchByStates(states: string[]): Promise<Issue[]> {
    // Project filter takes precedence over team filter when both are set.
    const useProject = !!this.cfg.project_slug;
    const filterFragment = useProject
      ? `project: { slugId: { eq: $scope } } state: { name: { in: $states } }`
      : `team: { key: { eq: $scope } } state: { name: { in: $states } }`;
    const query = `
      query Candidates($scope: String!, $states: [String!], $first: Int!, $after: String) {
        issues(first: $first, after: $after, filter: { ${filterFragment} }) {
          nodes { ${ISSUE_FIELDS} }
          pageInfo { hasNextPage endCursor }
        }
      }
    `;
    const out: Issue[] = [];
    let after: string | null = null;
    const terminal = this.terminalSet();
    const excludeLabels = new Set(this.cfg.exclude_labels.map((s) => s.toLowerCase()));
    type Page = { issues: { nodes: RawIssue[]; pageInfo: { hasNextPage: boolean; endCursor: string | null } } };
    while (true) {
      const data: Page = await gql<Page>(this.cfg.api_key, query, {
        scope: useProject ? this.cfg.project_slug : this.cfg.team_key,
        states,
        first: PAGE_SIZE,
        after,
      });
      for (const n of data.issues.nodes) {
        const norm = normalize(n, terminal);
        if (excludeLabels.size > 0 && norm.labels.some((l) => excludeLabels.has(l))) continue;
        out.push(norm);
      }
      if (!data.issues.pageInfo.hasNextPage) break;
      after = data.issues.pageInfo.endCursor;
    }
    return out;
  }

  async fetchStatesByIds(ids: string[]): Promise<Map<string, string>> {
    if (ids.length === 0) return new Map();
    const query = `
      query States($ids: [ID!]!) {
        issues(filter: { id: { in: $ids } }, first: 250) {
          nodes { id state { name } }
        }
      }
    `;
    const data = await gql<{ issues: { nodes: { id: string; state: { name: string } }[] } }>(
      this.cfg.api_key,
      query,
      { ids },
    );
    const m = new Map<string, string>();
    for (const n of data.issues.nodes) m.set(n.id, n.state.name);
    return m;
  }

  async markIssueDone(issueId: string): Promise<{ ok: boolean; reason?: string }> {
    // Find the team's "completed"-type state, then update the issue.
    const data = await gql<{
      issue: { team: { states: { nodes: { id: string; type: string; name: string }[] } } };
    }>(
      this.cfg.api_key,
      `query($id: String!) { issue(id: $id) { team { states { nodes { id type name } } } } }`,
      { id: issueId },
    );
    const states = data.issue.team.states.nodes;
    const done =
      states.find((s) => s.type === "completed" && s.name === "Done") ??
      states.find((s) => s.type === "completed");
    if (!done) return { ok: false, reason: "no completed state on team" };
    const update = await gql<{ issueUpdate: { success: boolean } }>(
      this.cfg.api_key,
      `mutation($id: String!, $stateId: String!) { issueUpdate(id: $id, input: { stateId: $stateId }) { success } }`,
      { id: issueId, stateId: done.id },
    );
    return { ok: update.issueUpdate.success };
  }

  async fetchIssuesByIds(ids: string[]): Promise<Issue[]> {
    if (ids.length === 0) return [];
    const query = `
      query ByIds($ids: [ID!]!) {
        issues(filter: { id: { in: $ids } }, first: 250) {
          nodes { ${ISSUE_FIELDS} }
        }
      }
    `;
    const data = await gql<{ issues: { nodes: RawIssue[] } }>(this.cfg.api_key, query, { ids });
    const terminal = this.terminalSet();
    return data.issues.nodes.map((n) => normalize(n, terminal));
  }
}
