"""Isolated real HTTP workbench for Playwright; never used by production CLI.

Only the model provider is replaced. REST, CSRF, SSE, permissions, mutations,
SessionStore, subprocess verification and the built SPA are the real runtime.
"""

import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

import uvicorn

from ai.events import StreamEnd, StreamStart
from ai.messages import AssistantMessage
from ai.models import Model
from coding_agent.web.app import create_app
from coding_agent.web.config import ProviderProfileConfig, ProviderRegistry, parse_allowed_root


class FakeProvider:
    async def stream(self, model, context, tools):
        yield StreamStart()
        if "planning phase" in context.system_prompt:
            response = {
                "objective": "Fix value safely", "constraints": [], "revision": 1,
                "steps": [{"id": "fix", "objective": "Return the expected value",
                           "target_files": ["app.py"], "evidence_queries": ["return 1"],
                           "acceptance_criteria": ["tests pass"]}],
                "acceptance_criteria": ["tests pass"],
            }
        else:
            response = {"operations": [{"kind": "edit_file", "path": "app.py",
                "replacements": [{"old_text": "return 1", "new_text": "return 2"}]}]}
        yield StreamEnd(message=AssistantMessage(content=json.dumps(response)))


class FakeRegistry(ProviderRegistry):
    def __init__(self):
        super().__init__([
            ProviderProfileConfig(id=profile, label=label, base_url="https://unused.invalid/v1",
                api_key_env="MINI_PI_E2E_PROVIDER_KEY", models=[model], default_model=model)
            for profile, label, model in [
                ("default", "Default Fake", "default-model"),
                ("active", "Active Fake", "active-model"),
            ]
        ])

    def create(self, profile_id, model_id):
        if not self.has(profile_id, model_id):
            raise ValueError("unknown test profile/model")
        return FakeProvider(), Model(id=model_id, provider=profile_id)


def main():
    os.environ["MINI_PI_E2E_PROVIDER_KEY"] = "sk-test-only-server-sentinel"
    with tempfile.TemporaryDirectory(prefix="mini-pi-browser-") as temporary:
        root = Path(temporary)
        other = root / "other"
        projects = root / "projects"
        other.mkdir()
        projects.mkdir()
        for name in ("success", "denied", "cancelled", "refresh", "hard-denied", "l2-unavailable", "offline"):
            workspace = projects / name
            (workspace / "tests").mkdir(parents=True)
            (workspace / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            (workspace / "tests/test_app.py").write_text(
                "import os\nfrom app import value\n\ndef test_value():\n"
                "    assert value() == 2\n"
                "    assert 'MINI_PI_E2E_PROVIDER_KEY' not in os.environ\n",
                encoding="utf-8",
            )
            if name in {"hard-denied", "l2-unavailable"}:
                (workspace / ".mini_pi").mkdir()
                settings = ({"rules": [{"id": "deny-write", "effect": "deny", "tool": "workspace_mutation"}]}
                    if name == "hard-denied" else {"sandbox": {"level": "L2", "engine": "docker",
                        "image": "mini-pi/nonexistent-e2e@sha256:" + "0" * 64}})
                (workspace / ".mini_pi/settings.json").write_text(json.dumps(settings), encoding="utf-8")
        executable = ("& '" + sys.executable.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(sys.executable))
        app = create_app(
            roots=[parse_allowed_root(f"Other={other}"), parse_allowed_root(f"Projects={projects}")],
            providers=FakeRegistry(),
            verify_command=executable + " -m pytest -q -p no:cacheprovider",
            static_dir=Path(__file__).resolve().parents[2] / "web/dist",
            state_root=root / "private-state",
        )
        uvicorn.run(app, host="127.0.0.1", port=8876, access_log=False)


if __name__ == "__main__":
    main()
