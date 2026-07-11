"""
Compatibility shim for a real upstream bug in ragas 0.4.3 (verified against
a fresh `pip install ragas` on 2026-07-10, current version at time of
writing).

`ragas/llms/base.py` unconditionally does:
    from langchain_community.chat_models.vertexai import ChatVertexAI
at module import time — even though we never use Vertex AI (this project
uses Gemini via `langchain-google-genai`, a separate, actively-maintained
integration). That submodule no longer exists in current
`langchain-community` (which is itself sunset/archived and being trimmed
down), so `import ragas` fails outright before you ever get to choose an
LLM provider.

The stub below fixes this cleanly with no version downgrades: it's
verified to coexist fine with this project's actual langchain-core 1.4.9 /
langchain-google-genai 4.2.7 stack in the same environment — the failure
mode isn't a real cross-package version conflict, just this one
unconditional dead import. (An earlier attempt to "fix" this by pinning
langchain-community lower — to a version old enough to still have the
vertexai submodule — does cause a real cascading conflict, since that
drags langchain-core down with it and breaks langchain-google-genai. That
is NOT what this fix does.)

evaluation/requirements.txt is still a separate file from the main
backend/requirements.txt, but that's for a weaker, more ordinary reason:
ragas pulls in `datasets`, `instructor`, `openai`, `tiktoken` and friends,
which have no reason to be sitting in the serving container's image if
you deploy the API. Run evaluation from its own venv if you want a lean
production image; running it from the same venv as the API also works.

Import this module BEFORE importing anything from `ragas`:
    import evaluation._ragas_compat  # noqa: F401  (side-effect import)
    from ragas import evaluate
"""
import sys
import types


class _UnusedChatVertexAI:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "ChatVertexAI stub was instantiated, but this project never configures "
            "Vertex AI — it uses Gemini via langchain-google-genai. If you're seeing "
            "this, something is misconfigured; check evaluation/run_ragas.py's LLM setup."
        )


def _install_stub() -> None:
    module_name = "langchain_community.chat_models.vertexai"
    if module_name in sys.modules:
        return
    stub = types.ModuleType(module_name)
    stub.ChatVertexAI = _UnusedChatVertexAI
    sys.modules[module_name] = stub


_install_stub()
