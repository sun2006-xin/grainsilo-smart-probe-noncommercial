param(
    [string]$PythonPath = "python",
    [string]$BuildId = (Get-Date -Format "yyyyMMdd-HHmmss")
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$buildRoot = Join-Path $repoRoot ".build\station-user-$BuildId"
$venvRoot = Join-Path $buildRoot "venv"
$workRoot = Join-Path $buildRoot "work"
$distRoot = Join-Path $buildRoot "dist"
$webStage = Join-Path $buildRoot "web"
$iconPath = Join-Path $buildRoot "grain-silo-icon.ico"
$appName = "粮仓集成监控中心"
$packageName = "GrainSilo-Desktop-Windows-x64"
$packageRoot = Join-Path $buildRoot $packageName
$archivePath = Join-Path $buildRoot ($packageName + ".zip")

if (Test-Path -LiteralPath $buildRoot) {
    throw "构建目录已存在，为避免覆盖保留现有产物：$buildRoot。请使用新的 -BuildId。"
}
New-Item -ItemType Directory -Path $buildRoot | Out-Null

$pythonCommand = Get-Command -Name $PythonPath -ErrorAction Stop
if (-not $pythonCommand.Source) {
    throw "无法解析 Python 可执行文件：$PythonPath"
}
$pythonExe = $pythonCommand.Source

# Package a private staging copy, never mutate the working-tree web assets.
$sourceWeb = Join-Path $repoRoot "station\web"
Copy-Item -LiteralPath $sourceWeb -Destination $webStage -Recurse
$demoOnlyAssets = @(
    (Join-Path $webStage "assets\models\grainsilo-quarter-cut.blend"),
    (Join-Path $webStage "assets\models\quarter-cut-demo.glb"),
    (Join-Path $webStage "assets\models\README.md")
)
foreach ($assetPath in $demoOnlyAssets) {
    $resolvedAsset = [System.IO.Path]::GetFullPath($assetPath)
    $resolvedWeb = [System.IO.Path]::GetFullPath($webStage) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedAsset.StartsWith($resolvedWeb, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝删除构建暂存目录之外的文件：$resolvedAsset"
    }
    if (Test-Path -LiteralPath $resolvedAsset -PathType Leaf) {
        Remove-Item -LiteralPath $resolvedAsset
    }
}

& (Join-Path $PSScriptRoot "new_grain_icon.ps1") -OutputPath $iconPath
if (-not $?) { throw "生成粮穗图标失败。" }

& $pythonExe -m venv $venvRoot
if ($LASTEXITCODE -ne 0) { throw "创建隔离构建环境失败，退出码：$LASTEXITCODE" }
$buildPython = Join-Path $venvRoot "Scripts\python.exe"
& $buildPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot "tools\requirements-station-build.txt")
if ($LASTEXITCODE -ne 0) { throw "安装固定版本打包工具失败，退出码：$LASTEXITCODE" }

$webData = $webStage + ";web"
$iconData = $iconPath + ";assets"
& $buildPython -m PyInstaller `
    --onedir `
    --windowed `
    --clean `
    --noconfirm `
    --name $appName `
    --icon $iconPath `
    --paths (Join-Path $repoRoot "station") `
    --add-data $webData `
    --add-data $iconData `
    --specpath $buildRoot `
    --distpath $distRoot `
    --workpath $workRoot `
    (Join-Path $repoRoot "station\desktop_app.py")
if ($LASTEXITCODE -ne 0) { throw "用户版 EXE 构建失败，退出码：$LASTEXITCODE" }

$builtApp = Join-Path $distRoot $appName
New-Item -ItemType Directory -Path $packageRoot | Out-Null
Get-ChildItem -LiteralPath $builtApp | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $packageRoot -Recurse
}
$builtExe = Join-Path $packageRoot ($appName + ".exe")
if (-not (Test-Path -LiteralPath $builtExe -PathType Leaf)) {
    throw "便携包内没有找到主程序：$builtExe"
}
$selfCheck = Start-Process -FilePath $builtExe -ArgumentList "--self-check" `
    -Wait -PassThru -WindowStyle Hidden
if ($selfCheck.ExitCode -ne 0) {
    throw "打包程序隔离自检失败，退出码：$($selfCheck.ExitCode)"
}
Copy-Item -LiteralPath (Join-Path $repoRoot "docs\电脑端运行与数据说明.md") `
    -Destination (Join-Path $packageRoot "使用说明.md")
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") `
    -Destination (Join-Path $packageRoot "LICENSE")
