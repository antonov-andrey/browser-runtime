# browser-runtime

Переиспользуемый run-local Playwright MCP runtime с постоянными named profiles, bundled Playwright Chromium и optional platform-provided network proxies.

Репозиторий владеет запуском браузера, публичным MCP profile router, физическими run-local profile directories, reset и writeback-candidate snapshots профиля, stealth, locale, timezone, viewport и isolated backend. Он не владеет workflow orchestration, domain extraction, VPN config, tunnel protocols, SOCKS5 gateways, provider slots или VPN lifecycle.

## Граница Runtime

Платформа предоставляет:

- immutable source directory `playwright_profile/`, которая может быть пустой;
- writable run-local roots для profiles, backends, outputs и writeback candidate;
- immutable `proxy_by_name_map` из exact stable names в run-local SOCKS5 URLs;
- exact Linux OCI target platform для image build.

Public MCP requests могут содержать structural query values:

- `profile=<physical-profile>`;
- `profile_source=<physical-source-profile>` только для новой MCP initialization;
- `network_proxy_name={zitadel_user_id}/{vpn_config.name}`.

Caller читает `network_proxy_name` из exact browser input setting. Router только проверяет переданное name по map, никогда не выбирает и не распределяет names и удаляет собственные structural values перед forwarding. Backend identity является парой `(physical_profile, network_proxy_name)`. Поэтому один profile может одновременно работать через разные proxies; разные exact settings не вызывают `browser_close` и не создают profile lease conflict. Отсутствующий proxy name означает direct browser egress.

Каждый proxied backend запускает bundled Playwright Chromium с exact `socks5://` endpoint, отключает QUIC, сохраняет target DNS на proxy side и не настраивает direct-proxy fallback. Runtime не устанавливает и не использует Google Chrome или Playwright channel `chrome`. Browser не читает VPN metadata, credentials, tunnel state, routes или `tun0`. Разрыв существующего TCP остаётся ordinary browser error, который caller обрабатывает по собственному retry contract.

## Профили

Named profiles используют отдельные backend process, config directory, output directory и persistent working user-data directory для каждой profile/proxy pair. Две пары никогда не открывают один `userDataDir`. Isolated backend не имеет persistent profile.

Named target без `profile_source` инициализирует pair-local copy из immutable source только при её отсутствии. Explicit source reset разрешает source под тем же exact proxy name, останавливает только target pair и атомарно заменяет её working copy под deterministic pair locks.

`POST /runtime/mcp-playwright-profile/writeback-candidate?profile=<physical-profile>&network_proxy_name=<stable-name>` останавливает exact backend pair и атомарно заменяет единственную candidate directory из working copy. Побеждает последняя успешная publication. Proxy identity не становится частью profile bytes или writeback destination.

`POST /runtime/execution-state-restore` является run-local platform command после доказанной остановки predecessor и требует отдельные `X-Browser-Runtime-Execution-State-Restore-Token` и stable `X-Browser-Runtime-Execution-State-Restore-Identity`. Он прекращает admission новых запросов, прерывает stale streaming handlers predecessor, дожидается ограниченных candidate operations, останавливает все backend pairs и удаляет только attempt-local browser state. HMAC-защищённый completed marker делает повтор exact identity безопасным no-op; новая execution identity снова выполняет полную очистку. После этого следующая execution начинает каждую пару из текущего immutable accepted profile source.

## Безопасность

Browser и MCP processes выполняются non-root. Image не получает VPN secret, S3 credential, Product DB credential, Kubernetes API token или VPN control API. Network reachability предоставляется platform и не выводится из пользовательского input.

## Разработка

```bash
python -m pip install -e ".[browser,test]"
python -m pytest -q
python -m compileall browser_runtime
docker buildx build --load \
  --build-arg NODE_IMAGE=public.ecr.aws/docker/library/node:24-bookworm-slim \
  --build-arg PLAYWRIGHT_IMAGE=public.ecr.aws/docker/library/python:3.14-slim-trixie \
  --build-context workflow_container_contract=../workflow-container-contract \
  -f docker/playwright/Dockerfile -t browser-runtime:local .
```

Product deployment определяет target platform по Kubernetes nodes и передаёт её build явно. Local build выше использует текущий Docker target и не является Product release identity.
Product release сначала разрешает оба canonical family selectors в exact digests; browser runtime не вводит собственные distro-варианты Node или Debian Python. Python build/runtime dependencies устанавливаются только из hash-locked requirements, а `@playwright/mcp` и его transitive graph — только через committed `package-lock.json` и `npm ci`; global/moving `npm install` в image отсутствует.
