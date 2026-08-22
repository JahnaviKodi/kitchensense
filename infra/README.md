# Infrastructure

`main.bicep` defines everything KitchenSense runs on in Azure:

| Resource | Name | Notes |
| --- | --- | --- |
| Container Registry | `acrkitchensense<hash>` | Basic SKU, admin user disabled |
| Key Vault | `kv-ks-<hash>` | RBAC authorisation, soft delete with 90 day retention |
| PostgreSQL flexible server | `psql-kitchensense-<hash>` | Burstable B1ms, PostgreSQL 16, 32GB, public access |
| Storage account | `stks<hash>` | Standard_LRS, TLS 1.2 minimum, **shared key access disabled** |
| Blob container | `receipts` | private; lifecycle rule deletes blobs after 30 days |
| Managed identity | `id-kitchensense-<env>` | user-assigned; the identity the container app runs as |
| Log Analytics workspace | `log-kitchensense-<env>` | 30 day retention |
| Container Apps environment | `cae-kitchensense-<env>` | logs go to the workspace above |
| Container App | `kitchensense-api` | external ingress on 8000, 0–2 replicas, 0.25 CPU / 0.5Gi |

The container app runs under a user-assigned managed identity that holds **AcrPull** on
the registry, **Key Vault Secrets User** on the vault and **Storage Blob Data
Contributor** on the storage account, so no registry password and no storage key exists
anywhere and secrets are read at runtime rather than stored in the template.

The template grants **Key Vault Secrets User** on the same vault to one other principal:
the service principal GitHub Actions signs in as, named by the `deployPrincipalId`
parameter. The deploy workflow reads `postgres-connection-string` to run migrations, and
that is a data-plane call the Contributor role does not cover. Pass an empty string to
add no such assignment.

The identity is user-assigned rather than system-assigned deliberately. A system-assigned
principal does not exist until its resource has been created, which means its role
assignments can only be created *after* the container app — leaving a window where the
app tries to pull its image before AcrPull has propagated, and the deployment fails. A
user-assigned identity exists on its own, so both role assignments are created first and
the container app declares `dependsOn` on them. Purge protection is deliberately left off
so a torn-down staging vault's name can be reused.

The `environmentName` parameter (`staging` or `production`) is what lets both
environments share this file. **Each environment gets its own resource group** —
production is `rg-kitchensense`. That keeps the container app name stable at
`kitchensense-api` and gives each environment its own registry, since the registry name
is derived from the resource group ID.

The deploy workflow only builds and rolls out images. Infrastructure changes are applied
by running the deployment command below.

## The database

`postgresAdminPassword` is a required parameter with no default — a template cannot
invent a password, and any default would be a shared credential in source control. **Every
deployment must pass it**, including the `what-if` in step 1 below, and passing the same
value again is a no-op. Avoid `: / ? # [ ] @` in it: the connection string is a URI, and
those characters would be read as delimiters.

The connection string is written to the vault as **`postgres-connection-string`**. Nothing
extra was needed to let the app read it — the container app's identity already holds Key
Vault Secrets User on the whole vault, which covers every secret in it. Note that the
deploying principal needs `Microsoft.KeyVault/vaults/secrets/write`, which is a
control-plane permission: Contributor on the resource group has it, and the data-plane
**Key Vault Secrets Officer** role does not.

### How the app finds it

Three environment variables on the container, all derived from resources in this template
rather than hardcoded, so staging reads staging's vault as staging's identity:

| Variable | Value |
| --- | --- |
| `KEY_VAULT_URI` | `keyVault.properties.vaultUri` |
| `AZURE_CLIENT_ID` | `appIdentity.properties.clientId` |
| `POSTGRES_SECRET_NAME` | `postgres-connection-string` |

`AZURE_CLIENT_ID` is not optional. The identity is *user-assigned*, and a credential given
no client id looks for a system-assigned one, which this app does not have.

