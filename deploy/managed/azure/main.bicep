// EXPERIMENTAL / CLOUD UNVERIFIED. Compilation is not a deployment test.
targetScope = 'resourceGroup'

@description('Use a NEW resource group, separate from existing production.')
@minLength(3)
@maxLength(16)
param prefix string = 'phone-demo'
param location string = resourceGroup().location
@description('Public Linux amd64 image built with Docker target managed, pinned by sha256 digest.')
param image string
@allowed(['I_ACKNOWLEDGE_CLOUD_UNVERIFIED'])
param deploymentAcknowledgement string
@description('Provision infrastructure first; enable only after reviewing secrets and network configuration.')
param enableApp bool = false
@secure()
@minLength(24)
param databasePassword string
@secure()
@description('String-valued app env settings, including provider keys and a random LOCAL_API_TOKEN. Never commit.')
param appSettings object = {}
@description('Empty uses Azure-managed HTTPS hostname. Custom domain requires a separately configured certificate/DNS.')
param publicBaseUrl string = ''

var suffix = uniqueString(resourceGroup().id, prefix)
var appName = '${prefix}-phone'
var dbName = '${prefix}-${suffix}'
var filesName = 'ph${suffix}'
var endpoint = empty(publicBaseUrl) ? 'https://${appName}.${environment.properties.defaultDomain}' : publicBaseUrl
var databaseUrl = 'postgresql://phoneadmin:${uriComponent(databasePassword)}@${dbName}.postgres.database.azure.com:5432/phone?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt'
var suppliedSettingNames = [for entry in items(appSettings): entry.key]
var settingNames = union(['DEPLOYMENT_MODE', 'DATABASE_URL', 'PUBLIC_BASE_URL', 'DATABASE_SCHEMA_PREFIX', 'MCP_ENABLED', 'HOME', 'OWNER_PROMPT_DIRECTORY', 'RECORDING_NOTICE_DIRECTORY'], suppliedSettingNames)
var effectiveSettings = union(appSettings, {
  DEPLOYMENT_MODE: 'managed'
  DATABASE_URL: databaseUrl
  PUBLIC_BASE_URL: endpoint
  DATABASE_SCHEMA_PREFIX: 'phone'
  MCP_ENABLED: 'true'
  HOME: '/data/runtime-home'
  OWNER_PROMPT_DIRECTORY: '/data/owner-prompts'
  RECORDING_NOTICE_DIRECTORY: '/data/recording-notices'
})

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-network'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.88.0.0/16'] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.88.0.0/23'
          delegations: [{ name: 'apps', properties: { serviceName: 'Microsoft.App/environments' } }]
          serviceEndpoints: [{ service: 'Microsoft.Storage' }]
        }
      }
      {
        name: 'database'
        properties: {
          addressPrefix: '10.88.2.0/24'
          delegations: [{ name: 'postgres', properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' } }]
        }
      }
    ]
  }
}
var appsSubnetId = '${network.id}/subnets/apps'
var dbSubnetId = '${network.id}/subnets/database'

resource dns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: '${prefix}.postgres.database.azure.com'
  location: 'global'
}
resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dns
  name: 'phone-network'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: network.id } }
}
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: dbName
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    administratorLogin: 'phoneadmin'
    administratorLoginPassword: databasePassword
    version: '17'
    storage: { storageSizeGB: 32, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { delegatedSubnetResourceId: dbSubnetId, privateDnsZoneArmResourceId: dns.id, publicNetworkAccess: 'Disabled' }
  }
  dependsOn: [dnsLink]
}
resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'phone'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}
resource files 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: filesName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      virtualNetworkRules: [{ id: appsSubnetId, action: 'Allow' }]
    }
  }
}
resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2024-01-01' = {
  parent: files
  name: 'default'
  properties: { shareDeleteRetentionPolicy: { enabled: true, days: 7 } }
}
resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2024-01-01' = {
  parent: fileService
  name: 'phone-files'
  properties: { shareQuota: 5, enabledProtocols: 'SMB', accessTier: 'TransactionOptimized' }
}
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30, workspaceCapping: { dailyQuotaGb: json('0.1') } }
}
resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${prefix}-environment'
  location: location
  properties: {
    vnetConfiguration: { infrastructureSubnetId: appsSubnetId, internal: false }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey }
    }
  }
}
resource mount 'Microsoft.App/managedEnvironments/storages@2025-01-01' = {
  parent: environment
  name: 'phonefiles'
  properties: {
    azureFile: {
      accountName: files.name
      accountKey: files.listKeys().keys[0].value
      shareName: share.name
      accessMode: 'ReadWrite'
    }
  }
}
resource phone 'Microsoft.App/containerApps@2025-01-01' = if (enableApp) {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8766, transport: 'auto', allowInsecure: false }
      secrets: [for key in settingNames: {
        name: 'cfg-${toLower(replace(key, '_', '-'))}'
        value: string(effectiveSettings[key])
      }]
    }
    template: {
      containers: [{
        name: 'phone'
        image: image
        command: ['uvicorn']
        args: ['app.main:app', '--host', '0.0.0.0', '--port', '8766', '--workers', '1']
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [for key in settingNames: {
          name: key
          secretRef: 'cfg-${toLower(replace(key, '_', '-'))}'
        }]
        volumeMounts: [{ volumeName: 'files', mountPath: '/data' }]
        probes: [
          { type: 'Startup', httpGet: { path: '/health', port: 8766 }, periodSeconds: 5, failureThreshold: 30 }
          { type: 'Liveness', httpGet: { path: '/health', port: 8766 }, periodSeconds: 30 }
          { type: 'Readiness', httpGet: { path: '/readyz', port: 8766 }, periodSeconds: 10 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 1 }
      volumes: [{
        name: 'files'
        storageName: mount.name
        storageType: 'AzureFile'
        mountOptions: 'uid=10001,gid=10001,dir_mode=0700,file_mode=0600,nosharesock'
      }]
    }
  }
  dependsOn: [database]
}

output publicUrl string = endpoint
output applicationName string = appName
output databaseServer string = postgres.name
output fileStorageAccount string = files.name
output cloudVerification string = '${deploymentAcknowledgement}; UNVERIFIED: no Azure deployment or real telephone test has been completed.'
