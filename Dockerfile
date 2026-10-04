FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim AS base
WORKDIR /app
COPY pyproject.toml ./
COPY routing/ routing/
COPY data/ data/
RUN pip install --no-cache-dir -e .

FROM base AS test
RUN pip install --no-cache-dir -e ".[dev]"
COPY tests/ tests/
COPY profile_solver.py ./
CMD ["sh", "-c", "pytest && python profile_solver.py"]

FROM base
COPY --from=web /web/dist web/dist
CMD ["uvicorn", "routing.server:app", "--host", "0.0.0.0", "--port", "8000"]
