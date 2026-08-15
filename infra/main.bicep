targetScope = 'resourceGroup'

@description('Environment this deployment represents. Drives tags, the APP_ENV variable and the registry name.')
@allowed([
  'staging'
  'production'
])
param environmentName string

@description('Azure region for all resources.')
param location string = 'uksouth'

@description('Name of the container app. Each environment gets its own resource group, so the name is stable.')
param containerAppName string = 'kitchensense-api'

// ACR names are globally unique and allow alphanumerics only. uniqueString keeps the
// name stable for a given resource group while staying distinct between environments.
@description('Name of the container registry. Derived from the resource group and normally left at the default.')
param registryName string = 'acrkitchensense${uniqueString(resourceGroup().id)}'

@description('Image the container app runs. Leave empty — the default — to keep whatever image the app is already running. The deploy workflow is what rolls out new images; set this to pin or roll back to a specific one, and on the first deployment, where there is no running image to keep.')
param containerImage string = ''

@description('Whether the container app already exists. True, the default, keeps the image it is running. Set to false for the first deployment into an empty resource group; containerImage is then required, since there is no running image to read.')
param containerAppExists bool = true

// Key Vault names are globally unique, 3-24 characters, and allow alphanumerics and
// hyphens only. The prefix is kept short so the 13-character uniqueString fits.
@description('Name of the key vault. Derived from the resource group and normally left at the default.')
param keyVaultName string = 'kv-ks-${uniqueString(resourceGroup().id)}'

@description('Days a deleted key vault stays recoverable before it can be purged.')
@minValue(7)
@maxValue(90)
param keyVaultSoftDeleteRetentionInDays int = 90

@description('Days to retain data in the Log Analytics workspace.')
param logRetentionInDays int = 30

var tags = {
  application: 'kitchensense'
  environment: environmentName
}

// Deploying this template must never change the running image, so the image is read back
// from the app rather than declared here. The lookup lives in a module because an
// `existing` reference in this file would compile to a reference() on the container app
// below — a resource referencing itself, which ARM rejects as a circular dependency.
//
// The module is skipped whenever its output would not be used: ARM's if() evaluates only
// the branch it returns, so currentImage.outputs is never touched when it is.
module currentImage 'current-image.bicep' = if (containerAppExists && empty(containerImage)) {
  name: 'kitchensense-current-image'
  params: {
    containerAppName: containerAppName
  }
}

// Creating the app with no image supplied is refused rather than papered over with a
// public placeholder. A placeholder that does not answer the health probes on port 8000
// leaves the revision permanently unhealthy, and the deployment spends its whole timeout
// waiting before failing with "Operation expired" — a slow, unrecognisable version of the
// error raised here. fail() sits in a branch of the ternary below, and ARM's if()
// evaluates only the branch it returns, so it is reached in this case alone.
var noImageError = 'Nothing to run: containerAppExists is false, so there is no running image to keep, and no containerImage was supplied. Build and push an image, then pass it, for example: --parameters containerAppExists=false containerImage=myregistry.azurecr.io/kitchensense:abc1234. See infra/README.md.'

var resolvedImage = !empty(containerImage)
  ? containerImage
  : (containerAppExists ? currentImage!.outputs.image : fail(noImageError))

var acrPullRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

var keyVaultSecretsUserRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    // The deploy workflow authenticates with a federated credential, so the shared
    // admin account is never needed.
    adminUserEnabled: false
  }
}

// The container app runs as this identity. It is user-assigned rather than
// system-assigned for one reason: a system-assigned principal does not exist until its
// resource is created, so its role assignments can only ever be created *after* the
// container app. That is what caused the failed pull — the app started before AcrPull
// had propagated. A user-assigned identity exists independently, so the role
// assignments below can be created first and the container app can depend on them.
resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-kitchensense-${environmentName}'
  location: location
  tags: tags
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    // Access is granted through Azure RBAC role assignments, not the legacy per-vault
    // access policy list, so permissions live alongside every other role assignment here.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: keyVaultSoftDeleteRetentionInDays
  }
}

// Lets the container app's identity pull images, so no registry credentials are stored
// anywhere.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, appIdentity.id, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

// Lets the same identity read secret values from the vault. "Secrets User" is read-only:
// it can get and list secrets but cannot create or delete them.
resource keyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.id, keyVaultSecretsUserRoleDefinitionId)
  scope: keyVault
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleDefinitionId
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-kitchensense-${environmentName}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionInDays
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-kitchensense-${environmentName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  // Neither role assignment is referenced from inside this resource, so Bicep cannot
  // infer the ordering. Without this the app is created in parallel with them and can
  // attempt its first image pull before AcrPull has propagated.
  dependsOn: [
    acrPull
    keyVaultSecretsUser
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: appIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: resolvedImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'APP_ENV'
              value: environmentName
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
      }
    }
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output appIdentityClientId string = appIdentity.properties.clientId
output containerAppName string = containerApp.name
// Worth checking after an infrastructure deployment: it should be the SHA-tagged image
// the app was already running.
output containerAppImage string = resolvedImage
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
