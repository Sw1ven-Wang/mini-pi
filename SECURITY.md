# Security

Mini-Pi is experimental local coding-agent software, not a hosted or multi-tenant service.
No response-time SLA or bug bounty is offered.

## Reporting a vulnerability

Please do not publish API keys, private source, runtime databases, recovery blobs,
or a working exploit against a real user workspace in a public issue.
Use GitHub's private vulnerability reporting feature for this repository when available:

<https://github.com/Sw1ven-Wang/mini-pi/security/advisories/new>

If private reporting is unavailable, open an issue containing only a request for a private
reporting channel, without vulnerability details or sensitive attachments.

Include the tested commit, OS, Python version, sandbox level, and a minimal synthetic
reproduction. Sanitize paths, provider configuration, commands, logs, and environment data.

## Important boundaries

- Keep the Web service on loopback. Do not expose it through LAN/public listeners,
  tunnels, or a reverse proxy; authentication and multi-user isolation are not implemented.
- L1 limits convenience and lifecycle behavior but is **not a security sandbox**.
  Tests and verification commands execute project code. Review untrusted repositories
  and use separately validated L2 isolation when required.
- L2 requires a digest-pinned image and verified Docker/Podman isolation. The local
  Linux runtime test container does not substitute for the opt-in L2 security suite.
- Deterministic DENY rules cannot be overridden by human approvals or AUTO.
  AUTO is disabled by default and does not provide blanket permission bypass.
- Interrupted operations with uncertain effects or resource ownership remain blocked.
  Never edit the runtime schema version or remove pending state to force a resume.
- Private runtime state and backups can contain original source bytes. Keep them outside
  allowed task roots, limit local access, and do not attach them to public issues.
- Store provider credentials in server environment variables, not in source, settings,
  command-line examples with real values, or committed provider profiles.

See [WEB_WORKBENCH.md](WEB_WORKBENCH.md) for the current implementation and its limitations.
