"""Approved actions using existing memex/GitHub infrastructure."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from briefing_plan import public_research_question, safe_text

GITHUB_API = "https://api.github.com"
_ACTION_ID = re.compile(r"^[a-f0-9]{24,64}$")


class ActionError(RuntimeError):
    """Safe action configuration/protocol error."""


class ActionGateway:
    def __init__(
        self, *, client: httpx.Client, token: str, repo: str,
        memex_url: str | None, chat_id: int,
    ) -> None:
        if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ActionError("actions_not_configured")
        self.client = client
        self.repo = repo
        self.memex_url = memex_url
        self.chat_id = chat_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def __call__(self, proposal: dict[str, Any], reconcile: bool = False) -> dict[str, Any]:
        identifier = proposal.get("action_id", "")
        if not _ACTION_ID.fullmatch(identifier):
            raise ActionError("invalid_action_identifier")
        action = proposal["action"]
        if action["kind"] == "research":
            if reconcile:
                return self._research_status(proposal)
            return self._research_start(proposal)
        if action["kind"] not in {"create_task", "update_task"}:
            raise ActionError("unsupported_action")
        if reconcile and self._repo_url((proposal.get("result") or {}).get("pr_url"), "pull"):
            return self._task_status(proposal)
        if not self.memex_url:
            return {"status": "failed", "error": "task_actions_not_configured"}
        url = urlsplit(self.memex_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password:
            raise ActionError("unsafe_action_endpoint")
        payload: dict[str, Any] = {
            "version": 1, "vault_id": "mindMe", "chat_id": self.chat_id,
            "action_id": identifier, "kind": action["kind"],
        }
        if action["kind"] == "create_task":
            payload["text"] = safe_text(action["text"])
        else:
            payload.update(
                path=action["path"], expected_revision=proposal["source_revision"],
                change=action["change"],
            )
        try:
            response = self.client.post(self.memex_url, json=payload, follow_redirects=False)
            if response.status_code >= 500:
                return {"status": "unknown", "error": "task_service_unavailable"}
            if response.status_code not in {200, 201, 202, 409}:
                return {"status": "failed", "error": "task_service_rejected"}
            result = response.json()
            if (
                not isinstance(result, dict)
                or result.get("action_id") != identifier
                or result.get("status") not in {"submitted", "merged", "conflict", "failed", "in_progress"}
            ):
                raise ActionError("invalid_task_receipt")
            if result.get("pr_url") and not self._repo_url(result["pr_url"], "pull"):
                raise ActionError("invalid_result_link")
            return result
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "error": "task_result_unconfirmed"}

    def save_review(self, identifier: str, review: dict[str, Any], source_revision: str) -> dict[str, Any]:
        if not _ACTION_ID.fullmatch(identifier) or not self.memex_url or not re.fullmatch(r"[a-f0-9]{40}", source_revision):
            raise ActionError("review_actions_not_configured")
        url = urlsplit(self.memex_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password:
            raise ActionError("unsafe_action_endpoint")
        payload = {
            "version": 1, "vault_id": "mindMe", "chat_id": self.chat_id,
            "action_id": identifier, "kind": "save_review", "review": review,
            "source_revision": source_revision,
        }
        if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 49_152:
            raise ActionError("review_payload_capacity")
        try:
            response = self.client.post(self.memex_url, json=payload, follow_redirects=False, timeout=30.0)
            if response.status_code not in {200, 201, 202, 409, 503}:
                raise ActionError("review_service_unavailable")
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise ActionError("review_result_unconfirmed") from None
        if (
            not isinstance(result, dict) or result.get("action_id") != identifier
            or result.get("status") not in {"submitted", "merged", "closed", "conflict", "failed", "in_progress"}
        ):
            raise ActionError("invalid_review_receipt")
        if result.get("status") in {"submitted", "merged"} and not self._repo_url(result.get("pr_url"), "pull"):
            raise ActionError("invalid_review_result_link")
        return result

    def _task_status(self, proposal: dict[str, Any]) -> dict[str, Any]:
        result = proposal["result"]
        number = result["pr_url"].rsplit("/", 1)[-1]
        path = result.get("path")
        action = proposal["action"]
        if action["kind"] == "update_task":
            expected_path = action["path"]
            if action.get("change", {}).get("status") == "done":
                expected_path = "tasks/done/" + expected_path.rsplit("/", 1)[-1]
            valid_path = path == expected_path
        else:
            valid_path = isinstance(path, str) and bool(re.fullmatch(
                rf"tasks/\d{{4}}-\d{{2}}-\d{{2}}-action-{re.escape(proposal['action_id'])}\.md",
                path,
            ))
        if not valid_path:
            raise ActionError("invalid_task_result_path")
        try:
            response = self.client.get(
                f"{GITHUB_API}/repos/{self.repo}/pulls/{number}",
                headers=self.headers, follow_redirects=False,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ActionError("invalid_task_status")
            if data.get("merged") is not True:
                return {**result, "status": "submitted"}
            files = self.client.get(
                f"{GITHUB_API}/repos/{self.repo}/pulls/{number}/files",
                params={"per_page": 100}, headers=self.headers, follow_redirects=False,
            )
            files.raise_for_status()
            changed = files.json()
            if not isinstance(changed, list):
                raise ActionError("invalid_task_status")
            target = next((item for item in changed if item.get("filename") == path), None)
            if target is None or target.get("status") == "removed":
                return {**result, "status": "conflict", "error": "expected_task_change_missing"}
            canonical = self.client.get(
                f"{GITHUB_API}/repos/{self.repo}/contents/{path}",
                headers=self.headers, follow_redirects=False,
            )
            if canonical.status_code == 404:
                return {**result, "status": "conflict", "error": "canonical_task_missing"}
            canonical.raise_for_status()
            current = canonical.json()
            if not isinstance(current, dict) or current.get("sha") != target.get("sha"):
                return {**result, "status": "conflict", "error": "canonical_task_changed"}
            if proposal["action"].get("change", {}).get("status") == "done":
                original = self.client.get(
                    f"{GITHUB_API}/repos/{self.repo}/contents/{proposal['source_path']}",
                    headers=self.headers, follow_redirects=False,
                )
                if original.status_code != 404:
                    original.raise_for_status()
                    return {**result, "status": "conflict", "error": "open_task_still_present"}
            return {**result, "status": "merged"}
        except (httpx.HTTPError, ValueError):
            return {**result, "status": "unknown", "error": "canonical_task_unavailable"}

    def _repo_url(self, value: object, kind: str) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(
            rf"https://github\.com/{re.escape(self.repo)}/{kind}/\d+", value,
        ))

    def _research_start(self, proposal: dict[str, Any]) -> dict[str, Any]:
        question = public_research_question(proposal["action"]["text"])
        marker = f"mindme-action:{proposal['action_id']}"
        payload = {
            "title": "[dig] " + question[:100],
            "body": (
                f"<!-- {marker} -->\n"
                "Owner-approved bounded public research from a mindMe proposal.\n\n"
                f"## Question\n{question}\n\n"
                "## Hard scope\n"
                "- Answer only this question with at most five primary/public sources.\n"
                "- One short report, at most 1,200 words, with source links and uncertainties.\n"
                "- Do not disclose private vault material in external queries.\n"
                "- Do not create follow-on jobs, tasks or research requests.\n"
                "- No code, deployments, purchases or account changes.\n"
                "- Follow the repository's privacy and research-quality rules; these scope limits take precedence over a broader default research tier.\n"
                "- Submit the report under areas/agents/research/ in one reviewable PR.\n"
                f"- Include {marker} in the PR body so its result can be reconciled.\n"
                "- Link that PR in a comment on this issue. Completion requires a merged report, not just closing this issue.\n"
            ),
            "labels": ["dig"],
        }
        try:
            response = self.client.post(
                f"{GITHUB_API}/repos/{self.repo}/issues",
                headers=self.headers, json=payload, follow_redirects=False,
            )
            if response.status_code != 201:
                return {
                    "status": "failed" if 400 <= response.status_code < 500 else "unknown",
                    "error": "research_submission_unconfirmed",
                }
            data = response.json()
            if not isinstance(data, dict) or not self._repo_url(data.get("html_url"), "issues"):
                return {"status": "unknown", "error": "research_receipt_invalid"}
            return {"status": "submitted", "issue_url": data["html_url"]}
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "error": "research_result_unconfirmed"}

    def _research_status(self, proposal: dict[str, Any]) -> dict[str, Any]:
        issue_url = (proposal.get("result") or {}).get("issue_url")
        try:
            if not self._repo_url(issue_url, "issues"):
                response = self.client.get(
                    f"{GITHUB_API}/search/issues",
                    params={"q": f'repo:{self.repo} "mindme-action:{proposal["action_id"]}" in:body', "per_page": 5},
                    headers=self.headers, follow_redirects=False,
                )
                response.raise_for_status()
                data = response.json()
                matches = [
                    item for item in data.get("items", [])
                    if f"mindme-action:{proposal['action_id']}" in (item.get("body") or "")
                    and self._repo_url(item.get("html_url"), "issues")
                ]
                if len(matches) != 1:
                    return {"status": "unknown", "error": "research_not_reconciled"}
                issue_url = matches[0]["html_url"]
            number = issue_url.rsplit("/", 1)[-1]
            response = self.client.get(
                f"{GITHUB_API}/repos/{self.repo}/issues/{number}/comments",
                params={"per_page": 30}, headers=self.headers, follow_redirects=False,
            )
            response.raise_for_status()
            comments = response.json()
            if not isinstance(comments, list):
                raise ActionError("invalid_research_status")
            pr_numbers = []
            for comment in comments:
                for number in re.findall(
                    rf"https://github\.com/{re.escape(self.repo)}/pull/(\d+)",
                    comment.get("body") or "",
                ):
                    if number not in pr_numbers:
                        pr_numbers.append(number)
            for number in pr_numbers[:3]:
                pr = self.client.get(
                    f"{GITHUB_API}/repos/{self.repo}/pulls/{number}",
                    headers=self.headers, follow_redirects=False,
                )
                pr.raise_for_status()
                pr_data = pr.json()
                if (
                    pr_data.get("merged") is not True
                    or f"mindme-action:{proposal['action_id']}" not in (pr_data.get("body") or "")
                ):
                    continue
                files = self.client.get(
                    f"{GITHUB_API}/repos/{self.repo}/pulls/{number}/files",
                    params={"per_page": 30}, headers=self.headers, follow_redirects=False,
                )
                files.raise_for_status()
                for file in files.json():
                    path = file.get("filename") or ""
                    if (
                        path.startswith("areas/agents/research/")
                        and path.endswith(".md") and file.get("status") != "removed"
                        and ".." not in path and ".private." not in path and ".full." not in path
                    ):
                        canonical = self.client.get(
                            f"{GITHUB_API}/repos/{self.repo}/contents/{path}",
                            headers=self.headers, follow_redirects=False,
                        )
                        if canonical.status_code == 404:
                            continue
                        canonical.raise_for_status()
                        if canonical.json().get("type") != "file":
                            continue
                        return {
                            "status": "merged", "issue_url": issue_url,
                            "pr_url": f"https://github.com/{self.repo}/pull/{number}", "path": path,
                        }
            return {"status": "submitted", "issue_url": issue_url}
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "error": "research_status_unavailable"}
