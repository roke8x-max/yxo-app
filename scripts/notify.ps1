param(
  [string]$Title = "yxo-app notification",
  [string]$Body  = ""
)
$webhook = $env:YXO_NOTIFY_WEBHOOK
if (-not $webhook) {
    Write-Warning "YXO_NOTIFY_WEBHOOK not set, skip notify"
    exit 0
}
$payload = @{ msgtype = "text"; text = @{ content = "$Title`n$Body" } } | ConvertTo-Json -Compress
try {
    curl.exe -s -X POST -H "Content-Type: application/json" -d $payload $webhook
    Write-Output "notify sent: $Title"
} catch {
    Write-Warning "notify failed: $_"
    exit 1
}
