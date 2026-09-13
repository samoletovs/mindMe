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

@description('Suffix for the Function App + plan names only. Lets us rebuild the app under a fresh SCM hostname if the platform wedges one, while keeping storage/Key Vault/identity stable. Defaults to `suffix`.')
@minLength(3)
@maxLength(10)
param functionSuffix string = suffix

@description('Name of the existing Foundry AIServices account (for RBAC).')
param foundryAccountName string = 'foundrylab-aiservices'

@description('Name of the existing Foundry project (for diagnostics tagging).')
param foundryProjectName string = 'mindMe'

@description('Allowed Telegram chat id (single-user allowlist, Hard Rule 2). Provided at deploy via param file (read from local env var; never committed).')
param telegramAllowedChatId string

type ActionBriefingConfig = {
  enabled: bool
  modelDeployment: string
}

@description('Opt-in action briefing. Use an existing model deployment and seed memex-action-url in Key Vault before enabling.')
param actionBriefing ActionBriefingConfig = {
  enabled: false
  modelDeployment: ''
}

@description('Tags applied to all resources.')
param tags object = {
  project: 'mindMe'
  owner: 'samoletovs'
  costCenter: 'personal'
  environment: 'prod'
}

// --- Resource names ----------------------------------------------------------

var storageName = toLower('st${namePrefix}${suffix}')
// Dedicated DEPLOYMENT/host storage, separate from the data storage on purpose.
// Flex Consumption wedges its SCM endpoint (persistent 503 on publish that NEVER
// recovers — survives restart, stop/start, and storage swaps) when
// AzureWebJobsStorage uses managed-identity auth against a storage account with
// shared-key access DISABLED. Fix: a shared-key (connection-string) host/deploy
// storage. The data storage (`storageName`) stays MI-only + shared-key-disabled;
// only host/deploy storage uses a key. See docs/deploy.md. (2026-06-30)
var deployStorageName = toLower('st${namePrefix}dep${suffix}')
var keyVaultName = toLower('kv-${namePrefix}-${suffix}')
var logWorkspaceName = 'log-${namePrefix}'
var appInsightsName = 'appi-${namePrefix}'
var managedIdentityName = 'id-${namePrefix}'
var functionAppName = 'func-${namePrefix}-${functionSuffix}'
var functionPlanName = 'plan-${namePrefix}-${functionSuffix}'

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

resource personalOsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'personal-os'
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

// --- Deployment / host storage (shared-key) ---------------------------------
// Holds ONLY the function app package + AzureWebJobsStorage host artifacts.
// Shared-key access is ENABLED here (unlike the data storage) because Flex
// Consumption's deploy plane wedges when host storage is MI-only on a
// shared-key-disabled account. No personal data ever lands here.
resource deployStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: deployStorageName
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
    allowSharedKeyAccess: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource deployBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: deployStorage
  name: 'default'
}

resource deployAppPackageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: deployBlobService
  name: 'app-package'
  properties: {
    publicAccess: 'None'
  }
}

// Connection string for host + deployment storage. Contains an account key, so
// it flows into app settings (the supported Flex non-MI pattern). Data-plane
// access to personal content still uses the UAMI against `storageName`.
var deployStorageConnString = 'DefaultEndpointsProtocol=https;AccountName=${deployStorage.name};AccountKey=${deployStorage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

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
          value: '${deployStorage.properties.primaryEndpoints.blob}app-package'
          authentication: {
            // Connection-string (shared-key) auth — NOT managed identity. MI auth
            // here is what wedged the SCM endpoint for 4 days. The connection
            // string lives in the named app setting below.
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
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
        // Host storage (AzureWebJobsStorage) + deployment storage via a
        // shared-key connection string to the dedicated deploy storage account.
        // Do NOT switch these to `AzureWebJobsStorage__accountName` +
        // managedidentity on a shared-key-disabled account — that wedges the
        // Flex SCM endpoint (persistent 503 on publish). See docs/deploy.md.
        {
          name: 'AzureWebJobsStorage'
          value: deployStorageConnString
        }
        {
          name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          value: deployStorageConnString
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
          name: 'AZURE_STORAGE_PERSONAL_OS_CONTAINER'
          value: personalOsContainer.name
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
          name: 'DIG_GITHUB_TOKEN'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=dig-github-token)'
        }
        {
          name: 'DIG_REPO'
          value: 'samoletovs/mindVault'
        }
        // Reaper poller (github_reapers.py): a fine-grained PAT with, on BOTH
        // samoletovs/mindVault and samoletovs/familyVault — Actions: Read+Write
        // (to workflow_dispatch the reapers), Pull requests: Read, Contents: Read.
        // Seed the `reaper-github-token` secret before redeploy, or the poller
        // falls back to DIG_GITHUB_TOKEN (mindVault-only) and skips familyVault.
        {
          name: 'REAPER_GITHUB_TOKEN'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=reaper-github-token)'
        }
        // memex capture webhook: note/URL/YouTube/voice captures are forwarded here
        // (full URL incl. the function ?code= key), stored as a Key Vault secret so the
        // key never lands in source. Seed memex-webhook-url before any redeploy, or this
        // resolves empty and breaks the capture flows.
        {
          name: 'MEMEX_WEBHOOK_URL'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=memex-webhook-url)'
        }
        {
          name: 'MEMEX_STATE_URL'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=memex-state-url)'
        }
        {
          name: 'MINDME_ACTION_BRIEFING_ENABLED'
          value: string(actionBriefing.enabled)
        }
        {
          name: 'MINDME_BRIEFING_MODEL'
          value: actionBriefing.modelDeployment
        }
        {
          name: 'MEMEX_ACTION_URL'
          value: actionBriefing.enabled ? '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=memex-action-url)' : ''
        }
        {
          name: 'BRIEFING_ENCRYPTION_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=briefing-encryption-key)'
        }
        // Tracing safety (agentFlow Phase 1, AGENTS.md Hard Rule 9):
        // belt-and-suspenders content suppression in case any nested
        // OTel-aware library auto-instruments at runtime. The Function App
        // also sets these defensively in code; both layers are required.
        {
          name: 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT'
          value: 'false'
        }
        {
          name: 'AZURE_TRACING_ENABLED'
          value: 'false'
        }
        {
          name: 'OTEL_PYTHON_DISABLED_INSTRUMENTATIONS'
          value: 'httpx,requests,urllib,urllib3,aiohttp-client,azure_sdk'
        }
      ]
    }
  }
  dependsOn: [
    briefingContainer
    captureQueue
    deployAppPackageContainer
  ]
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
