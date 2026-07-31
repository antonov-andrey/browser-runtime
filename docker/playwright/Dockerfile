ARG NODE_IMAGE
ARG PLAYWRIGHT_IMAGE

FROM ${NODE_IMAGE} AS node-runtime

FROM ${PLAYWRIGHT_IMAGE}

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    PATH=/opt/browser-runtime-node/node_modules/.bin:${PATH} \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY --from=node-runtime /usr/local/ /usr/local/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 tini xauth xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY docker/playwright/package.json docker/playwright/package-lock.json /opt/browser-runtime-node/
RUN cd /opt/browser-runtime-node \
    && npm ci --omit=dev --ignore-scripts --fund=false --audit=false \
    && playwright-mcp install-browser --with-deps chromium \
    && npm cache clean --force

COPY docker/playwright/browser_install_verify.mjs /tmp/browser_install_verify.mjs

RUN NPM_PACKAGE_ROOT=/opt/browser-runtime-node/node_modules node /tmp/browser_install_verify.mjs \
    && rm /tmp/browser_install_verify.mjs

COPY pyproject.toml ./
COPY browser_runtime ./browser_runtime
COPY --from=workflow_container_contract README.md pyproject.toml /tmp/workflow-container-contract/
COPY --from=workflow_container_contract workflow_container_contract /tmp/workflow-container-contract/workflow_container_contract
COPY docker/playwright/build-requirements.lock docker/playwright/requirements.lock /tmp/browser-runtime-lock/

RUN python -m pip install --root-user-action=ignore --no-cache-dir --require-hashes \
        -r /tmp/browser-runtime-lock/build-requirements.lock \
        -r /tmp/browser-runtime-lock/requirements.lock \
    && python -m pip install --root-user-action=ignore --no-build-isolation --no-cache-dir --no-deps \
        /tmp/workflow-container-contract . \
    && rm -rf /tmp/browser-runtime-lock /tmp/workflow-container-contract \
    && groupadd --gid 1000 browser \
    && useradd --uid 1000 --gid browser --create-home --shell /usr/sbin/nologin browser \
    && mkdir -p /output /runtime \
    && chown -R browser:browser /app /output /runtime

USER root

ENTRYPOINT ["/usr/bin/tini", "-s", "--", "browser-runtime-container-entrypoint"]