The connection string itself is deliberately absent from the container definition. Putting
it there would place the database password in the template, in the deployment history and
in the output of `az containerapp show` — all readable by anyone with resource-group read
access. The app fetches it at runtime instead.

The fetch is best-effort at startup and lazy thereafter, which is what lets the container
start while the PostgreSQL server is stopped. A vault that is briefly unreachable, or a
role assignment that has not propagated, produces a warning in the startup logs and a
`not_configured` on `/health/deep` — not a container that will not start. Set
`DATABASE_URL` on the container to bypass the vault entirely; its presence takes
precedence.

## Receipt uploads

Receipt images do not travel through the API. A client asks `POST /uploads` for
permission, uploads the file straight to blob storage, and comes back to `POST
/uploads/{id}/confirm`. The container app never buffers a photograph.

The permission is a **user delegation SAS**: write-only, one named blob, five minutes.
What makes it worth the extra moving parts is what it is *not* — the usual way to sign a
SAS is with one of the account's two access keys, and an account key is a permanent,
unscoped, unrevocable credential for the whole account. Here nothing reads a key:

* The app asks storage for a *user delegation key*, authenticating as the managed
  identity. That is what **Storage Blob Data Contributor** above is for, and it is
  needed for both halves — a delegation SAS can grant no more than the identity signing
  it already holds, so an identity without write access could sign a write SAS that
  storage then refuses.
* The key expires on its own, and revoking the role assignment invalidates every SAS
  derived from it.
* `allowSharedKeyAccess` is set to **false** on the account, so the account keys cannot
  be used at all — by this app or by anything added to it later.

The container is private (`publicAccess: 'None'`) and the account has
`allowBlobPublicAccess: false`, which makes that true regardless of what any container's
own setting says.

One practical consequence of disabling shared key access: browsing the container in the
portal, or with `az storage blob list`, no longer works with an account key — the portal
defaults to key authentication and will report a failure that reads like a permissions
bug. Switch its authentication method to **Microsoft Entra user account**, or pass
`--auth-mode login` to the CLI, and give yourself the data-plane role:

```bash
STORAGE_ID=$(az storage account list -g rg-kitchensense --query "[0].id" -o tsv)
az role assignment create   --assignee "$(az ad signed-in-user show --query id -o tsv)"   --role "Storage Blob Data Contributor"   --scope "$STORAGE_ID"
```

A lifecycle management policy deletes block blobs under the `receipts/` prefix
`receiptRetentionInDays` (30) days after last modification. Receipt images are transient:
they are read once by the extraction step, and what survives is the events they produced.
The prefix filter is deliberate — a container added to this account later is not swept up
by a policy written before it existed.

The API is given the account **name**, as `STORAGE_ACCOUNT_NAME`, and builds the blob
endpoint from it. There is no connection string and no key in the container definition.
An unset name is not an error: the app starts, logs that uploads are unconfigured, and
answers 503 from `/uploads` while everything else works.

## Identity

Three more environment variables, and unlike the vault and the registry these cannot be
derived from anything this template deploys — they identify an Entra External ID tenant,
which is a directory the API trusts rather than a resource here. They are parameters with
defaults so a second environment can point at a different tenant without editing the file:

| Variable | Parameter | Default |
| --- | --- | --- |
| `ENTRA_TENANT_ID` | `entraTenantId` | `14e3c719-bb1f-41cf-a75b-ba38e91e072d` |
| `ENTRA_CLIENT_ID` | `entraClientId` | `87749dc2-96a6-4eb6-a811-02400be2309c` |
| `ENTRA_AUDIENCE` | `entraAudience` | the client id |

None of them is a secret. They are public identifiers, and the signing keys they lead to
are public keys — the API never calls the tenant as a client, it only validates what
callers present, so it has no client secret at all.

The audience is separate from the client id because they are separate ideas. An API that
later accepts tokens minted for an Application ID URI changes its audience without changing
its identity.

