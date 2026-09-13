# Security

## Credentials and local data

Keep API keys in the ignored local `.env` or process environment. Example configuration files contain placeholders only. Store OAuth credentials, service-account files, tokens, cookies and browser profiles outside Git. Keep personal documents, recordings, databases and generated output local.

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits.

## Before sharing changes

Review the staged files and run a secret scanner before pushing. Ignore rules do not protect files that have already been tracked. If a credential is committed, revoke or rotate it at the provider and review the Git history.

The application source was reviewed for accidental credential and personal-data disclosure. Secret scans do not establish that every runtime vulnerability is absent. The functional checks and their limits are recorded in [VERIFICATION.md](VERIFICATION.md).

## Reporting a vulnerability

Use this repository's private vulnerability reporting feature when available. Do not include active credentials, private documents or account sessions in a public issue or pull request.
