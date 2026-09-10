# The build context is Python/ (see ../.dockerignore), so this file is built
# with the Dockerfile named explicitly and a context one level up from where it
# lives, e.g. from Python/:
#   docker build -f linkedinmcp/Dockerfile -t linkedin-outreach:latest .
# Every COPY source below is therefore a path relative to Python/, not to this
# file's own directory.
FROM python:3.13-slim

WORKDIR /app

RUN pip install uv

# README.md is copied here, before `uv sync`, and that order is load-bearing,
# not cosmetic. pyproject.toml declares `readme = "README.md"`, and the
# `uv sync` below installs this project itself (not just its dependencies),
# which means hatchling builds it -- and hatchling reads the declared readme
# file while doing so. Copy pyproject.toml and uv.lock without README.md and
# the build fails with "OSError: Readme file does not exist: README.md".
# (../.dockerignore does not exclude *.md for the same reason.)
COPY pyproject.toml uv.lock README.md ./

# The project's shared environment file. The container has no other source of
# Unipile and Gemini credentials, and this is the same file the notebooks read,
# so the service runs against exactly the configuration the user already has.
# The build fails if it is absent, which is the right direction: without it the
# container would refuse to start, because `OUTREACH_API_KEY` lives in this file
# too, and it would have no Unipile or Gemini credentials besides.
#
# **The image therefore contains real secrets.** Anyone with read access to the
# Artifact Registry repository this is pushed to can pull it and extract them.
# That is the user's explicit choice, made because Cloud Run's alternative
# (Secret Manager) is more moving parts than this deployment wants.
COPY .env ./

COPY lib/ lib/
COPY functions.py pipeline.py profiles.py messages_sync.py ./
COPY templates/ templates/

# `COPY` of a directory carries its dotfiles too, so linkedinmcp/.env comes
# along when one exists. That file is optional and holds only overrides -- the
# service-specific settings and the zero-pacing values -- layered on top of the
# .env copied above. See app.py for the load order.
COPY linkedinmcp/ linkedinmcp/

# --frozen installs exactly what uv.lock already pins and skips resolution
# entirely -- it trusts the lockfile rather than checking it against
# pyproject.toml (that check, failing when the two disagree, is `--locked`;
# `--frozen` only fails if uv.lock is missing outright). Paired with COPY'ing
# pyproject.toml and uv.lock verbatim above, that keeps this build reproducible
# and network-independent at sync time: the image gets exactly the versions
# recorded in the lockfile the code was tested against, never a fresh resolve
# done today. That guarantee is only as good as the lockfile itself, though --
# run `uv lock --check` before building to confirm uv.lock still matches
# pyproject.toml; this Dockerfile has no way to catch the two having drifted.
RUN uv sync --no-dev --frozen

# Fail the *build* on a packaging mistake, rather than a scheduled job hours
# later. `linkedinmcp/clients.py` imports these lazily, inside each factory, so
# that importing the module stays free -- which means a wheel missing one of
# them would not surface at startup at all. `get_status` would not catch it
# either: that tool probes Firestore and nothing else, so the container would
# report itself healthy while being unable to send a single message. One import
# here costs nothing and turns a silent 7am failure into a red build.
RUN uv run --no-sync python -c "import linkedinmcp.app, lib.unipile, pipeline, functions, profiles, messages_sync"

EXPOSE 8080

# Unbuffered stdout/stderr, so Cloud Run's log tail is live instead of waiting
# on a buffer flush.
ENV PYTHONUNBUFFERED=1

# The working directory stays /app, which is the repository's Python/ directory.
# That is what makes `load_dotenv(".env")` in app.py find the shared env file
# copied above, and what makes `templates_dir`'s default (`templates`) resolve
# to the templates/ directory beside it.
#
# --no-sync: without it, `uv run` re-checks and re-syncs the environment before
# running anything on every single container start -- seconds of cold-start
# latency, paid again on every revision and every scale-to-zero wake, and a step
# that can fail on a transient index outage at the worst possible moment. The
# sync already happened once above, at build time.
#
# --factory linkedinmcp.app:create_app: app.py deliberately has no module-level
# `app` (see its own docstring for why -- a module-level app would read settings,
# and therefore require every OUTREACH_* variable, at import time, breaking test
# collection). `uvicorn linkedinmcp.app:app` would fail with an import error.
CMD ["uv", "run", "--no-sync", "uvicorn", "--factory", "linkedinmcp.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
