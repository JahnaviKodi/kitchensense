# 002 — Container registry authentication

## Decision
The Container App pulls images using a user-assigned managed identity with
the AcrPull role. The registry admin user is disabled.

## What happened first
The initial Bicep deployment failed twice with "Operation expired" and no
revision created. Two causes:

1. Health probes targeted /health on port 8000, but the placeholder image
   mcr.microsoft.com/k8se/quickstart serves port 80 and has no such endpoint.
2. The Container App was configured to authenticate to our private registry
   using managed identity, while pointing at a public Microsoft image.

The role assignment and the Container App were also created simultaneously,
so the AcrPull permission had not propagated when the app attempted to pull.

## Stopgap
Enabled the registry admin user and created the Container App via CLI with a
password credential, to get a working deployment.

## Resolution
- User-assigned managed identity, so the identity survives app recreation
- dependsOn ensures both role assignments exist before the Container App
- Registry admin user disabled once identity-based pulls were verified

## Alternatives rejected
- System-assigned identity — lost whenever the app is recreated
- Registry admin password — a shared credential with full access, stored in
  the app configuration
