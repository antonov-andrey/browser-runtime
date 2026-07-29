# Browser Runtime

## Назначение

`browser-runtime` предоставляет одну переиспользуемую поверхность автоматизации браузера. Он владеет процессами браузера, маршрутизацией Playwright MCP, физическими run-local профилями, snapshot/writeback профилей, stealth, locale, timezone и viewport. `vpn-runtime` владеет VPN gateway и tunnel, а workflow execution остаётся внешним потребителем публичного MCP router.

## Движок Браузера И Платформа

Runtime использует bundled Playwright Chromium, установленный той же версией Playwright, что и runtime package. Установленный Google Chrome, Playwright channel `chrome` и зависимость от отдельного Chrome repository отсутствуют.

Deployment передаёт exact Linux OCI target platform, определённую по eligible Kubernetes nodes, каждому build явно. Browser image обязан публиковать manifest/config той же platform и содержать доступный для неё bundled Chromium. Architecture builder process не является скрытым target, а hardcoded `linux/arm64` отсутствует.

## Граница Браузера И Proxy

Платформа передаёт одну immutable `proxy_by_name_map`, где keys являются exact stable `{zitadel_user_id}/{vpn_config.name}`, а values — run-local SOCKS5 URL. Browser runtime не создаёт эту map и не читает VPN configuration, active Version, provider metadata или credentials.

Public router принимает optional structural values `profile`, `profile_source` и `network_proxy_name`. Caller передаёт exact proxy name из своей конкретной input setting; router никогда не выбирает и не распределяет names. Он отклоняет unsafe, duplicate, inconsistent или unknown values и удаляет собственные structural values перед forwarding. Backend identity равен `(physical_profile, network_proxy_name)`; отсутствие обоих использует isolated direct backend. Один named profile может одновременно работать через разные proxy-specific backend processes без общего Chromium state, network context, `userDataDir`, port, config или output.

Proxied backend разрешает только переданный run-local Service endpoint, ждёт SOCKS5 TCP readiness, запускает Chromium с exact `socks5://` URL, отключает QUIC и оставляет resolution target hostname на SOCKS side. Fallback proxy и VPN-specific reconnect branch отсутствуют. Direct и proxied backends используют одинаковую browser implementation и различаются только явной proxy launch configuration.

## Жизненный Цикл Профиля

Платформа материализует immutable source directory `playwright_profile/`, в том числе пустую. Каждая пара named profile/proxy владеет отдельной working directory ниже run-local profile root. Target без explicit source копируется из immutable source только при отсутствии pair-local directory. Explicit source reset допустим только для новой MCP initialization, разрешает source и target под тем же exact proxy name, детерминированно блокирует обе pair identities, останавливает exact target backend и атомарно заменяет только его working directory.

Profile lease identity включает router endpoint, physical profile и stable proxy name. Correction attempts сохраняют identity. Разные profile/proxy pairs выполняются параллельно, потому что их working directories не пересекаются. Candidate publication останавливает exact pair и атомарно заменяет один общий candidate из его working copy; proxy name входит в backend identity, но не в profile bytes или writeback destination. Побеждает последняя успешно завершённая publication.

После доказанной остановки predecessor platform вызывает run-local `POST /runtime/execution-state-restore` с отдельным Secret-backed одноцелевым credential, недоступным workflow container. Router сначала ждёт завершения всех уже принятых MCP и candidate requests, затем останавливает все backend process groups и очищает только mutable profile, candidate, backend-runtime и output roots, не заменяя их mounted directories. Следующий request заново создаёт pair-local profile из уже материализованного platform immutable source. Query parameters и body у restore-команды запрещены.

## Граница Процессов И Безопасности

Router владеет одним lazy `@playwright/mcp` process для каждой active backend identity. Каждый process использует отдельные loopback port, runtime directory, generated config, stealth script и output directory. Browser и MCP processes выполняются non-root. Image получает только browser profile state и safe proxy endpoints; VPN secrets, tunnel devices, `NET_ADMIN`, Product storage credentials и Kubernetes API отсутствуют.

## Проверки

Behavior tests покрывают bundled Chromium launch без channel `chrome`, strict structural route parsing, unknown proxy rejection, independent backends одного profile с разными proxies, isolated direct mode, exact SOCKS launch, proxy-side DNS, отсутствие forced close при proxy difference, profile reset, candidate publication, MCP streaming и non-root container startup.

Build verification проверяет exact target platform и наличие runnable bundled Chromium внутри image. Integration tests используют реальные SOCKS endpoints с различимым egress и доказывают concurrent profile/proxy isolation. Установление VPN и fail-closed gateway принадлежат tests `vpn-runtime`.
