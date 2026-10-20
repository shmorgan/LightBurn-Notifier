# Run this ON the LightBurn machine (10.255.201.243) - pairing is localhost-only.
# LightBurn will show a consent dialog; click Allow within 30 seconds.
$body = @{ application_name = 'Status Page'; capabilities = @('state') } | ConvertTo-Json
$r = Invoke-RestMethod -Uri http://localhost:19520/api/connect -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 40
if ($r.status -eq 'ok') { Write-Host "Secret: $($r.secret)" } else { $r | ConvertTo-Json }
