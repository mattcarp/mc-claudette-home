import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linear_client import active_issues_query, create_comment_mutation, issue_update_mutation, workflow_states_query


class LinearClientTests(unittest.TestCase):
    def test_active_issues_query_requests_needed_fields(self):
        query = active_issues_query()
        self.assertIn("identifier", query)
        self.assertIn("description", query)
        self.assertIn("labels", query)
        self.assertIn("inverseRelations", query)
        self.assertIn("issue { identifier", query)
        self.assertIn("state { name type }", query)

    def test_comment_mutation_is_comment_only(self):
        mutation = create_comment_mutation()
        self.assertIn("commentCreate", mutation)
        self.assertNotIn("issueUpdate", mutation)

    def test_state_lookup_query_filters_by_team_and_name(self):
        query = workflow_states_query()
        self.assertIn("workflowStates", query)
        self.assertIn("team", query)
        self.assertIn("names", query)

    def test_issue_update_mutation_moves_state(self):
        mutation = issue_update_mutation()
        self.assertIn("issueUpdate", mutation)
        self.assertIn("stateId", mutation)
        self.assertNotIn("commentCreate", mutation)
