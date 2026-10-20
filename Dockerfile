# Lynk API image.
#
# Three things this image must never do (POTENTIAL_ISSUES #38):
#   * run as root
#   * bake the working tree - including the .env - into a layer
#   * ship the test framework
#
# The builder stage installs the runtime dependencies into a virtualenv; the
# runtime stage copies only that virtualenv and the application package.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# A dedicated unprivileged user. The uid/gid are fixed so a bind-mounted
# volume can be given matching ownership.
RUN groupadd --gid 1001 lynk \
    && useradd --uid 1001 --gid 1001 --no-create-home --shell /usr/sbin/nologin lynk

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Only the application package. The tests, the deployment scripts, the
# development tooling and the .env never enter the image: the container gets
# its configuration from the environment (docker-compose passes env_file).
COPY --chown=lynk:lynk app/ ./app/

USER lynk

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
