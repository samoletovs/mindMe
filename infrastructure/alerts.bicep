// mindMe: alert rules on the existing Application Insights component.
//
// Standalone on purpose. main.bicep is the clean-rebuild recipe and must not be redeployed
// against the live app: it would provision a new plan and replace the live app settings.
// This template only adds one action group and three log search alert rules.
//
// Deploy (idempotent):
//   az deployment group what-if -g foundrylab-rg -f infrastructure/alerts.bicep
//   az deployment group create  -g foundrylab-rg -f infrastructure/alerts.bicep
//
// Notifications email the subscription's Owner role, so no address lives in source. To add
// another recipient pass -p alertEmail=<address> at deploy time; never commit it.
//
// Cost: a log search alert evaluated every 15 minutes is USD 0.50 a month, so three rules
// are USD 1.50. Action-group email is free for the first 1,000 a month.

targetScope = 'resourceGroup'

@description('Existing Application Insights component the rules query.')
param appInsightsName string = 'appi-mindme'

@description('Region for the alert rules. Keep it the same as the Application Insights component (swedencentral).')
param location string = resourceGroup().location

@description('Optional extra email recipient. Empty means the subscription Owner only. Pass at deploy time; never commit an address.')
param alertEmail string = ''

@description('Tags applied to all resources.')
param tags object = {
  project: 'mindMe'
  owner: 'samoletovs'
  costCenter: 'personal'
  environment: 'prod'
}

var alertTags = union(tags, { managedBy: 'bicep' })

// Built-in Owner role. The action group emails users who hold it at subscription scope.
var ownerRoleId = '8e3af657-a8ff-443c-a75c-2fe8c4bcb635'

// Every query returns only timestamps, function names or counts: never message content.
var rules = [
  {
    name: 'alert-mindme-scheduled-run-failed'
    displayName: 'mindMe scheduled run failed'
    description: 'A timer function (morning_briefing_timer, weekly_review_timer or reaper_poll_timer) failed. Open the results link to see which, then read that run\'s traces (docs/deploy.md, Alerts).'
    severity: 2
    // Twice the evaluation frequency, so a run whose telemetry arrives late is still seen.
    windowSize: 'PT30M'
    query: 'requests | where name endswith "_timer" and success == false | project timestamp, name'
    operator: 'GreaterThan'
    threshold: 0
  }
  {
    name: 'alert-mindme-heartbeat-missing'
    displayName: 'mindMe heartbeat missing'
    description: 'reaper_poll_timer runs every 30 minutes and has not run for two hours: the host is down, timers are not firing, or telemetry stopped arriving.'
    severity: 1
    windowSize: 'PT2H'
    query: 'requests | where name == "reaper_poll_timer" | project timestamp'
    operator: 'LessThan'
    threshold: 1
  }
  {
    name: 'alert-mindme-http-server-errors'
    displayName: 'mindMe HTTP server errors'
    description: 'An HTTP function returned a 5xx other than 503, or returned 503 at least three times in an hour (for example memex unreachable while Telegram retries captures).'
    severity: 2
    windowSize: 'PT1H'
    query: 'requests | where toint(resultCode) >= 500 | summarize server_errors = countif(toint(resultCode) != 503), unavailable = countif(toint(resultCode) == 503) | where server_errors > 0 or unavailable >= 3'
    operator: 'GreaterThan'
    threshold: 0
  }
]

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-mindme'
  location: 'Global'
  tags: alertTags
  properties: {
    groupShortName: 'mindme'
    enabled: true
    armRoleReceivers: [
      {
        name: 'subscription-owner'
        roleId: ownerRoleId
        useCommonAlertSchema: true
      }
    ]
    emailReceivers: empty(alertEmail) ? [] : [
      {
        name: 'extra-recipient'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

resource alertRules 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for rule in rules: {
  name: rule.name
  location: location
  tags: alertTags
  properties: {
    displayName: rule.displayName
    description: rule.description
    severity: rule.severity
    enabled: true
    scopes: [
      appInsights.id
    ]
    evaluationFrequency: 'PT15M'
    windowSize: rule.windowSize
    // Stateful: one notification when a rule fires and one when it resolves, not one per evaluation.
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: rule.query
          timeAggregation: 'Count'
          operator: rule.operator
          threshold: rule.threshold
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}]

output actionGroupId string = actionGroup.id
output alertRuleNames array = [for (rule, i) in rules: alertRules[i].name]
