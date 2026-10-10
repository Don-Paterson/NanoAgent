// NanoAgent rendezvous: Azure Web PubSub + a Flex Consumption Function that issues
// short-lived, single-group client tokens.
//
//   az group create -n rg-nanoagent -l uksouth
//   az deployment group create -g rg-nanoagent -f infra/main.bicep -p baseName=nanoagent
//
// Then publish ../function (deploy.ps1 does all of this).

@description('Short lowercase name used as a prefix (3-11 chars, letters/digits).')
@minLength(3)
@maxLength(11)
param baseName string = 'nanoagent'

param location string = resourceGroup().location

@description('Free_F1: 20 connections, 20k messages/day (one Free instance per subscription). Standard_S1 for regular use.')
@allowed([ 'Free_F1', 'Standard_S1' ])
param pubSubSku string = 'Free_F1'

param hubName string = 'nano'

@description('Comma-separated group prefixes the negotiate function will issue tokens for.')
param groupPrefixes string = 'lab-,sup-'

@description('Client token lifetime in minutes (agents re-negotiate on reconnect).')
param tokenMinutes int = 60

var suffix = take(uniqueString(resourceGroup().id), 8)
var deployContainer = 'app-package'
var roleBlobOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'      // Storage Blob Data Owner
var roleQueueContrib = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'   // Storage Queue Data Contributor
var roleTableContrib = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'   // Storage Table Data Contributor

resource pubsub 'Microsoft.SignalRService/webPubSub@2024-03-01' = {
  name: '${baseName}-wps-${suffix}'
  location: location
  sku: {
    name: pubSubSku
    tier: pubSubSku == 'Free_F1' ? 'Free' : 'Standard'
    capacity: 1
  }
  properties: {
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    tls: { clientCertEnabled: false }
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: toLower('${baseName}${suffix}')
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deployContainer
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${baseName}-id-${suffix}'
  location: location
}

resource roles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in [ roleBlobOwner, roleQueueContrib, roleTableContrib ]: {
  name: guid(storage.id, identity.id, r)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${baseName}-plan-${suffix}'
  location: location
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: '${baseName}-fn-${suffix}'
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deployContainer}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
  }
  dependsOn: [ roles ]
}

resource appSettings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: functionApp
  name: 'appsettings'
  properties: {
    AzureWebJobsStorage__accountName: storage.name
    AzureWebJobsStorage__credential: 'managedidentity'
    AzureWebJobsStorage__clientId: identity.properties.clientId
    WEBPUBSUB_CONNECTION: pubsub.listKeys().primaryConnectionString
    NANO_HUB: hubName
    NANO_GROUP_PREFIXES: groupPrefixes
    NANO_TOKEN_MINUTES: string(tokenMinutes)
  }
}

output functionAppName string = functionApp.name
output functionHost string = functionApp.properties.defaultHostName
output webPubSubHost string = pubsub.properties.hostName
