# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| latest 0.x release | ✅ |
| older releases | ❌ |

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/polaris-health/cavell-prism-client/security/advisories/new)
or email <support@cavell.ai> <!-- TODO(owner): replace with a dedicated security contact -->.

We aim to acknowledge reports within 5 business days.

## Scope notes

- This SDK never stores credentials: the LLM Gateway key is held in memory
  and sent per request; FHIR credentials go only to your own FHIR server.
- The demo datasets in `docs/notebooks/` are fully synthetic — reports about
  "patient data" in them are appreciated but not vulnerabilities.