$licenseRoot = Join-Path $packageRoot "LICENSES"
New-Item -ItemType Directory -Path $licenseRoot | Out-Null
$pythonLicense = Join-Path (Split-Path -Parent $pythonExe) "LICENSE.txt"
if (-not (Test-Path -LiteralPath $pythonLicense -PathType Leaf)) {
    throw "未找到随 Python 运行时再分发的许可证文本：$pythonLicense"
}
Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseRoot "Python-LICENSE.txt")
$pyinstallerMetadata = Get-ChildItem -LiteralPath (Join-Path $venvRoot "Lib\site-packages") -Directory -Filter "pyinstaller-*.dist-info" | Select-Object -First 1
if (-not $pyinstallerMetadata) {
    throw "未找到 PyInstaller 许可证目录。"
}
$pyinstallerLicense = Join-Path $pyinstallerMetadata.FullName "licenses\COPYING.txt"
if (-not (Test-Path -LiteralPath $pyinstallerLicense -PathType Leaf)) {
    throw "未找到 PyInstaller COPYING.txt：$pyinstallerLicense"
}
Copy-Item -LiteralPath $pyinstallerLicense -Destination (Join-Path $licenseRoot "PyInstaller-COPYING.txt")
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSES\Apache-2.0.txt") -Destination $licenseRoot
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSES\ECharts-NOTICE.txt") -Destination $licenseRoot
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSES\ECharts-d3-3-Clause.txt") -Destination $licenseRoot
Copy-Item -LiteralPath (Join-Path $repoRoot "docs\第三方组件许可证说明.md") -Destination (Join-Path $packageRoot "第三方组件许可证说明.md")
Copy-Item -LiteralPath (Join-Path $repoRoot "docs\资源来源与外部服务说明.md") -Destination (Join-Path $packageRoot "资源来源与外部服务说明.md")

# Ship the corresponding PC station source with the portable binary. Firmware
# sources are intentionally excluded because they depend on private AutoLink.
$sourcePackageRoot = Join-Path $packageRoot "SOURCE-CODE"
$sourceStationRoot = Join-Path $sourcePackageRoot "station"
$sourceWebRoot = Join-Path $sourceStationRoot "web"
$sourceToolsRoot = Join-Path $sourcePackageRoot "tools"
$sourceDocsRoot = Join-Path $sourcePackageRoot "docs"
New-Item -ItemType Directory -Path $sourceWebRoot, $sourceToolsRoot, $sourceDocsRoot | Out-Null
Get-ChildItem -LiteralPath (Join-Path $repoRoot "station") -File -Filter "*.py" |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $sourceStationRoot }
Copy-Item -Path (Join-Path $webStage "*") -Destination $sourceWebRoot -Recurse
foreach ($toolName in @("build_station_user.ps1", "new_grain_icon.ps1", "requirements-station-build.txt")) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $toolName) -Destination $sourceToolsRoot
}
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSES") -Destination $sourcePackageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $sourcePackageRoot
foreach ($docName in @("电脑端运行与数据说明.md", "第三方组件许可证说明.md", "资源来源与外部服务说明.md")) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\$docName") -Destination $sourceDocsRoot
}
Copy-Item -LiteralPath (Join-Path $repoRoot "docs\便携包源码说明.md") -Destination (Join-Path $sourcePackageRoot "README.md")

$requiredSourceFiles = @(
    "README.md",
    "LICENSE",
    "station\desktop_app.py",
    "station\server.py",
    "station\web\index.html",
    "tools\build_station_user.ps1",
    "tools\requirements-station-build.txt",
    "LICENSES\Apache-2.0.txt",
    "docs\电脑端运行与数据说明.md"
)
foreach ($relativePath in $requiredSourceFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourcePackageRoot $relativePath) -PathType Leaf)) {
        throw "便携包缺少对应源码文件：SOURCE-CODE\$relativePath"
    }
}
Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $archivePath -CompressionLevel Optimal

$hash = Get-FileHash -LiteralPath $archivePath -Algorithm SHA256
Write-Output "用户版程序：$(Join-Path $packageRoot ($appName + '.exe'))"
Write-Output "便携发布包：$archivePath"
Write-Output "SHA-256：$($hash.Hash)"
