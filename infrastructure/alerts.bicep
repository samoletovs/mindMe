// mindMe: alert rules on the existing Application Insights component.
//
// Standalone on purpose. main.bicep is the clean-rebuild recipe and must not be redeployed
// against the live app: it would provision a new plan and replace the live app settings.
// This template only adds three log search alert rules.
//
// Deploy (idempotent):
//   az deployment group what-if -g foundrylab-rg -f infrastructure/alerts.bicep
//   az deployment group create  -g foundrylab-rg -f infrastructure/alerts.bicep --name mindme-alerts
//
// Notifications go to an existing action group whose receivers are already verified
// (prime-alerts: the owner's email and Azure mobile app push). A new email receiver would
// need a one-time code confirmed within 30 minutes, and the subscription Owner's Entra
// profile has no email address, so an Owner-role receiver would reach nobody. Reusing the
// verified group keeps every address out of this repository and survives redeploys.
//
// Cost: a log search alert evaluated every 15 minutes is USD 0.50 a month per monitored
// time series. The heartbeat and HTTP rules are one series each. The scheduled-run rule is
// split per timer and its query returns only failing timers, so it is billed only for the
// timers that fail: USD 1.50 to 2.50 a month in total.

targetScope = 'resourceGroup'

@description('Existing Application Insights component the rules query.')
param appInsightsName string = 'appi-mindme'

@description('Region for the alert rules. Keep it the same as the Application Insights component (swedencentral).')
param location string = resourceGroup().location

@description('Existing action group to notify. Its receivers must already be verified.')
param actionGroupName string = 'prime-alerts'

@description('Resource group of that action group, in this subscription.')
param actionGroupResourceGroup string = 'prime-rg'

@description('Tags applied to all resources.')
param tags object = {
  project: 'mindMe'
  owner: 'samoletovs'
  costCenter: 'personal'
  environment: 'prod'
}

var alertTags = union(tags, { managedBy: 'bicep' })

// Every query returns only timestamps, function names or counts: never message content.
var rules = [
  {
    name: 'alert-mindme-scheduled-run-failed'
    displayName: 'mindMe scheduled run failed'
    description: 'A timer function (morning_briefing_timer, weekly_review_timer or reaper_poll_timer) failed. The alert names the timer; read that run\'s traces (docs/deploy.md, Alerts).'
    severity: 2
    // Twice the evaluation frequency, so a run whose telemetry arrives late is still seen.
    windowSize: 'PT30M'
    query: 'requests | where name endswith "_timer" and success == false | project timestamp, name'
    operator: 'GreaterThan'
    threshold: 0
    // One alert per timer. A single combined alert stays open while any timer keeps
    // failing, and an open stateful alert does not notify again, so a persistently failing
    // reaper would hide a failed morning briefing.
    dimensions: [
      {
        name: 'name'
        operator: 'Include'
        values: [
          '*'
        ]
      }
    ]
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
    dimensions: []
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
    dimensions: []
  }
]

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' existing = {
  name: actionGroupName
  scope: resourceGroup(actionGroupResourceGroup)
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
          dimensions: rule.dimensions
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
