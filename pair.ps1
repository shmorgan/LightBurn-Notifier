# Run this ON the LightBurn computer - pairing is localhost-only.
# LightBurn will show a consent dialog; click Allow within 30 seconds.
#
#   .\pair.ps1                   read-only machine/job state (default)
#   .\pair.ps1 -IncludeProject   also allow reading the loaded project's filename
#                                (shown in the page and in alerts). Note: LightBurn's
#                                'project' capability also covers camera imagery,
#                                though this project never requests it.
param([switch]$IncludeProject)
$caps = @('state')
if ($IncludeProject) { $caps += 'project' }
$body = @{ application_name = 'Status Page'; capabilities = $caps } | ConvertTo-Json
$r = Invoke-RestMethod -Uri http://localhost:19520/api/connect -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 40
if ($r.status -eq 'ok') { Write-Host "Secret: $($r.secret)" } else { $r | ConvertTo-Json }
