param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$bitmap = [System.Drawing.Bitmap]::new(
    256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::Transparent)

$background = [System.Drawing.SolidBrush]::new(
    [System.Drawing.Color]::FromArgb(13, 132, 119))
$grain = [System.Drawing.SolidBrush]::new(
    [System.Drawing.Color]::FromArgb(242, 198, 112))
$grainLight = [System.Drawing.SolidBrush]::new(
    [System.Drawing.Color]::FromArgb(255, 225, 161))
$stem = [System.Drawing.Pen]::new(
    [System.Drawing.Color]::FromArgb(255, 225, 161), 9)
$graphics.FillEllipse($background, 8, 8, 240, 240)
$graphics.DrawLine($stem, 128, 221, 128, 62)

function Draw-Grain([single]$X, [single]$Y, [single]$Angle, $Brush) {
    $state = $graphics.Save()
    $graphics.TranslateTransform($X, $Y)
    $graphics.RotateTransform($Angle)
    $graphics.FillEllipse($Brush, -15, -24, 30, 48)
    $graphics.Restore($state)
}

Draw-Grain 106 153 -48 $grain
Draw-Grain 150 135 48 $grainLight
Draw-Grain 106 116 -48 $grainLight
Draw-Grain 150 98 48 $grain
Draw-Grain 116 80 -32 $grain

$handle = $bitmap.GetHicon()
$icon = [System.Drawing.Icon]::FromHandle($handle)
$stream = [System.IO.File]::Create([System.IO.Path]::GetFullPath($OutputPath))
try {
    $icon.Save($stream)
}
finally {
    $stream.Dispose()
    $icon.Dispose()
    $stem.Dispose()
    $grain.Dispose()
    $grainLight.Dispose()
    $background.Dispose()
    $graphics.Dispose()
    $bitmap.Dispose()
}

Write-Output "粮穗图标已生成：$OutputPath"