The application has **no defaults** for these. Unset, every protected endpoint answers 503
and the startup log says so. A default would be a guess at which directory issues our
tokens, and the wrong guess is a way of accepting the wrong ones.

Note that External ID tenants live on `ciamlogin.com`, not the `login.microsoftonline.com`
host a workforce tenant uses. The discovery URL is built from the tenant id and can be
overridden with `ENTRA_OPENID_CONFIGURATION_URL` if a tenant is reachable some other way.

## The database, continued

Two firewall rules. `AllowAllAzureServices` is the `0.0.0.0`–`0.0.0.0` sentinel behind the
portal's "allow Azure services" checkbox; it is what lets the container app connect, since
a Container Apps environment with no dedicated VNet has no stable outbound IP to name
instead. It is broader than the name suggests — it admits Azure resources in *any*
subscription, not just this one, so the database's own credentials are what actually
protect it. The second rule is optional and off by default:

```bash
--parameters clientIpAddress=$(curl -s ifconfig.me)
```

`azure.extensions` is set to `VECTOR`, which allow-lists the extension at the server. That
is only half of it — the extension still has to be created inside the database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### There is no auto-stop

This was asked for and does not exist. Azure Database for PostgreSQL flexible server has
no idle auto-stop or auto-pause — that is an Azure SQL serverless feature, and it has no
PostgreSQL equivalent on any tier. `az postgres flexible-server` offers only a manual
`stop`, which takes no arguments beyond the server name, and a stopped server **restarts
itself automatically after 7 days** whether or not anyone asks.

Stopping by hand is the whole of what is available:

```bash
az postgres flexible-server stop --name <server> --resource-group rg-kitchensense
az postgres flexible-server start --name <server> --resource-group rg-kitchensense
```

Note that stopping suspends compute billing but **not storage** — the 32GB is charged
either way. To approximate the intent, run the `stop` above on a schedule (a cron'd
GitHub Actions job, or an Azure Automation runbook) and accept the weekly auto-restart.
Nothing in this template does that today.

The application is built for this. It opens no connection at startup, so the container
starts normally with the server stopped; `/health` — which both probes point at — never
touches the database; and `/health/deep` reports `"database": "unreachable"` while still
returning 200, so the app is not restarted over it. Only the endpoints that need rows
fail, with a 503.

## Who owns the running image

The deploy workflow does, and a Bicep deployment must never take it back. Because ARM
rewrites every property it is given, a template that names an image would reset a
SHA-tagged build to whatever the template said — which is exactly how a deployment once
rolled production back to `kitchensense:v1`.

So `containerImage` is empty by default and the image is read back from the running app
instead. `current-image.bicep` is a one-resource module that does nothing but look the
app up and output the image it is running; `main.bicep` feeds that straight back into the
container. The lookup has to sit in its own module: an `existing` reference in
`main.bicep` compiles to a `reference()` on the very container app that file deploys, and
ARM rejects a resource that references itself as a circular dependency.

That leaves one thing the template cannot work out for itself — whether the app is there
to be read. `containerAppExists` answers it, and defaults to `true` so the routine case
needs no thought and no flag. **The first deployment into an empty resource group must
pass `containerAppExists=false`**; forget it and the deployment fails with
`ResourceNotFound` rather than quietly rolling the image back.

