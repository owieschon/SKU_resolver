# Deployed runtime image (P4). Builds the API + Twilio voice server.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY data ./data
RUN pip install --no-cache-dir -e ".[serve]"

# Runtime config via env (see src/runtime/config.py). Provide a real
# SKU_SESSION_SECRET and SKU_WEBHOOK_SECRET in production; set
# SKU_LLM_PROVIDER + the matching *_API_KEY to enable the LLM seams; set
# TWILIO_* / ASSEMBLYAI_API_KEY for live voice.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn runtime.app:create_app --factory --host 0.0.0.0 --port ${PORT}"]
