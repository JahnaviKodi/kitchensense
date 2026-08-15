targetScope = 'resourceGroup'

// Reads the image a container app is already running so an infrastructure deployment can
// keep it instead of resetting it.
//
// This is a separate module for one reason: an `existing` reference in main.bicep would
// compile to a reference() on the very resource main.bicep deploys, and ARM rejects a
// resource that references itself as a circular dependency. A nested deployment is
// resolved before the container app is submitted, so there is no cycle.
//
// The lookup fails with ResourceNotFound if the app does not exist yet, which is why the
// caller only deploys this module when it knows the app is there.

@description('Name of the container app to read. It must already exist.')
param containerAppName string

resource containerApp 'Microsoft.App/containerApps@2024-03-01' existing = {
  name: containerAppName
}

@description('Image the app\'s first container is currently running.')
output image string = containerApp.properties.template.containers[0].image
