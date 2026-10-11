# Security policy

This repository is a sanitized public snapshot of BOT CRM. It must not contain production credentials, database dumps, runtime payment payloads, logs, or personal customer data.

## Local configuration

Copy `inf.env.example` to `inf.env` and keep all real values only in the local file. `inf.env` and other `*.env` files are ignored by Git.

## Reporting a vulnerability

Do not publish credentials, personal data, or exploit details in a public issue. Contact the repository owner privately and include only the minimum information needed to reproduce the problem.

## Before deployment

- generate unique secrets and passwords;
- use a dedicated database account instead of `root`;
- disable Django debug mode;
- restrict allowed hosts and CORS origins;
- enable HTTPS and secure cookies;
- run `python manage.py check --deploy`;
- keep production data outside the repository.
