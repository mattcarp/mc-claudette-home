import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reviewer import _is_openai_provider, _is_reasoning_model, _openai_model_name, llm_review_diff, review_worktree


class ReviewerModelNameTests(unittest.TestCase):
    def test_strips_openai_prefix(self):
        self.assertEqual(_openai_model_name("openai/gpt-5.5"), "gpt-5.5")

    def test_non_openai_prefix_unchanged(self):
        # Only openai/ prefix is stripped; deepseek/ passes through unchanged
        self.assertEqual(_openai_model_name("deepseek/deepseek-v4-pro"), "deepseek/deepseek-v4-pro")

    def test_bare_name_unchanged(self):
        self.assertEqual(_openai_model_name("gpt-4o"), "gpt-4o")


class IsOpenAIProviderTests(unittest.TestCase):
    def test_openai_prefixed(self):
        self.assertTrue(_is_openai_provider("openai/gpt-5.5"))

    def test_bare_name_is_openai(self):
        self.assertTrue(_is_openai_provider("gpt-4o"))

    def test_deepseek_not_openai(self):
        self.assertFalse(_is_openai_provider("deepseek/deepseek-v4-pro"))

    def test_anthropic_not_openai(self):
        self.assertFalse(_is_openai_provider("anthropic/claude-opus-4"))

    def test_gemini_not_openai(self):
        self.assertFalse(_is_openai_provider("google/gemini-2.5-pro"))


class IsReasoningModelTests(unittest.TestCase):
    def test_gpt55_is_reasoning(self):
        self.assertTrue(_is_reasoning_model("gpt-5.5"))

    def test_o1_is_reasoning(self):
        self.assertTrue(_is_reasoning_model("o1-preview"))

    def test_o3_is_reasoning(self):
        self.assertTrue(_is_reasoning_model("o3-mini"))

    def test_gpt4o_not_reasoning(self):
        self.assertFalse(_is_reasoning_model("gpt-4o"))

    def test_gpt4o_mini_not_reasoning(self):
        self.assertFalse(_is_reasoning_model("gpt-4o-mini"))

    def test_reasoning_model_omits_temperature(self):
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "PASS: looks good"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client):
                llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="openai/gpt-5.5",
                )

        call_kwargs = fake_client.chat.completions.create.call_args[1]
        self.assertNotIn("temperature", call_kwargs)


class PathSanityTests(unittest.TestCase):
    def test_review_worktree_rejects_added_path_with_spaces(self):
        """Worker creating a file literally named e.g. 'node --test foo.mjs'
        should fail the deterministic gate. MAG-48 hit this exact failure mode."""
        from reviewer import review_worktree
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=cwd, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=cwd, check=True)
            (cwd / "ok.txt").write_text("seed\n")
            subprocess.run(["git", "add", "."], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=cwd, check=True)
            (cwd / "node --test foo.mjs").write_text("oops\n")
            subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "bad"], cwd=cwd, check=True)
            result = review_worktree(cwd)
        self.assertFalse(result.ok)
        self.assertIn("suspicious", result.reason.lower())

    def test_review_worktree_rejects_added_path_starting_with_command_word(self):
        from reviewer import review_worktree
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=cwd, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=cwd, check=True)
            (cwd / "ok.txt").write_text("seed\n")
            subprocess.run(["git", "add", "."], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=cwd, check=True)
            (cwd / "npm-install-output.log").write_text("ok\n")  # legit
            subprocess.run(["git", "add", "."], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "ok"], cwd=cwd, check=True)
            result = review_worktree(cwd)
        # Non-suspicious because the dash separates words; only spaces / shell
        # leading tokens are rejected.
        self.assertTrue(result.ok)


