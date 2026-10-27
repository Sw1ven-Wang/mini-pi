"""Web tests never write to the user's real persistent runtime directory."""

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path_factory, monkeypatch):
    from coding_agent.runtime.kernel import RuntimeKernel
    state = tmp_path_factory.mktemp("host-runtime")
    monkeypatch.setattr("coding_agent.web.tasks.default_state_root", lambda: state)
    kernels = []
    original = RuntimeKernel.__init__

    def tracked(self, *args, **kwargs):
        original(self, *args, **kwargs)
        kernels.append(self)

    monkeypatch.setattr(RuntimeKernel, "__init__", tracked)
    yield
    for kernel in kernels:
        kernel.close()
