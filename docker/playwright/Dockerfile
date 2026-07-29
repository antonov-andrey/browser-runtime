ARG PLAYWRIGHT_IMAGE=python:3.14-bookworm
FROM ${PLAYWRIGHT_IMAGE}

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg tini xauth xvfb \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g npm@11.18.0 @playwright/mcp@0.0.77 \
    && playwright-mcp install-browser --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY docker/playwright/browser_install_verify.mjs /tmp/browser_install_verify.mjs

RUN NPM_GLOBAL_ROOT="$(npm root -g)" node /tmp/browser_install_verify.mjs \
    && rm /tmp/browser_install_verify.mjs

COPY pyproject.toml ./
COPY browser_runtime ./browser_runtime
COPY --from=workflow_container_contract README.md pyproject.toml /tmp/workflow-container-contract/
COPY --from=workflow_container_contract workflow_container_contract /tmp/workflow-container-contract/workflow_container_contract

RUN python -m pip install --root-user-action=ignore --no-cache-dir /tmp/workflow-container-contract ".[browser]" \
    && rm -rf /tmp/workflow-container-contract \
    && groupadd --gid 1000 browser \
    && useradd --uid 1000 --gid browser --create-home --shell /usr/sbin/nologin browser \
    && mkdir -p /output /runtime \
    && chown -R browser:browser /app /output /runtime

USER root

ENTRYPOINT ["/usr/bin/tini", "-s", "--", "browser-runtime-container-entrypoint"]