class LLMReviewDiffTests(unittest.TestCase):
    def test_skip_when_no_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = llm_review_diff(Path("/tmp"), "title", "desc", "output")
        self.assertFalse(result.ok)
        self.assertIn("API key", result.reason)

    def test_uses_openrouter_base_url_and_key_when_set(self):
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "PASS: looks good"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        env = {
            "OPENROUTER_API_KEY": "sk-or-v1-test",
            "LANGGRAPH_REVIEWER_BASE_URL": "https://openrouter.ai/api/v1",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client) as mock_openai:
                llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="openai/gpt-5.5",
                )

        mock_openai.assert_called_once()
        kwargs = mock_openai.call_args.kwargs
        self.assertEqual(kwargs.get("api_key"), "sk-or-v1-test")
        self.assertEqual(kwargs.get("base_url"), "https://openrouter.ai/api/v1")

    def test_explicit_reviewer_api_key_env_takes_precedence(self):
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "PASS: looks good"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        env = {
            "OPENAI_API_KEY": "sk-openai-direct",
            "OPENROUTER_API_KEY": "sk-or-router",
            "LANGGRAPH_REVIEWER_API_KEY": "sk-explicit-override",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client) as mock_openai:
                llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="openai/gpt-5.5",
                )

        kwargs = mock_openai.call_args.kwargs
        self.assertEqual(kwargs.get("api_key"), "sk-explicit-override")

    def test_non_openai_reviewer_blocked_without_calling_client(self):
        """A deepseek/ reviewer model must fail fast before touching the OpenAI client."""
        fake_client = MagicMock()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client) as mock_openai:
                result = llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="deepseek/deepseek-v4-pro",
                )
        self.assertFalse(result.ok)
        self.assertIn("not an OpenAI model", result.reason)
        mock_openai.assert_not_called()

    def test_gpt55_uses_max_completion_tokens(self):
        """openai/gpt-5.5 (reasoning model) must use max_completion_tokens, not max_tokens."""
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "PASS: looks good"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client):
                llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="openai/gpt-5.5",
                )

        call_kwargs = fake_client.chat.completions.create.call_args[1]
        self.assertNotIn("max_tokens", call_kwargs)
        self.assertGreaterEqual(call_kwargs.get("max_completion_tokens", 0), 512)

    def test_non_reasoning_uses_max_tokens(self):
        """Non-reasoning models (gpt-4o) use max_tokens and temperature."""
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "PASS: looks good"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client):
                llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="openai/gpt-4o",
                )

        call_kwargs = fake_client.chat.completions.create.call_args[1]
        self.assertNotIn("max_completion_tokens", call_kwargs)
        self.assertGreaterEqual(
            call_kwargs.get("max_tokens", call_kwargs.get("max_completion_tokens", 0)),
            512,
        )
        self.assertIn("temperature", call_kwargs)

    def test_pass_on_openai_pass_response(self):
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "PASS: changes look relevant and focused"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client):
                result = llm_review_diff(
                    Path("/tmp"), "docs: add runbook", "Add a runbook.", "worker done",
                    reviewer_model="openai/gpt-5.5",
                )

        self.assertTrue(result.ok)
        self.assertIn("llm_review passed", result.reason)

    def test_prefers_dirty_worker_diff_over_parent_commit_diff(self):
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "PASS: dirty change reviewed"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(("git", "init"), cwd=repo, check=True, capture_output=True)
            subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=repo, check=True)
            subprocess.run(("git", "config", "user.name", "Test User"), cwd=repo, check=True)
            path = repo / "feature.txt"
            path.write_text("base\n", encoding="utf-8")
            subprocess.run(("git", "add", "feature.txt"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, capture_output=True)
            path.write_text("old committed change\n", encoding="utf-8")
            subprocess.run(("git", "add", "feature.txt"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-m", "old change"), cwd=repo, check=True, capture_output=True)
            path.write_text("dirty worker change\n", encoding="utf-8")

            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
                with patch("reviewer.OpenAI", return_value=fake_client):
                    result = llm_review_diff(
                        repo,
                        "title",
                        "desc",
                        "output",
                        reviewer_model="openai/gpt-5.5",
                    )

        self.assertTrue(result.ok)
        prompt = fake_client.chat.completions.create.call_args[1]["messages"][0]["content"]
        self.assertIn("dirty worker change", prompt)
        self.assertNotIn("-base", prompt)

    def test_fail_on_openai_fail_response(self):
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "FAIL: diff is empty, no changes made"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client):
                result = llm_review_diff(
                    Path("/tmp"), "docs: add runbook", "Add a runbook.", "worker done",
                    reviewer_model="openai/gpt-5.5",
                )

        self.assertFalse(result.ok)
        self.assertIn("llm_review failed", result.reason)

    def test_fail_on_openai_exception(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", side_effect=RuntimeError("connection error")):
                result = llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="openai/gpt-5.5",
                )

        self.assertFalse(result.ok)
        self.assertIn("LLM reviewer error", result.reason)

    def test_ambiguous_response_treated_as_fail(self):
        fake_response = MagicMock()
        fake_response.choices[0].message.content = "I'm not sure about this"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with patch("reviewer.OpenAI", return_value=fake_client):
                result = llm_review_diff(
                    Path("/tmp"), "title", "desc", "output",
                    reviewer_model="openai/gpt-5.5",
                )

        self.assertFalse(result.ok)
        self.assertIn("ambiguous", result.reason)


class ReviewWorktreeTests(unittest.TestCase):
    def test_untracked_non_runtime_file_fails_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(("git", "init"), cwd=repo, check=True, capture_output=True)
            subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=repo, check=True)
            subprocess.run(("git", "config", "user.name", "Test User"), cwd=repo, check=True)
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(("git", "add", "base.txt"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, capture_output=True)
            (repo / "new-test.mjs").write_text("test\n", encoding="utf-8")

            result = review_worktree(repo)

        self.assertFalse(result.ok)
        self.assertIn("untracked review file", result.reason)

    def test_committed_trailing_whitespace_fails_even_when_worktree_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(("git", "init"), cwd=repo, check=True, capture_output=True)
            subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=repo, check=True)
            subprocess.run(("git", "config", "user.name", "Test User"), cwd=repo, check=True)

            path = repo / "docs.md"
            path.write_text("clean\n", encoding="utf-8")
            subprocess.run(("git", "add", "docs.md"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, capture_output=True)

            path.write_text("bad trailing whitespace  \n", encoding="utf-8")
            subprocess.run(("git", "add", "docs.md"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-m", "bad whitespace"), cwd=repo, check=True, capture_output=True)

            result = review_worktree(repo)

        self.assertFalse(result.ok)
        self.assertIn("trailing whitespace", result.reason)
