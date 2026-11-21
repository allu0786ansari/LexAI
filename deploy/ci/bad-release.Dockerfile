# A planted bad release for the CI deploy rehearsal: the process starts and /healthz is fine,
# but the release ships a model name the model server does not have, so /readyz never passes.
# deploy.py must detect it in the smoke test and put the previous release back.
ARG BASE=lexai-backend:local
FROM ${BASE}
ENTRYPOINT ["env", "LEXAI_LLM_MODEL=does-not-exist-in-this-release"]
CMD ["uvicorn", "lexai.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
