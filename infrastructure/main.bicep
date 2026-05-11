// mindMe — Phase 2 infrastructure.
//
// Deploys: Storage, Key Vault, Log Analytics, App Insights, User-Assigned
// Managed Identity, Flex Consumption Function App, and the RBAC role
// assignments wiring them together.
//
// Scope: resource group (foundrylab-rg). Foundry is in the same RG already.
//
// Deploy:
//   az deployment group create \
//     -g foundrylab-rg \
//     -f infrastructure/main.bicep \
//     -p infrastructure/main.bicepparam

targetScope = 'resourceGroup'

@description('Short, lowercase tag for resource names. Keep small to leave room for storage 24-char cap.')
@minLength(3)
@maxLength(8)
param namePrefix string = 'mindme'

@description('Azure region. Must match the Foundry project region.')
param location string = resourceGroup().location

@description('5-char random suffix appended to globally-unique resources.')
@minLength(3)
@maxLength(8)
param suffix string

@description('Name of the existing Foundry AIServices account (for RBAC).')
param foundryAccountName string = 'foundrylab-aiservices'

@description('Name of the existing Foundry project (for diagnostics tagging).')
param foundryProjectName string = 'mindMe'

@description('Allowed Telegram chat id (single-user allowlist, Hard Rule 2). Provided at deploy via param file (read from local env var; never committed).')
param telegramAllowedChatId string

@description('Tags applied to all resources.')
param tags object = {
  project: 'mindMe'
  owner: 'samoletovs'
  costCenter: 'personal'
  environment: 'prod'
}

// --- Resource names ----------------------------------------------------------

var storageName = toLower('st${namePrefix}${suffix}')
var keyVaultName = toLower('kv-${namePrefix}-${suffix}')
var logWorkspaceName = 'log-${namePrefix}'
var appInsightsName = 'appi-${namePrefix}'
var managedIdentityName = 'id-${namePrefix}'
var functionAppName = 'func-${namePrefix}-${suffix}'
var functionPlanName = 'plan-${namePrefix}-${suffix}'

// --- User-Assigned Managed Identity -----------------------------------------

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
  tags: tags
}

// --- Log Analytics + Application Insights -----------------------------------

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: 1
    }
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logWorkspace.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// --- Storage ----------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    encryption: {
      services: {
        blob: {
          enabled: true
        }
        queue: {
          enabled: true
        }
      }
      keySource: 'Microsoft.Storage'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource briefingContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'briefing-context'
  properties: {
    publicAccess: 'None'
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource captureQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: 'capture-events'
}

// --- Key Vault --------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: null
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// --- Function App (Flex Consumption) ----------------------------------------

resource functionPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: functionPlanName
  location: location
  tags: tags
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    serverFarmId: functionPlan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    keyVaultReferenceIdentity: uami.id
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}app-package'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: uami.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 512
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: uami.properties.clientId
        }
        // Host storage (AzureWebJobsStorage) via the UAMI — required by Flex
        // Consumption when shared-key access is disabled on the storage account.
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storageName
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__clientId'
          value: uami.properties.clientId
        }
        {
          name: 'AZURE_KEYVAULT_NAME'
          value: keyVaultName
        }
        {
          name: 'AZURE_STORAGE_ACCOUNT'
          value: storageName
        }
        {
          name: 'AZURE_STORAGE_BRIEFING_CONTAINER'
          value: 'briefing-context'
        }
        {
          name: 'AZURE_STORAGE_CAPTURE_QUEUE'
          value: 'capture-events'
        }
        {
          name: 'AZURE_AI_PROJECT_ENDPOINT'
          value: 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}'
        }
        {
          name: 'AZURE_AI_AGENT_NAME'
          value: 'companion'
        }
        {
          name: 'TELEGRAM_BOT_TOKEN'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=telegram-bot-token)'
        }
        {
          name: 'TELEGRAM_WEBHOOK_SECRET'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=telegram-webhook-secret)'
        }
        {
          name: 'TELEGRAM_ALLOWED_CHAT_ID'
          value: telegramAllowedChatId
        }
        {
          name: 'BRIEFING_ENCRYPTION_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=briefing-encryption-key)'
        }
      ]
    }
  }
  dependsOn: [
    briefingContainer
    captureQueue
  ]
}

// --- App-package container for deployment-from-storage ----------------------

resource appPackageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'app-package'
  properties: {
    publicAccess: 'None'
  }
}

// --- RBAC role assignments --------------------------------------------------
// Role IDs:
//   Storage Blob Data Owner:        b7e6dc6d-f1e8-4753-8033-0f276bb0955b
//   Storage Queue Data Contributor: 974c5e8b-45b9-4653-ba55-5f855dd0fb88
//   Key Vault Secrets User:         4633458b-17de-408a-b874-0445c86b69e6
//   Monitoring Metrics Publisher:   3913510d-42f4-4e42-8a64-420c390055eb
//   Cognitive Services User:        a97b65f3-24c7-4388-baec-2e87135dc908
//   Azure AI User (Foundry):        53ca6127-db72-4b80-b1b0-d745d6d5456d

resource roleStorageBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, uami.id, 'StorageBlobDataOwner')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  }
}

resource roleStorageQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, uami.id, 'StorageQueueDataContributor')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
  }
}

resource roleKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, uami.id, 'KeyVaultSecretsUser')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

resource roleAppInsightsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, uami.id, 'MonitoringMetricsPublisher')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}

// Foundry AIServices RBAC: needs to be assigned on the existing AIServices
// account. We reference it but don't deploy it.
resource foundryAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryAccountName
}

resource roleFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, uami.id, 'CognitiveServicesUser')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
  }
}

// --- Outputs ----------------------------------------------------------------

output functionAppName string = functionApp.name
output functionAppDefaultHostName string = functionApp.properties.defaultHostName
output storageAccountName string = storage.name
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output managedIdentityClientId string = uami.properties.clientId
output managedIdentityResourceId string = uami.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output logWorkspaceId string = logWorkspace.id
