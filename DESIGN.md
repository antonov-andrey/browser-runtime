# Browser Runtime

## Purpose

`browser-runtime` provides one reusable browser automation surface. It owns browser processes, Playwright MCP routing, physical run-local profiles, profile snapshots, stealth, locale, timezone, and viewport. `vpn-runtime` owns every VPN gateway and tunnel concern. Workflow execution remains external and consumes only the public MCP router.

## Browser And Proxy Boundary

The platform passes one immutable `proxy_by_name_map` whose keys are exact stable `{zitadel_user_id}/{vpn_config.name}` values and whose values are run-local SOCKS5 URLs. The browser runtime neither creates this map nor reads VPN configuration, active Version, provider metadata, or credentials.

The public router accepts optional structural values `profile`, `profile_source`, and `network_proxy_name`. The caller must supply the exact proxy name from its concrete input setting; the router never selects or distributes names. It rejects unsafe, duplicate, inconsistent, or unknown values and strips them before forwarding the request. The backend identity is exactly `(physical_profile, network_proxy_name)`; absence of both uses the isolated direct backend. A named profile may have multiple simultaneous proxy-specific backend processes without sharing Chromium state, network context, `userDataDir`, ports, config, or output.

A proxied backend resolves only the supplied run-local Service endpoint, waits for SOCKS5 TCP readiness, configures Playwright with that exact `socks5://` URL, disables QUIC, and keeps target hostname resolution on the SOCKS side. It has no fallback proxy or VPN-specific reconnect branch. Direct and proxied backends use the same browser implementation and differ only by the explicit proxy launch configuration.

## Profile Lifecycle

The platform materializes an immutable source `playwright_profile/` directory even when empty. Each named profile/proxy pair owns one separate working directory under the run-local profile root. A target without explicit source is copied from the immutable source only when that pair-local directory is absent. An explicit source reset is valid only for a new MCP initialization, resolves both source and target under the same exact supplied proxy name, locks the pair identities deterministically, stops the exact target backend pair, and atomically replaces only its working directory.

Profile lease identity includes the router endpoint, physical profile, and supplied stable proxy name. Correction attempts retain the same identity. Different profile/proxy pairs remain concurrent because their working directories are disjoint. Candidate publication stops the exact pair and atomically replaces one shared candidate from its working copy; the proxy name is part of backend identity but never profile bytes or writeback destination. The last successfully completed candidate publication wins.

## Process And Security Boundary

The router owns one lazy `@playwright/mcp` process per active backend identity. Every process uses a separate loopback port, runtime directory, generated config, stealth script, and output directory. Browser and MCP processes run non-root. The image receives only browser profile state and safe proxy endpoints; it never receives VPN secrets, tunnel devices, `NET_ADMIN`, Product storage credentials, or Kubernetes API access.

## Verification Contract

Behavior tests cover strict structural route parsing, unknown proxy rejection, independent backends for the same profile with different proxies, isolated direct mode, exact SOCKS launch configuration, proxy-side DNS settings, no forced close on proxy difference, profile reset, candidate publication, MCP streaming, and non-root container startup. Integration tests use real SOCKS endpoints with distinguishable egress and prove concurrent profile/proxy isolation. VPN establishment and fail-closed gateway behavior belong to `vpn-runtime` tests.
