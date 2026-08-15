# Infrastructure

`main.bicep` defines everything KitchenSense runs on in Azure:

| Resource | Name | Notes |
| --- | --- | --- |
| Container Registry | `acrkitchensense<hash>` | Basic SKU, admin user disabled |
| Log Analytics workspace | `log-kitchensense-<env>` | 30 day retention |
| Container Apps environment | `cae-kitchensense-<env>` | logs go to the workspace above |
| Container App | `kitchensense-api` | external ingress on 8000, 0–2 replicas, 0.25 CPU / 0.5Gi |

The container app runs under a system-assigned managed identity that holds **AcrPull**
on the registry, so no registry password exists anywhere.

The `environmentName` parameter (`staging` or `production`) is what lets both
environments share this file. **Each environment gets its own resource group** —
production is `rg-kitchensense`. That keeps the container app name stable at
`kitchensense-api` and gives each environment its own registry, since the registry name
is derived from the resource group ID.

The deploy workflow only builds and rolls out images. Infrastructure changes are applied
by running the deployment command below.

---

## One-time setup

Everything here is done once per environment, by someone with permission to create app
registrations and assign roles.

### 1. Deploy the infrastructure

```bash
az deployment group create \
  --resource-group rg-kitchensense \
  --template-file infra/main.bicep \
  --parameters environmentName=production
```

The first deployment starts the container app on a public placeholder image
(`mcr.microsoft.com/k8se/quickstart`), because the registry has no image yet. The first
run of the deploy workflow replaces it with a real SHA-tagged build. Later deployments
of the Bicep file leave the running image alone unless you pass `containerImage`
yourself.

Note the outputs — `registryLoginServer` and `containerAppFqdn` are useful for
verification.

### 2. Create the app registration GitHub will sign in as

```bash
az ad app create --display-name kitchensense-deploy
APP_ID=$(az ad app list --display-name kitchensense-deploy --query "[0].appId" -o tsv)
az ad sp create --id "$APP_ID"
```

### 3. Register the federated credential

This is what replaces a client secret: GitHub presents a short-lived OIDC token, and
Azure trusts it only for the exact repository, branch and workflow trigger described
here. Replace `<owner>/<repo>` with the GitHub repository path.

```bash
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<owner>/<repo>:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

The `subject` must match exactly how the workflow runs. `deploy.yml` triggers on pushes
to `main`, so the subject is the `ref:refs/heads/main` form above. If you later add a
trigger for pull requests or tags, add a second credential — one per subject:

- pull requests → `repo:<owner>/<repo>:pull_request`
- tags → `repo:<owner>/<repo>:ref:refs/tags/v1.0.0`
- GitHub Environments → `repo:<owner>/<repo>:environment:production`

### 4. Give the service principal access

```bash
SUB_ID=$(az account show --query id -o tsv)
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
ACR_ID=$(az acr list -g rg-kitchensense --query "[0].id" -o tsv)

# Deploy infrastructure and update the container app.
az role assignment create \
  --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope "/subscriptions/$SUB_ID/resourceGroups/rg-kitchensense"

# Push images.
az role assignment create \
  --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
  --role AcrPush \
  --scope "$ACR_ID"
```

### 5. Add the GitHub secrets

In the repository, under **Settings → Secrets and variables → Actions**, add three
repository secrets. **There is no client secret** — the federated credential is the
whole authentication story.

| Secret | Value | How to get it |
| --- | --- | --- |
| `AZURE_CLIENT_ID` | app registration's application (client) ID | `az ad app list --display-name kitchensense-deploy --query "[0].appId" -o tsv` |
| `AZURE_TENANT_ID` | directory (tenant) ID | `az account show --query tenantId -o tsv` |
| `AZURE_SUBSCRIPTION_ID` | subscription ID | `az account show --query id -o tsv` |

These are identifiers rather than credentials, but keeping them as secrets avoids
publishing the tenant layout in logs.

### 6. Check it works

Push to `main` (or run the workflow manually from the Actions tab) and confirm:

```bash
curl https://$(az containerapp show -n kitchensense-api -g rg-kitchensense \
  --query properties.configuration.ingress.fqdn -o tsv)/health
```

---

## How the deploy workflow finds things

`.github/workflows/deploy.yml` looks up the registry by the `environment` tag inside the
resource group, so the generated registry name never has to be copied into the workflow.
Images are tagged with the full commit SHA — never `latest` — so every revision of the
container app points at exactly one build, and rolling back is a matter of pointing
`az containerapp update --image` at an older SHA.

## Staging

Create the resource group, then deploy the same file with a different parameter:

```bash
az group create --name rg-kitchensense-staging --location uksouth
az deployment group create \
  --resource-group rg-kitchensense-staging \
  --template-file infra/main.bicep \
  --parameters environmentName=staging
```

Staging needs its own federated credential subject (for whichever branch or GitHub
Environment deploys it) and its own role assignments on the staging resource group.