A first deployment must also pass `containerImage`, because there is no running image to
keep and the template will not invent one. Deploying with neither fails straight away
with an error naming both parameters. That refusal is deliberate: the obvious placeholder,
`mcr.microsoft.com/k8se/quickstart`, listens on port 80 while ingress and both health
probes here target 8000, so the revision never becomes healthy and the deployment burns
its entire timeout before failing with `Operation expired` — the same slow, unreadable
failure this environment has already hit once. A refusal that names the missing parameter
is the same problem reported in a second instead of an hour. [Bootstrapping a new
environment](#1-bootstrap-the-registry) covers how to get that first image built when the
registry does not exist yet.

`containerImage` stays available for pinning and rollback: passing it overrides the
lookup, and the `containerAppImage` output reports what was actually deployed.

---

## One-time setup

Everything here is done once per environment, by someone with permission to create app
registrations and assign roles.

### 1. Bootstrap the registry

The container app has to start on a real image — one that answers `/health` on port 8000,
which no public placeholder does. That image has to live in the registry, and the registry
does not exist yet, so it is created first and the template adopts it.

Its name is derived from the resource group ID, so read the name the template will use
rather than choosing one. `what-if` evaluates the template without deploying anything;
`containerImage` is only supplied here because the template refuses to be evaluated
without it, and `postgresAdminPassword` because it has no default. Use the real password
here — what-if runs the same validation a deployment does, so a throwaway that fails the
complexity rules fails the command.

```bash
REGISTRY=$(az deployment group what-if \
  --resource-group rg-kitchensense \
  --template-file infra/main.bicep \
  --parameters environmentName=production containerAppExists=false containerImage=none \
    postgresAdminPassword="$PGPASSWORD" \
  --no-pretty-print \
  --query "changes[?contains(resourceId, 'Microsoft.ContainerRegistry/registries')].after.name | [0]" \
  --output tsv)

az acr create --name "$REGISTRY" --resource-group rg-kitchensense --sku Basic

az acr login --name "$REGISTRY"
docker build --tag "$REGISTRY.azurecr.io/kitchensense:bootstrap" .
docker push "$REGISTRY.azurecr.io/kitchensense:bootstrap"
```

The build runs locally, so **Docker must be running on the machine you run this from**.
The obvious alternative, `az acr build`, builds inside ACR and needs no local Docker, but
it goes through ACR Tasks — which this subscription cannot use, and the command fails with
`TasksOperationsNotAllowed`. Building locally and pushing is the same end result: the
registry ends up holding `kitchensense:bootstrap` built from the `Dockerfile` in the
repository root. `az acr login` authenticates with the Azure CLI's own credentials, so the
registry's admin user stays disabled. This mirrors what `deploy.yml` does on every run.

Do not pass `registryName` to the deployment below to use some other
name: later deployments run without it and would fall back to the derived name, leaving
the app pointed at a second, empty registry.

### 2. Deploy the infrastructure

```bash
az deployment group create \
  --resource-group rg-kitchensense \
  --template-file infra/main.bicep \
  --parameters environmentName=production containerAppExists=false \
    containerImage="$REGISTRY.azurecr.io/kitchensense:bootstrap" \
    postgresAdminPassword="$PGPASSWORD"
```

Two of those parameters belong to this first deployment only: `containerAppExists=false`
says there is no running image to preserve yet, and `containerImage` supplies what to
start on instead. Every later deployment is run without either, and leaves the running
image alone — the deploy workflow replaces the bootstrap image on its first run. See [Who
owns the running image](#who-owns-the-running-image) above. `postgresAdminPassword` is the
exception: it is required on **every** deployment, first or not.

Note the outputs — `registryLoginServer` and `containerAppFqdn` are useful for
verification.

### 3. Create the app registration GitHub will sign in as

```bash
az ad app create --display-name kitchensense-deploy
APP_ID=$(az ad app list --display-name kitchensense-deploy --query "[0].appId" -o tsv)
az ad sp create --id "$APP_ID"
```

### 4. Register the federated credential

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

### 5. Give the service principal access

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

echo "Deploy principal object id: $SP_ID"
```

The third role this principal needs — **Key Vault Secrets User**, so the deploy workflow
can read the connection string to migrate with — is *not* created here. `main.bicep`
declares it, from the `deployPrincipalId` parameter. Keeping it in the template means it
is reviewed with everything else and cannot drift out of a tenant nobody remembers
granting.

Note why it is needed at all: Contributor above is not enough. On an RBAC-authorised vault
Contributor grants control-plane rights over the vault but no access to the secret
*values* in it, so `az keyvault secret show` returns Forbidden without a data-plane role.

`deployPrincipalId` is the **object id of the service principal**, not the app
registration's client id. They are different GUIDs, and a role assignment accepts either
before failing at deployment with `PrincipalNotFound`. `$SP_ID` above is the right one.

### 5a. Re-deploy so the vault role assignment exists

There is an ordering knot here: step 2 deploys the template, but the principal it grants
access to is only created in step 3. On a **first** bootstrap into a tenant where that
service principal does not exist yet, pass an empty id at step 2 to skip the assignment:

```bash
--parameters deployPrincipalId=''
```

then re-run the deployment once the principal exists:

```bash
az deployment group create \
  --resource-group rg-kitchensense \
  --template-file infra/main.bicep \
  --parameters environmentName=production deployPrincipalId="$SP_ID" \
    postgresAdminPassword="$PGPASSWORD"
```

For the existing production environment the default already names the right principal, so
this is just a normal deployment. **The deploy workflow's migration step cannot read the
secret until a template deployment has actually run** — the workflow only builds images
and rolls them out; it never applies infrastructure.

### 6. Add the GitHub secrets

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

### 7. Check it works

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

## Migrations

`deploy.yml` runs `alembic upgrade head` against production after pushing the image and
**before** updating the container app, so a failed migration aborts the deploy with the
previous revision still serving. It reads `postgres-connection-string` from the vault with
the same OIDC login the rest of the workflow uses, which is why the deploy principal needs
Key Vault Secrets User in [step 5](#5-give-the-service-principal-access).

Two consequences worth knowing before writing a migration:

- **The old image runs against the new schema** for the minute or so between the migration
  and the rollout. Widen in one deploy and narrow in a later one; a migration that drops or
  renames a column the running revision still selects breaks production before the new
  image is anywhere near it.
- **A stopped server fails the deploy.** The job checks the server state first and fails
  with the `start` command rather than hanging on a connection a stopped server accepts
  and then drops. Deploying does not switch the database on by itself.

The job opens a PostgreSQL firewall rule for the runner's own IP and deletes it in an
`always()` step. GitHub-hosted runners do sit on Azure addresses, so the existing
`AllowAllAzureServices` rule would most likely admit them and both steps could be dropped —
but that is undocumented behaviour, and the day it changes it presents as a connection
timeout in the middle of a deploy. The rule costs roughly a minute per run.

## Staging

Create the resource group, then run steps 1 and 2 against it with a different
`environmentName`. Staging has its own registry, so it needs its own bootstrap image too:

```bash
az group create --name rg-kitchensense-staging --location uksouth

REGISTRY=$(az deployment group what-if \
  --resource-group rg-kitchensense-staging \
  --template-file infra/main.bicep \
  --parameters environmentName=staging containerAppExists=false containerImage=none \
    postgresAdminPassword="$PGPASSWORD" \
  --no-pretty-print \
  --query "changes[?contains(resourceId, 'Microsoft.ContainerRegistry/registries')].after.name | [0]" \
  --output tsv)

az acr create --name "$REGISTRY" --resource-group rg-kitchensense-staging --sku Basic

az acr login --name "$REGISTRY"
docker build --tag "$REGISTRY.azurecr.io/kitchensense:bootstrap" .
docker push "$REGISTRY.azurecr.io/kitchensense:bootstrap"

az deployment group create \
  --resource-group rg-kitchensense-staging \
  --template-file infra/main.bicep \
  --parameters environmentName=staging containerAppExists=false \
    containerImage="$REGISTRY.azurecr.io/kitchensense:bootstrap" \
    postgresAdminPassword="$PGPASSWORD"
```

Staging needs its own federated credential subject (for whichever branch or GitHub
Environment deploys it) and its own role assignments on the staging resource group.
