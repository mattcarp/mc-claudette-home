from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from state import LinearIssue

LINEAR_GRAPHQL = "https://api.linear.app/graphql"
TERMINAL_STATES = {"Done", "Canceled", "Cancelled", "Duplicate"}


def active_issues_query() -> str:
    return """
    query ActiveIssues($teamKey: String!, $states: [String!], $first: Int!) {
      issues(
        first: $first,
        filter: { team: { key: { eq: $teamKey } }, state: { name: { in: $states } } }
      ) {
        nodes {
          id
          identifier
          title
          description
          priority
          state { name type }
          labels { nodes { name } }
          inverseRelations {
            nodes {
              type
              issue { identifier state { name type } }
            }
          }
        }
      }
    }
    """


def create_comment_mutation() -> str:
    return """
    mutation CreateComment($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """


def workflow_states_query() -> str:
    return """
    query WorkflowStates($teamKey: String!, $names: [String!]) {
      workflowStates(
        filter: { team: { key: { eq: $teamKey } }, name: { in: $names } }
      ) {
        nodes {
          id
          name
        }
      }
    }
    """


def issue_update_mutation() -> str:
    return """
    mutation IssueUpdate($issueId: String!, $stateId: String!) {
      issueUpdate(id: $issueId, input: { stateId: $stateId }) {
        success
      }
    }
    """


class LinearGraphQLClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY")
        if not self.api_key:
            raise RuntimeError("LINEAR_API_KEY is required")

    def gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            LINEAR_GRAPHQL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("errors"):
            raise RuntimeError(json.dumps(payload["errors"]))
        return payload["data"]

    def fetch_active_issues(self, team_key: str, states: list[str], first: int = 50) -> list[LinearIssue]:
        data = self.gql(active_issues_query(), {"teamKey": team_key, "states": states, "first": first})
        issues: list[LinearIssue] = []
        for node in data["issues"]["nodes"]:
            labels = tuple(label["name"] for label in node.get("labels", {}).get("nodes", []))
            blockers: list[str] = []
            for relation in node.get("inverseRelations", {}).get("nodes", []):
                if relation.get("type") == "blocks":
                    related = relation.get("issue") or {}
                    state = related.get("state") or {}
                    state_name = state.get("name")
                    state_type = (state.get("type") or "").lower()
                    if state_name not in TERMINAL_STATES and state_type not in {"completed", "canceled"}:
                        blockers.append(related.get("identifier", "unknown"))
            issues.append(
                LinearIssue(
                    id=node["id"],
                    identifier=node["identifier"],
                    title=node["title"],
                    description=node.get("description") or "",
                    state=node["state"]["name"],
                    labels=labels,
                    priority=node.get("priority"),
                    blocked_by=tuple(blockers),
                )
            )
        return issues

    def add_comment(self, issue_id: str, body: str) -> None:
        self.gql(create_comment_mutation(), {"issueId": issue_id, "body": body})

    def state_id_for(self, team_key: str, state_name: str) -> str | None:
        data = self.gql(workflow_states_query(), {"teamKey": team_key, "names": [state_name]})
        for node in data.get("workflowStates", {}).get("nodes", []):
            if node.get("name") == state_name:
                return node.get("id")
        return None

    def set_issue_state(self, issue_id: str, state_id: str) -> None:
        self.gql(issue_update_mutation(), {"issueId": issue_id, "stateId": state_id})

    def transition_issue_to_state(self, team_key: str, issue_id: str, state_name: str) -> bool:
        state_id = self.state_id_for(team_key, state_name)
        if not state_id:
            return False
        self.set_issue_state(issue_id, state_id)
        return True
