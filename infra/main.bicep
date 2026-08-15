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
// This is a parameter rather than a variable so containerImage can default from it —
// Bicep does not allow parameter defaults to reference variables.
@description('Name of the container registry. Derived from the resource group and normally left at the default.')
param registryName string = 'acrkitchensense${uniqueString(resourceGroup().id)}'

@description('Image the container app runs. Defaults to the v1 tag in this environment\'s registry; the deploy workflow replaces it with a SHA-tagged image.')
param containerImage string = '${registryName}.azurecr.io/kitchensense:v1'

@description('Days to retain data in the Log Analytics workspace.')
param logRetentionInDays int = 30

var tags = {
  application: 'kitchensense'
  environment: environmentName
}

var acrPullRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
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
  identity: {
    type: 'SystemAssigned'
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
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
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

// Lets the container app's system-assigned identity pull images, so no registry
// credentials are stored anywhere.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, containerApp.id, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
