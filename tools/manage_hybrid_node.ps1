param(
    [ValidateSet('Install', 'Uninstall', 'Status')]
    [string]$Action = 'Status',
    [string]$ProjectRoot = '',
    [string]$CustomNodesRoot = ''
)

$ErrorActionPreference = 'Stop'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$Source = Join-Path $ProjectRoot 'vendor\h3-hybrid-node\minimax_h3_hybrid'

function Resolve-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

if (-not $CustomNodesRoot) {
    $Candidates = @()
    if ($env:COMFYUI_CUSTOM_NODES) { $Candidates += $env:COMFYUI_CUSTOM_NODES }
    $ProjectParent = Split-Path -Parent $ProjectRoot
    $Candidates += (Join-Path $ProjectParent 'ComfyUI\custom_nodes')
    $Candidates += (Join-Path $ProjectParent 'ComfyUI_windows_portable\ComfyUI\custom_nodes')
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Container) {
            $CustomNodesRoot = $Candidate
            break
        }
    }
}
if (-not $CustomNodesRoot -and $Action -ne 'Status') {
    Add-Type -AssemblyName System.Windows.Forms
    $Dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $Dialog.Description = 'Select this ComfyUI installation''s custom_nodes folder'
    $Dialog.ShowNewFolderButton = $false
    if ($Dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $Selected = $Dialog.SelectedPath
        $SelectedCandidates = @()
        if ((Split-Path -Leaf $Selected) -ieq 'custom_nodes') {
            $SelectedCandidates += $Selected
        }
        $SelectedCandidates += (Join-Path $Selected 'custom_nodes')
        $SelectedCandidates += (Join-Path $Selected 'ComfyUI\custom_nodes')
        foreach ($Candidate in $SelectedCandidates) {
            if (Test-Path -LiteralPath $Candidate -PathType Container) {
                $CustomNodesRoot = $Candidate
                break
            }
        }
        if (-not $CustomNodesRoot) {
            throw "The selected folder is not a ComfyUI custom_nodes directory: $Selected"
        }
    } else {
        throw 'Node installation was cancelled.'
    }
}
if (-not $CustomNodesRoot) {
    throw 'ComfyUI custom_nodes was not found. Pass -CustomNodesRoot or set COMFYUI_CUSTOM_NODES.'
}
$CustomNodesRoot = Resolve-NormalizedPath $CustomNodesRoot
$Target = Join-Path $CustomNodesRoot 'H3PromptStudio_HybridCond'

$ExpectedSource = Resolve-NormalizedPath $Source

if ($Action -eq 'Status') {
    if (-not (Test-Path -LiteralPath $Target)) {
        Write-Output "NOT_INSTALLED: $Target"
        exit 1
    }
    $Item = Get-Item -LiteralPath $Target -Force
    $Resolved = Resolve-NormalizedPath $Item.Target
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $Resolved -eq $ExpectedSource) {
        Write-Output "INSTALLED: $Target -> $Resolved"
        exit 0
    }
    Write-Output "CONFLICT: $Target exists but is not the expected junction."
    exit 2
}

if ($Action -eq 'Install') {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Hybrid node source is missing: $Source"
    }
    if (-not (Test-Path -LiteralPath $CustomNodesRoot -PathType Container)) {
        throw "ComfyUI custom_nodes directory is missing: $CustomNodesRoot"
    }
    if (Test-Path -LiteralPath $Target) {
        throw "Refusing to overwrite existing path: $Target"
    }
    New-Item -ItemType Junction -Path $Target -Target $Source | Out-Null
    Write-Output "INSTALLED: $Target -> $Source"
    Write-Output 'Restart ComfyUI once before using the long-video workflow.'
    exit 0
}

$Item = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
if ($null -eq $Item) {
    Write-Output "ALREADY_NOT_INSTALLED: $Target"
    exit 0
}
$Resolved = Resolve-NormalizedPath $Item.Target
if (-not ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $Resolved -ne $ExpectedSource) {
    throw "Refusing to remove a path that is not the expected junction: $Target"
}
Remove-Item -LiteralPath $Target -Force
Write-Output "UNINSTALLED: $Target"
