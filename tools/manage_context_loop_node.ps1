param(
    [ValidateSet('Install', 'Uninstall', 'Status')]
    [string]$Action = 'Status',
    [string]$ProjectRoot = '',
    [string]$CustomNodesRoot = ''
)

$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

function Resolve-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Resolve-CustomNodesRoot(
    [string]$Configured,
    [string]$ResolvedProjectRoot,
    [bool]$AllowPicker
) {
    $Candidates = @()
    if ($Configured) { $Candidates += $Configured }
    if ($env:COMFYUI_CUSTOM_NODES) { $Candidates += $env:COMFYUI_CUSTOM_NODES }
    $ProjectParent = Split-Path -Parent $ResolvedProjectRoot
    $Candidates += (Join-Path $ProjectParent 'ComfyUI\custom_nodes')
    $Candidates += (Join-Path $ProjectParent 'ComfyUI_windows_portable\ComfyUI\custom_nodes')
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Container)) {
            return Resolve-NormalizedPath $Candidate
        }
    }
    if ($AllowPicker) {
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
                    return Resolve-NormalizedPath $Candidate
                }
            }
            throw "The selected folder is not a ComfyUI custom_nodes directory: $Selected"
        }
        throw 'Node installation was cancelled.'
    }
    throw 'ComfyUI custom_nodes was not found. Pass -CustomNodesRoot or set COMFYUI_CUSTOM_NODES.'
}

$ProjectRoot = Resolve-NormalizedPath $ProjectRoot
$CustomNodesRoot = Resolve-CustomNodesRoot $CustomNodesRoot $ProjectRoot ($Action -ne 'Status')

$Packages = @(
    @{
        Name = 'Context Loop upstream'
        Source = Join-Path $ProjectRoot 'vendor\minimax-h3-contex-loop'
        Target = Join-Path $CustomNodesRoot 'ComfyUI-MiniMaxH3-Contex-Loop'
        Required = 'LICENSE'
    },
    @{
        Name = 'MiniMax H3 hybrid conditioning'
        Source = Join-Path $ProjectRoot 'vendor\h3-hybrid-node\minimax_h3_hybrid'
        Target = Join-Path $CustomNodesRoot 'H3PromptStudio_HybridCond'
        Required = '__init__.py'
    },
    @{
        Name = 'Idea2Video rule and project-output adapter'
        Source = Join-Path $ProjectRoot 'comfyui_nodes\H3PromptStudioRuleAdapter'
        Target = Join-Path $CustomNodesRoot 'H3PromptStudioRuleAdapter'
        Required = 'nodes.py'
    }
)

function Get-JunctionTarget([System.IO.FileSystemInfo]$Item) {
    $Value = $Item.Target
    if ($Value -is [System.Array]) { $Value = $Value[0] }
    if (-not $Value) { return '' }
    return Resolve-NormalizedPath ([string]$Value)
}

function Test-ExpectedJunction($Package) {
    if (-not (Test-Path -LiteralPath $Package.Target)) { return $false }
    $Item = Get-Item -LiteralPath $Package.Target -Force
    $Resolved = Get-JunctionTarget $Item
    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
            $Resolved -eq (Resolve-NormalizedPath $Package.Source))
}

if ($Action -eq 'Status') {
    $AllInstalled = $true
    foreach ($Package in $Packages) {
        if (Test-ExpectedJunction $Package) {
            Write-Output "INSTALLED: $($Package.Name): $($Package.Target) -> $($Package.Source)"
        } elseif (Test-Path -LiteralPath $Package.Target) {
            Write-Output "CONFLICT: $($Package.Target) exists but is not the expected junction."
            $AllInstalled = $false
        } else {
            Write-Output "NOT_INSTALLED: $($Package.Target)"
            $AllInstalled = $false
        }
    }
    if ($AllInstalled) {
        Write-Output 'PINNED_VERSION: Context Loop 0.3.20 (81e615c66384e8f747ded5d181ef5807f2775daa) + hybrid + local rule/output adapter'
        exit 0
    }
    exit 1
}

if ($Action -eq 'Install') {
    if (-not (Test-Path -LiteralPath $CustomNodesRoot -PathType Container)) {
        throw "ComfyUI custom_nodes directory is missing: $CustomNodesRoot"
    }
    # Validate every source and destination before creating any junction.
    foreach ($Package in $Packages) {
        if (-not (Test-Path -LiteralPath $Package.Source -PathType Container)) {
            throw "$($Package.Name) source is missing: $($Package.Source)"
        }
        $RequiredPath = Join-Path $Package.Source $Package.Required
        if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
            throw "$($Package.Name) required file is missing: $RequiredPath"
        }
        if (Test-ExpectedJunction $Package) {
            continue
        }
        if (Test-Path -LiteralPath $Package.Target) {
            throw "Refusing to overwrite existing path: $($Package.Target)"
        }
    }
    $CreatedTargets = @()
    try {
        foreach ($Package in $Packages) {
            if (Test-ExpectedJunction $Package) {
                Write-Output "ALREADY_INSTALLED: $($Package.Target)"
                continue
            }
            New-Item -ItemType Junction -Path $Package.Target -Target $Package.Source | Out-Null
            $CreatedTargets += $Package.Target
            Write-Output "INSTALLED: $($Package.Target) -> $($Package.Source)"
        }
    } catch {
        for ($CreatedIndex = $CreatedTargets.Count - 1; $CreatedIndex -ge 0; $CreatedIndex--) {
            $CreatedTarget = $CreatedTargets[$CreatedIndex]
            $CreatedPackage = $Packages | Where-Object { $_.Target -eq $CreatedTarget } | Select-Object -First 1
            if ($CreatedPackage -and (Test-ExpectedJunction $CreatedPackage)) {
                [System.IO.Directory]::Delete((Resolve-NormalizedPath $CreatedTarget))
                Write-Warning "Rolled back junction created by this run: $CreatedTarget"
            }
        }
        throw
    }
    Write-Output 'All Idea2Video nodes are installed. Restart ComfyUI manually once before rendering.'
    exit 0
}

for ($PackageIndex = $Packages.Count - 1; $PackageIndex -ge 0; $PackageIndex--) {
    $Package = $Packages[$PackageIndex]
    $Item = Get-Item -LiteralPath $Package.Target -Force -ErrorAction SilentlyContinue
    if ($null -eq $Item) {
        Write-Output "ALREADY_NOT_INSTALLED: $($Package.Target)"
        continue
    }
    if (-not (Test-ExpectedJunction $Package)) {
        throw "Refusing to remove a path that is not the expected junction: $($Package.Target)"
    }
    [System.IO.Directory]::Delete((Resolve-NormalizedPath $Package.Target))
    if (Test-Path -LiteralPath $Package.Target) {
        throw "The verified junction still exists after uninstall: $($Package.Target)"
    }
    Write-Output "UNINSTALLED: $($Package.Target)"
}
