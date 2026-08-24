# Kubernetes Lite - live demo script
# Run: powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
# Prereq: stack running (docker compose up -d --build) and Docker daemon up.

param(
    [string]$Api = "http://127.0.0.1:5000/api",
    [string]$Name = "web",
    [string]$Image = "nginx:alpine",
    [int]$Replicas = 3,
    [int]$Min = 2,
    [int]$Max = 6,
    [int]$TargetCpu = 50,
    [int]$LbPortMin = 8000
)

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Pause-Demo($msg) { Write-Host "`n>> $msg" -ForegroundColor Yellow; Read-Host "   press ENTER to continue" }

Step "0. Preflight"
$h = Invoke-RestMethod "$Api/health" -TimeoutSec 5
if ($h.status -ne 'healthy') { throw "API not healthy: $($h | ConvertTo-Json -Compress)" }
Info "control plane healthy, docker connected"

Step "1. Declarative deploy: $Name x$Replicas ($Image, autoscale $Min-$Max @ $TargetCpu%)"
$d = Invoke-RestMethod -Method Post "$Api/deploy" -ContentType "application/json" -Body (
    @{
        name = $Name; image = $Image; replicas = $Replicas; health_port = 80;
        min_replicas = $Min; max_replicas = $Max; target_cpu = $TargetCpu
    } | ConvertTo-Json
)
Info "container ids: $($d.container_ids.Count)"

Step "2. Attach load balancer"
$lb = Invoke-RestMethod -Method Post "$Api/lb" -ContentType "application/json" -Body (
    @{ name = $Name; target_port = 80 } | ConvertTo-Json
)
$LbPort = $lb.lb.port
Info "nginx LB listening on http://localhost:$LbPort"

Step "3. Brand each replica so we can SEE who answers"
$status = Invoke-RestMethod "$Api/status"
foreach ($c in $status.deployments | Where-Object name -eq $Name | Select-Object -ExpandProperty containers) {
    docker exec $c.name sh -c "echo 'Response from $($c.name)' > /usr/share/nginx/html/index.html" | Out-Null
}
Info "branded $($status.deployments[0].running) replicas"

Step "4. Round-robin proof: 8 requests through the LB"
1..8 | ForEach-Object {
    $r = Invoke-WebRequest "http://127.0.0.1:$LbPort/" -UseBasicParsing -TimeoutSec 3
    Write-Host "    req $($_): $($r.Content.Trim())"
}

Pause-Demo "Open the dashboard (http://localhost:5000) and show deployments, badges and event log"

Step "5. Chaos: kill a replica behind the orchestrator's back"
$victims = (Invoke-RestMethod "$Api/status").deployments |
    Where-Object name -eq $Name | Select-Object -ExpandProperty containers
$target = ($victims | Get-Random).full_id
$victimName = ($victims | Where-Object full_id -eq $target).name
docker kill $target | Out-Null
Info "killed $victimName"

Info "waiting for reconcile loop to self-heal (<=10s)..."
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep 3
    $dep = ((Invoke-RestMethod "$Api/status").deployments | Where-Object name -eq $Name)
    Info "running=$($dep.running)/$($dep.desired) heals=$($dep.heal_count)"
} while (($dep.running -lt $dep.desired) -and (Get-Date) -lt $deadline)
$r = Invoke-WebRequest "http://127.0.0.1:$LbPort/" -UseBasicParsing -TimeoutSec 3
Info "LB still serving: HTTP $($r.StatusCode)"

Pause-Demo "Point out the HEAL events and updated replica list on the dashboard"

Step "6. Elasticity: burn CPU in every replica, watch auto-scaler react"
foreach ($c in ((Invoke-RestMethod "$Api/status").deployments |
        Where-Object name -eq $Name).containers) {
    docker exec -d $c.name sh -c "yes > /dev/null" 2>$null
}
Info "load generated; polling autoscaler (interval + cooldown apply)..."
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep 5
    $dep = ((Invoke-RestMethod "$Api/status").deployments | Where-Object name -eq $Name)
    Info "desired=$($dep.desired) running=$($dep.running) avg_cpu=$($dep.avg_cpu)%"
    if ($dep.desired -ge $Max) { break }
}

Step "7. Remove load, watch it settle back down"
$dep = ((Invoke-RestMethod "$Api/status").deployments | Where-Object name -eq $Name)
foreach ($c in $dep.containers) {
    docker exec $c.full_id sh -c 'kill $(pidof yes) 2>/dev/null' 2>$null
}
Info "load stopped; scaler will trim toward min=$Min after its cooldown..."
Start-Sleep 45
$dep = ((Invoke-RestMethod "$Api/status").deployments | Where-Object name -eq $Name)
Info "settled at desired=$($dep.desired) avg_cpu=$($dep.avg_cpu)%"

Pause-Demo "Show AUTOSCALE events on the dashboard - they include the CPU reasoning"

Step "8. Teardown"
Invoke-RestMethod -Method Delete "$Api/delete/$Name" | Out-Null
Start-Sleep 3
$left = docker ps -a --filter "label=k8slite=true" --format "{{.Names}}"
if ($left) { Info "WARNING leftovers: $left" } else { Info "clean: zero managed containers remain" }

Step "Demo complete - full audit trail:"
(Invoke-RestMethod "$Api/events?limit=20").events | ForEach-Object {
    Write-Host "    [$($_.type)] $($_.message)"
}
